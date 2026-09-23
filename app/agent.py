"""The assistant: intent routing, FAQ answers, booking and appointment management.

Flow per inbound message (one contact at a time, see ``_lock_for``)::

    record inbound -> opted out? (BAJA/STOP ... ALTA/START) ->
    language (free text only) -> human took over? ->
    interactive reply? -> typed option / reset / name step ->
    intent route {booking | manage | human | info}

Booking and "my appointments" are explicit state machines (``app/state.py``).
Every state change goes through ``_goto``, which enforces
``state.can_transition``; every interactive reply is checked against the step
that produced it (``state.payload_allowed``) and its value is re-validated
against the live calendar (open day, inside the booking horizon, real slot,
still free). Stale or forged buttons get "option_expired" and the current step
is sent again. Confirming a slot is race-safe: the database refuses a second
confirmed booking for the same date and time (``SlotTaken``).

Everything the LLM does is optional: routing falls back to keywords and FAQ
answers fall back to knowledge-base snippets when no NIM key is set.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .catalog import (
    DAY_ABBR,
    MONTH_ABBR,
    SERVICES,
    WEEKLY_HOURS,
    get_service,
    service_label,
    t,
)
from .clock import Clock, system_clock
from .config import Settings
from .db import SlotTaken
from .kb import KnowledgeBase
from .llm import LLMClient
from .models import InboundMessage
from .state import (
    IN_FLOW_STATES,
    Conversation,
    IllegalTransition,
    State,
    Store,
    can_transition,
    payload_allowed,
)
from .transcribe import transcribe_audio
from .wa_client import WhatsAppClient

log = logging.getLogger("whatsapp_agent.agent")

# How far ahead customers may book, and how many days the date list shows.
BOOKING_HORIZON_DAYS = 21
DATES_SHOWN = 7
LIST_ROWS = 10  # WhatsApp's hard limit per list message

# --- Language / intent lexicons --------------------------------------------
_TOKEN_RE = re.compile(r"[a-z0-9]+")

ES_HINTS = {
    "hola", "gracias", "cita", "citas", "precio", "precios", "horario", "horarios",
    "cuanto", "cuanta", "donde", "quiero", "reservar", "buenas", "buenos", "para",
    "una", "que", "como", "cuando", "tienen", "abierto", "abren", "cierran", "corte",
    "barba", "mi", "mis", "el", "la", "los", "las", "hoy", "manana", "por", "favor",
    "necesito", "puedo", "cuesta", "cuestan", "hablar", "persona", "cancelar",
    "cambiar", "ustedes", "tambien", "aceptan", "estan", "hay", "dias", "tardes",
    "noches", "si", "claro", "vale", "nombre", "llamo",
}
EN_HINTS = {
    "hi", "hello", "thanks", "thank", "price", "prices", "hours", "where",
    "want", "book", "appointment", "appointments", "how", "much", "please", "open",
    "today", "haircut", "beard", "when", "do", "you", "the", "is", "are", "my",
    "can", "what", "your", "cancel", "reschedule", "tomorrow", "yes", "need",
    "does", "cost", "close", "talk", "person", "accept", "there", "morning",
    "afternoon", "evening", "name", "would", "like",
}
GREETING_KW = {
    "hola", "buenas", "buenos", "dias", "tardes", "noches", "hey", "hi", "hello",
    "menu", "inicio", "ola", "saludos", "good", "morning", "afternoon", "evening",
}
BOOK_KW = {
    "cita", "reservar", "reserva", "agendar", "agenda", "turno", "appointment",
    "book", "booking", "schedule", "reservacion", "agendame", "reservo",
}
HUMAN_KW = {
    "humano", "persona", "agente", "asesor", "reclamo", "queja", "urgente",
    "emergencia", "human", "agent", "person", "manager", "complaint",
    "representative", "hablar", "alguien", "someone",
}
RESET_KW = {
    "reset", "reiniciar", "inicio", "menu", "start", "cancelar", "cancel", "volver",
    "salir", "exit", "restart",
}
CANCEL_WORDS = {"cancelar", "cancel", "salir", "exit"}
MANAGE_VERBS = {
    "cancelar", "cancela", "cancelo", "anular", "anula", "cambiar", "cambio", "cambia",
    "mover", "muevo", "reprogramar", "reprogramo", "reagendar", "cancel", "change",
    "move", "reschedule",
}
MANAGE_STRONG = {"reprogramar", "reprogramo", "reagendar", "reschedule", "rebook"}
APPT_NOUNS = {
    "cita", "citas", "reserva", "reservas", "turno", "turnos", "reservacion",
    "appointment", "appointments", "booking", "bookings", "reservation",
}
POSSESSIVES = {"mi", "mis", "my"}
QUESTION_WORDS = {
    "que", "cual", "cuales", "cuanto", "cuanta", "cuantos", "como", "donde", "cuando",
    "what", "how", "where", "when", "which", "why", "who", "can", "do", "does", "is",
    "are",
}
INFO_WORDS = {
    "precio", "precios", "cuesta", "cuestan", "vale", "horario", "horarios", "abren",
    "cierran", "ubicacion", "direccion", "price", "prices", "cost", "hours", "open",
    "close", "address", "located", "promo", "promociones",
}
THANKS_KW = {
    "gracias", "muchas", "mil", "thanks", "thank", "you", "ty", "ok", "okay", "vale",
    "perfecto", "genial", "listo", "great", "cool", "bye", "adios", "chao", "chau",
    "excelente", "buenisimo", "super", "dale", "de", "nada",
}
YES_WORDS = {"si", "sip", "ok", "okay", "dale", "correcto", "confirmo", "yes", "yep", "sure", "claro"}
NO_WORDS = {"no", "nop", "nope"}
WEEKDAYS = {
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5,
    "domingo": 6, "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(_strip_accents(text).lower()))


def _norm(text: str) -> str:
    """Lowercase, accent-free, punctuation-free, single-spaced."""
    return " ".join(_TOKEN_RE.findall(_strip_accents(text).lower()))


def detect_lang(text: str) -> Optional[str]:
    """'es' / 'en' when the text carries evidence, None when it is neutral.

    Neutral input (a name, a time, a date title such as "Thu 24 Sep") returns
    None so it never flips the conversation's language.
    """
    toks = _tokens(text)
    es = len(toks & ES_HINTS) + (1 if ("¿" in text or "¡" in text) else 0)
    en = len(toks & EN_HINTS)
    if en > es:
        return "en"
    if es > en:
        return "es"
    return None


def is_manage_request(text: str) -> bool:
    """'cancel my appointment', 'reprogramar', 'mis citas', 'cambiar la cita'..."""
    toks = _tokens(text)
    if toks & MANAGE_STRONG:
        return True
    if toks & MANAGE_VERBS and toks & APPT_NOUNS:
        return True
    return bool(toks & POSSESSIVES and toks & APPT_NOUNS and not toks & (BOOK_KW - APPT_NOUNS))


def is_reset(text: str) -> bool:
    toks = _tokens(text)
    return bool(toks) and len(toks) <= 3 and bool(toks & RESET_KW) and not is_manage_request(text)


# Opt-out / opt-in keywords. Matched against the WHOLE message (accents and
# punctuation ignored) so "¿el local está en planta baja?" is not an opt-out.
OPT_OUT_ES = {
    "baja", "darme de baja", "dar de baja", "de baja", "cancelar suscripcion",
    "no mas mensajes", "no quiero mas mensajes", "no me escriban mas",
}
OPT_OUT_EN = {"unsubscribe", "opt out", "optout", "stop all", "stopall"}
OPT_OUT_ANY = {"stop"}
OPT_IN_ES = {"alta", "darme de alta", "dar de alta", "suscribirme", "reanudar"}
OPT_IN_EN = {"subscribe", "unstop", "opt in", "optin"}
OPT_IN_ANY = {"start"}


def is_opt_out(text: str) -> bool:
    return _norm(text) in OPT_OUT_ES | OPT_OUT_EN | OPT_OUT_ANY


def is_opt_in(text: str) -> bool:
    return _norm(text) in OPT_IN_ES | OPT_IN_EN | OPT_IN_ANY


def _keyword_lang(text: str) -> Optional[str]:
    norm = _norm(text)
    if norm in OPT_OUT_ES | OPT_IN_ES:
        return "es"
    if norm in OPT_OUT_EN | OPT_IN_EN:
        return "en"
    return None  # STOP / START are used in both languages


def looks_like_name(text: str) -> bool:
    s = text.strip()
    if not 2 <= len(s) <= 60:
        return False
    if any(ch.isdigit() for ch in s) or any(ch in s for ch in "?¿@/:#*"):
        return False
    if len(s.split()) > 5 or not re.search(r"[^\W\d_]", s):
        return False
    toks = _tokens(s)
    banned = BOOK_KW | HUMAN_KW | GREETING_KW | RESET_KW | MANAGE_VERBS | INFO_WORDS | QUESTION_WORDS
    return not toks & banned


def _parse_hour(text: str) -> Optional[int]:
    """'10', '10:00', '10am', '3 pm', '15h' -> hour (0-23); None otherwise."""
    m = re.fullmatch(
        r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.?\s?m\.?|p\.?\s?m\.?|h|hs|hrs|horas)?\s*",
        text.lower(),
    )
    if not m:
        return None
    hour = int(m.group(1))
    if m.group(2) not in (None, "00"):
        return None  # slots are on the hour
    suffix = (m.group(3) or "").replace(".", "").replace(" ", "")
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    return hour if 0 <= hour <= 23 else None


class Agent:
    def __init__(
        self,
        settings: Settings,
        wa: WhatsAppClient,
        store: Store,
        kb: KnowledgeBase,
        llm: LLMClient,
        clock: Optional[Clock] = None,
    ):
        self.settings = settings
        self.wa = wa
        self.store = store
        self.kb = kb
        self.llm = llm
        self.clock: Clock = clock or system_clock
        try:
            self.tz = ZoneInfo(settings.timezone)
        except Exception:  # pragma: no cover - bad tz name / missing tzdata
            log.warning("Unknown BUSINESS_TIMEZONE %r; using UTC", settings.timezone)
            self.tz = ZoneInfo("UTC")
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ================================================================= entry
    def handle_message(self, msg: InboundMessage) -> None:
        # Messages from one contact are handled one at a time: WhatsApp users
        # tap fast, and background tasks run on a thread pool.
        with self._lock_for(msg.wa_id):
            try:
                self._handle(msg)
            except Exception:  # pragma: no cover - defensive; keep worker alive
                log.exception("Failed to handle message %s from %s", msg.message_id, msg.wa_id)

    def _lock_for(self, wa_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(wa_id, threading.Lock())

    def _handle(self, msg: InboundMessage) -> None:
        wa_id = msg.wa_id
        self.store.ensure_conversation(wa_id, msg.profile_name)
        conv = self.store.get_conversation(wa_id)

        # Resolve the message to text and/or an interactive payload id.
        text: Optional[str] = None
        payload_id: Optional[str] = None
        if msg.type == "text":
            text = (msg.text or "").strip()
        elif msg.type == "interactive":
            payload_id = msg.interactive_id
            text = msg.interactive_title
        elif msg.type == "audio":
            text = self._transcribe(msg)
        elif msg.type == "image":
            text = msg.caption

        self.store.add_message(wa_id, "user", self._describe_inbound(msg, text))
        self.store.touch_user(wa_id)

        # Compliance before anything else. An opted-out contact gets nothing
        # at all (not even a read receipt) until they opt back in.
        typed = text if (text and not payload_id) else None
        if conv.opted_out:
            if typed and is_opt_in(typed):
                self._opt_in(conv, msg, typed)
            return
        if typed and is_opt_out(typed):
            self._opt_out(conv, msg, typed)
            return

        self.wa.mark_read(msg.message_id)

        # Language: only free text is evidence. Button titles were written by
        # the bot, and names/times/dates are neutral (detect_lang -> None).
        if text and not payload_id and conv.state != State.BOOKING_NAME:
            detected = detect_lang(text)
            if detected and detected != conv.lang:
                self.store.set_lang(wa_id, detected)
                conv.lang = detected
        lang = conv.lang

        # If a human has taken over, stay out of the way (allow an explicit reset).
        if conv.handoff:
            if text and not payload_id and is_reset(text):
                self.store.set_handoff(wa_id, False)
                self.store.resolve_handoffs(wa_id, by="customer")
                conv.handoff = False
                self._reset(conv)
                self._send_text(wa_id, t("reset_ack", lang))
                self._send_menu(wa_id, lang)
            return

        if msg.type == "audio" and text:
            self._send_text(wa_id, t("audio_heard", lang, text=text))

        # Interactive button / list replies drive the state machines.
        if payload_id:
            self._handle_payload(conv, payload_id)
            return

        # Voice note we could not transcribe.
        if msg.type == "audio" and not text:
            self._send_text(wa_id, t("audio_unsupported", lang))
            self._send_menu(wa_id, lang)
            return

        # Image without a caption we can act on -> route to a human.
        if msg.type == "image" and not text:
            self._send_text(wa_id, t("image_received", lang))
            self._escalate(conv, reason="image")
            return

        if not text:
            self._send_menu(wa_id, lang)
            return

        self._handle_text(conv, text)

    # ============================================================ free text
    def _handle_text(self, conv: Conversation, text: str) -> None:
        wa_id, lang = conv.wa_id, conv.lang

        # 1. A typed answer to the current step ("2", "10am", "mañana", "Haircut").
        option = self._match_typed_option(conv, text)
        if option:
            self._handle_payload(conv, option)
            return

        # 2. Reset / cancel words, checked before anything is taken as a name.
        if is_reset(text):
            was_in_flow = conv.state in IN_FLOW_STATES
            self._reset(conv)
            if was_in_flow and _tokens(text) & CANCEL_WORDS:
                self._send_text(wa_id, t("booking_cancelled", lang))
            else:
                self._send_menu(wa_id, lang)
            return

        # 3. The only free-text step: the customer's name.
        if conv.state == State.BOOKING_NAME:
            if looks_like_name(text):
                self._capture_name(conv, text)
                return
            if not (self._keyword_intent(text) or self._looks_like_question(text)):
                self._send_text(wa_id, t("name_invalid", lang))
                return
            # Otherwise it is a question or a request: handle it below, then
            # ask for the name again.

        # 4. Greetings show the main menu.
        toks = _tokens(text)
        if toks and toks <= GREETING_KW:
            self._reset(conv)
            self._send_menu(wa_id, lang)
            return

        # 5. "gracias", "ok 👍", a lone emoji: acknowledge, don't search the FAQ.
        if not toks or toks <= THANKS_KW:
            if conv.state in IN_FLOW_STATES:
                self._resend_step(conv)
            else:
                self._send_text(wa_id, t("thanks_reply", lang))
            return

        # 6. Classify and dispatch.
        intent = self._route_intent(text)
        if intent == "manage":
            self._start_manage(conv)
        elif intent == "booking":
            self._start_booking(conv)
        elif intent == "human":
            self._escalate(conv, reason="requested")
        else:
            answered = self._answer_info(conv, text)
            if answered and conv.state in IN_FLOW_STATES:
                # Answer first, then pick the flow back up where it was.
                self._resend_step(conv)

    def _match_typed_option(self, conv: Conversation, text: str) -> Optional[str]:
        """Map typed text to one of the options the current step offered."""
        offered = conv.draft.get("_offered") or []
        if conv.state not in IN_FLOW_STATES or not offered:
            return None
        norm = _norm(text)
        if not norm:
            return None

        if conv.state == State.BOOKING_TIME:
            hour = _parse_hour(text)
            if hour is not None and (hour >= 7 or not norm.isdigit()):
                return f"time:{hour:02d}:00"

        if norm.isdigit():
            index = int(norm)
            if 1 <= index <= len(offered):
                return offered[index - 1][0]
            return None

        if conv.state in (State.BOOKING_CONFIRM, State.MANAGE_CANCEL_CONFIRM) and norm in YES_WORDS:
            return offered[0][0]
        if conv.state == State.MANAGE_CANCEL_CONFIRM and norm in NO_WORDS:
            return offered[-1][0]

        exact = [o[0] for o in offered if norm in (_norm(o[1]), _norm(o[2] if len(o) > 2 else ""))]
        if len(exact) == 1:
            return exact[0]

        if conv.state == State.BOOKING_DATE and norm in WEEKDAYS:
            wanted = WEEKDAYS[norm]
            for pid, *_ in offered:
                if pid.startswith("date:"):
                    try:
                        if date_cls.fromisoformat(pid[5:]).weekday() == wanted:
                            return pid
                    except ValueError:
                        continue

        # "cancelar" -> "Cancelar cita", when exactly one option starts that way.
        first_word = [o[0] for o in offered if _norm(o[1]).split()[:1] == [norm]]
        if len(first_word) == 1:
            return first_word[0]
        return None

    # ============================================================ payloads
    def _handle_payload(self, conv: Conversation, payload_id: str) -> None:
        kind, _, value = payload_id.partition(":")
        if not payload_allowed(kind, conv.state):
            log.info("Rejected %r in state %s for %s", payload_id, conv.state, conv.wa_id)
            self._expired(conv)
            return
        handler = getattr(self, f"_on_{kind}")
        handler(conv, value)

    def _on_menu(self, conv: Conversation, value: str) -> None:
        wa_id, lang = conv.wa_id, conv.lang
        if value == "booking":
            self._start_booking(conv)
        elif value == "manage":
            self._start_manage(conv)
        elif value == "info":
            self._reset(conv)
            self._send_text(wa_id, t("info_prompt", lang))
        elif value == "human":
            self._escalate(conv, reason="requested")
        elif value == "main":
            self._reset(conv)
            self._send_menu(wa_id, lang)
        else:
            self._expired(conv)

    def _on_svc(self, conv: Conversation, value: str) -> None:
        if not get_service(value):
            self._expired(conv)
            return
        self._set_draft(conv, service_id=value)
        self._goto(conv, State.BOOKING_DATE)
        self._send_dates(conv)

    def _on_date(self, conv: Conversation, value: str) -> None:
        day = self._valid_day(value)
        if day is None:
            self._expired(conv)
            return
        if not self._free_slots(day):
            self._send_text(conv.wa_id, t("no_slots", conv.lang))
            self._send_dates(conv)
            return
        self._set_draft(conv, date=day.isoformat(), time=None)
        self._goto(conv, State.BOOKING_TIME)
        self._send_times(conv, day)

    def _on_times_page(self, conv: Conversation, value: str) -> None:
        iso, _, offset = value.rpartition("@")
        day = self._valid_day(iso)
        if day is None or iso != conv.draft.get("date") or not offset.isdigit():
            self._expired(conv)
            return
        self._send_times(conv, day, offset=int(offset))

    def _on_time(self, conv: Conversation, value: str) -> None:
        day = self._valid_day(conv.draft.get("date", ""))
        if day is None or value not in self._all_slots(day):
            self._expired(conv)
            return
        if value not in self._free_slots(day):
            self._send_text(
                conv.wa_id, t("slot_taken", conv.lang, date=self._date_title(day, conv.lang), time=value)
            )
            self._send_times(conv, day)
            return
        self._set_draft(conv, time=value)
        if conv.draft.get("mode") == "reschedule" or conv.draft.get("name"):
            # Rescheduling, or re-picking after "slot taken": the name is known.
            self._goto(conv, State.BOOKING_CONFIRM)
            self._send_confirm(conv)
        else:
            self._goto(conv, State.BOOKING_NAME)
            self._ask_name(conv)

    def _on_name(self, conv: Conversation, value: str) -> None:
        if value != "profile" or not conv.profile_name:
            self._expired(conv)
            return
        self._capture_name(conv, conv.profile_name)

    def _on_confirm(self, conv: Conversation, value: str) -> None:
        wa_id, lang = conv.wa_id, conv.lang
        rescheduling = conv.draft.get("mode") == "reschedule"
        if value == "yes":
            if rescheduling:
                self._finalize_reschedule(conv)
            else:
                self._finalize_booking(conv)
        elif value == "edit":
            if rescheduling:
                self._set_draft(conv, date=None, time=None)
                self._goto(conv, State.BOOKING_DATE)
                self._send_dates(conv)
            else:
                self._send_text(wa_id, t("booking_reset", lang))
                self._start_booking(conv)
        elif value == "cancel":
            draft = dict(conv.draft)
            self._reset(conv)
            if rescheduling:
                self._send_text(
                    wa_id,
                    t(
                        "reschedule_aborted",
                        lang,
                        date=self._pretty(draft.get("old_date", ""), lang),
                        time=draft.get("old_time", ""),
                    ),
                )
            else:
                self._send_text(wa_id, t("booking_cancelled", lang))
        else:
            self._expired(conv)

    # -------------------------------------------------------- my appointments
    def _on_appt(self, conv: Conversation, value: str) -> None:
        booking = self._own_upcoming(conv, value)
        if booking is None:
            self._send_text(conv.wa_id, t("appt_gone", conv.lang))
            self._start_manage(conv)
            return
        self._set_draft(conv, booking_id=booking["id"])
        self._goto(conv, State.MANAGE_ACTION)
        self._show_booking(conv, booking)

    def _on_appt_cancel(self, conv: Conversation, value: str) -> None:
        booking = self._selected_booking(conv, value)
        if booking is None:
            return
        self._goto(conv, State.MANAGE_CANCEL_CONFIRM)
        self._ask_cancel_confirm(conv, booking)

    def _on_appt_cancel_yes(self, conv: Conversation, value: str) -> None:
        booking = self._selected_booking(conv, value)
        if booking is None:
            return
        wa_id, lang = conv.wa_id, conv.lang
        if not self.store.cancel_booking(booking["id"], by="customer", wa_id=wa_id):
            self._send_text(wa_id, t("appt_gone", lang))
            self._start_manage(conv)
            return
        self._reset(conv)
        self._send_text(
            wa_id,
            t(
                "appt_cancelled",
                lang,
                service=service_label(booking["service_id"], lang),
                date=self._pretty(booking["date"], lang),
                time=booking["time"],
            ),
        )

    def _on_appt_keep(self, conv: Conversation, value: str) -> None:
        if str(conv.draft.get("booking_id")) != value:
            self._expired(conv)
            return
        self._reset(conv)
        self._send_text(conv.wa_id, t("appt_kept", conv.lang))

    def _on_appt_resched(self, conv: Conversation, value: str) -> None:
        booking = self._selected_booking(conv, value)
        if booking is None:
            return
        self.store.clear_draft(conv.wa_id)
        conv.draft = {}
        self._set_draft(
            conv,
            mode="reschedule",
            booking_id=booking["id"],
            service_id=booking["service_id"],
            name=booking["customer_name"],
            old_date=booking["date"],
            old_time=booking["time"],
        )
        self._goto(conv, State.BOOKING_DATE)
        self._send_dates(conv)

    def _selected_booking(self, conv: Conversation, value: str) -> Optional[dict]:
        """The booking this button refers to, if it is still the selected one."""
        if str(conv.draft.get("booking_id")) != value:
            self._expired(conv)
            return None
        booking = self._own_upcoming(conv, value)
        if booking is None:
            self._send_text(conv.wa_id, t("appt_gone", conv.lang))
            self._start_manage(conv)
        return booking

    def _own_upcoming(self, conv: Conversation, value: str) -> Optional[dict]:
        if not value.isdigit():
            return None
        for booking in self._upcoming(conv.wa_id):
            if booking["id"] == int(value):
                return booking
        return None

    def _upcoming(self, wa_id: str) -> list[dict]:
        now = self._now()
        return self.store.upcoming_bookings(wa_id, now.date().isoformat(), now.strftime("%H:%M"))

    def _start_manage(self, conv: Conversation) -> None:
        wa_id, lang = conv.wa_id, conv.lang
        upcoming = self._upcoming(wa_id)
        self.store.clear_draft(wa_id)
        conv.draft = {}
        if not upcoming:
            self._goto(conv, State.IDLE)
            self._send_buttons(
                wa_id,
                t("manage_none", lang),
                [
                    {"id": "menu:booking", "title": t("menu_book", lang)},
                    {"id": "menu:main", "title": t("menu_main", lang)},
                ],
            )
            return
        if len(upcoming) == 1:
            booking = upcoming[0]
            self._goto(conv, State.MANAGE_ACTION)
            self._set_draft(conv, booking_id=booking["id"])
            self._show_booking(conv, booking)
            return
        self._goto(conv, State.MANAGE_SELECT)
        rows = [
            {
                "id": f"appt:{b['id']}",
                "title": f"{self._pretty(b['date'], lang)} · {b['time']}",
                "description": service_label(b["service_id"], lang),
            }
            for b in upcoming[:LIST_ROWS]
        ]
        self._send_list(conv, t("manage_choose", lang), t("manage_button", lang), t("manage_section", lang), rows)

    def _show_booking(self, conv: Conversation, booking: dict) -> None:
        lang = conv.lang
        bid = booking["id"]
        self._send_buttons(
            conv.wa_id,
            t(
                "manage_detail",
                lang,
                service=service_label(booking["service_id"], lang),
                date=self._pretty(booking["date"], lang),
                time=booking["time"],
                name=booking["customer_name"],
            ),
            [
                {"id": f"appt_cancel:{bid}", "title": t("appt_cancel_btn", lang)},
                {"id": f"appt_resched:{bid}", "title": t("appt_resched_btn", lang)},
                {"id": f"appt_keep:{bid}", "title": t("appt_back_btn", lang)},
            ],
            conv=conv,
        )

    def _ask_cancel_confirm(self, conv: Conversation, booking: dict) -> None:
        lang = conv.lang
        bid = booking["id"]
        self._send_buttons(
            conv.wa_id,
            t(
                "cancel_confirm_body",
                lang,
                service=service_label(booking["service_id"], lang),
                date=self._pretty(booking["date"], lang),
                time=booking["time"],
            ),
            [
                {"id": f"appt_cancel_yes:{bid}", "title": t("cancel_yes", lang)},
                {"id": f"appt_keep:{bid}", "title": t("cancel_no", lang)},
            ],
            conv=conv,
        )

    # ============================================================= booking
    def _start_booking(self, conv: Conversation) -> None:
        lang = conv.lang
        self.store.clear_draft(conv.wa_id)
        conv.draft = {}
        self._goto(conv, State.BOOKING_SERVICE)
        rows = [
            {
                "id": f"svc:{svc['id']}",
                "title": svc.get(lang, svc["es"]),
                "description": f"${svc['price']} · {svc['minutes']} min",
            }
            for svc in SERVICES
        ]
        self._send_list(conv, t("choose_service", lang), t("service_button", lang), t("services_section", lang), rows)

    def _send_dates(self, conv: Conversation) -> None:
        lang = conv.lang
        days = self._bookable_days(DATES_SHOWN)
        if not days:
            self._send_text(conv.wa_id, t("no_days", lang))
            self._escalate(conv, reason="no_availability", announce=False)
            return
        today = self._now().date()
        rows = []
        for day in days:
            desc = ""
            if day == today:
                desc = t("today", lang)
            elif day == today + timedelta(days=1):
                desc = t("tomorrow", lang)
            rows.append({"id": f"date:{day.isoformat()}", "title": self._date_title(day, lang), "description": desc})
        if conv.draft.get("mode") == "reschedule":
            body = t("reschedule_choose_date", lang, service=service_label(conv.draft.get("service_id", ""), lang))
        else:
            body = t("choose_date", lang)
        self._send_list(conv, body, t("dates_button", lang), t("dates_section", lang), rows)

    def _send_times(self, conv: Conversation, day: date_cls, offset: int = 0) -> None:
        lang = conv.lang
        slots = self._free_slots(day)
        if not slots:
            self._send_text(conv.wa_id, t("no_slots", lang))
            self._goto(conv, State.BOOKING_DATE)
            self._send_dates(conv)
            return
        if offset >= len(slots):
            offset = 0
        remaining = slots[offset:]
        if len(remaining) > LIST_ROWS:
            # Paginate: 9 times plus a "more times" row (WhatsApp allows 10 rows).
            page = remaining[: LIST_ROWS - 1]
            rows = [{"id": f"time:{s}", "title": s} for s in page]
            rows.append(
                {
                    "id": f"times_page:{day.isoformat()}@{offset + len(page)}",
                    "title": t("more_times", lang),
                    "description": t("more_times_desc", lang, time=page[-1]),
                }
            )
        else:
            rows = [{"id": f"time:{s}", "title": s} for s in remaining]
        self._send_list(
            conv,
            t("choose_time", lang, date=self._date_title(day, lang)),
            t("times_button", lang),
            t("times_section", lang),
            rows,
        )

    def _ask_name(self, conv: Conversation) -> None:
        lang = conv.lang
        if conv.profile_name:
            self._send_buttons(
                conv.wa_id,
                t("ask_name_button", lang),
                [{"id": "name:profile", "title": conv.profile_name[:20]}],
                conv=conv,
            )
        else:
            self._set_draft(conv, _offered=None)
            self._send_text(conv.wa_id, t("ask_name", lang))

    def _capture_name(self, conv: Conversation, text: str) -> None:
        name = " ".join(text.split())[:60]
        self._set_draft(conv, name=name)
        self._goto(conv, State.BOOKING_CONFIRM)
        self._send_confirm(conv)

    def _send_confirm(self, conv: Conversation) -> None:
        lang = conv.lang
        draft = conv.draft
        service = service_label(draft.get("service_id", ""), lang)
        if draft.get("mode") == "reschedule":
            body = t(
                "reschedule_confirm_body",
                lang,
                service=service,
                old_date=self._pretty(draft.get("old_date", ""), lang),
                old_time=draft.get("old_time", ""),
                date=self._pretty(draft.get("date", ""), lang),
                time=draft.get("time", ""),
            )
        else:
            body = t(
                "confirm_body",
                lang,
                service=service,
                date=self._pretty(draft.get("date", ""), lang),
                time=draft.get("time", ""),
                name=draft.get("name", ""),
            )
        self._send_buttons(
            conv.wa_id,
            body,
            [
                {"id": "confirm:yes", "title": t("confirm_yes", lang)},
                {"id": "confirm:edit", "title": t("confirm_edit", lang)},
                {"id": "confirm:cancel", "title": t("confirm_cancel", lang)},
            ],
            conv=conv,
        )

    def _checked_slot(self, conv: Conversation) -> Optional[tuple[date_cls, str]]:
        """Re-validate the draft's day/time right before writing it."""
        day = self._valid_day(conv.draft.get("date", ""))
        time_str = conv.draft.get("time", "")
        if day is None or time_str not in self._all_slots(day):
            # The slot went stale (e.g. it is now in the past): pick again.
            self._send_text(conv.wa_id, t("option_expired", conv.lang))
            self._set_draft(conv, date=None, time=None)
            self._goto(conv, State.BOOKING_DATE)
            self._send_dates(conv)
            return None
        return day, time_str

    def _slot_taken(self, conv: Conversation, day: date_cls) -> None:
        taken = conv.draft.get("time", "")
        self._set_draft(conv, time=None)
        self._goto(conv, State.BOOKING_TIME)
        self._send_text(conv.wa_id, t("slot_taken", conv.lang, date=self._date_title(day, conv.lang), time=taken))
        self._send_times(conv, day)

    def _finalize_booking(self, conv: Conversation) -> None:
        wa_id, lang = conv.wa_id, conv.lang
        draft = conv.draft
        service_id = draft.get("service_id")
        name = draft.get("name")
        if not (service_id and get_service(service_id) and name and draft.get("date") and draft.get("time")):
            # Draft went stale; restart cleanly.
            self._send_text(wa_id, t("booking_reset", lang))
            self._start_booking(conv)
            return
        checked = self._checked_slot(conv)
        if checked is None:
            return
        day, time_str = checked
        label = service_label(service_id, lang)
        try:
            self.store.create_booking(
                wa_id=wa_id,
                customer_name=name,
                service_id=service_id,
                service_label=label,
                date=day.isoformat(),
                time=time_str,
            )
        except SlotTaken:
            self._slot_taken(conv, day)
            return
        self._reset(conv)
        self._send_text(
            wa_id,
            t(
                "booking_done",
                lang,
                name=name,
                service=label,
                date=self._date_title(day, lang),
                time=time_str,
                business=self.settings.business_name,
            ),
        )

    def _finalize_reschedule(self, conv: Conversation) -> None:
        wa_id, lang = conv.wa_id, conv.lang
        draft = dict(conv.draft)
        checked = self._checked_slot(conv)
        if checked is None:
            return
        day, time_str = checked
        try:
            moved = self.store.reschedule_booking(int(draft["booking_id"]), wa_id, day.isoformat(), time_str)
        except SlotTaken:
            self._slot_taken(conv, day)
            return
        self._reset(conv)
        if not moved:
            self._send_text(wa_id, t("appt_gone", lang))
            return
        self._send_text(
            wa_id,
            t(
                "rescheduled_done",
                lang,
                name=draft.get("name", ""),
                service=service_label(draft.get("service_id", ""), lang),
                date=self._date_title(day, lang),
                time=time_str,
            ),
        )

    # ======================================================== flow helpers
    def _goto(self, conv: Conversation, target: str) -> None:
        if not can_transition(conv.state, target):
            raise IllegalTransition(f"{conv.state} -> {target}")
        if conv.state != target:
            self.store.set_state(conv.wa_id, target)
            conv.state = target

    def _reset(self, conv: Conversation) -> None:
        self._goto(conv, State.IDLE)
        self.store.clear_draft(conv.wa_id)
        conv.draft = {}

    def _set_draft(self, conv: Conversation, **patch) -> None:
        conv.draft = self.store.update_draft(conv.wa_id, patch)

    def _expired(self, conv: Conversation) -> None:
        self._send_text(conv.wa_id, t("option_expired", conv.lang))
        self._resend_step(conv)

    def _resend_step(self, conv: Conversation) -> None:
        """Send the prompt for the step the conversation is currently in."""
        state, draft = conv.state, conv.draft
        if state == State.BOOKING_SERVICE:
            self._start_booking(conv)
        elif state == State.BOOKING_DATE:
            self._send_dates(conv)
        elif state == State.BOOKING_TIME:
            day = self._valid_day(draft.get("date", ""))
            if day is None:
                self._goto(conv, State.BOOKING_DATE)
                self._send_dates(conv)
            else:
                self._send_times(conv, day)
        elif state == State.BOOKING_NAME:
            self._ask_name(conv)
        elif state == State.BOOKING_CONFIRM:
            self._send_confirm(conv)
        elif state in (State.MANAGE_SELECT, State.MANAGE_ACTION, State.MANAGE_CANCEL_CONFIRM):
            booking = self._own_upcoming(conv, str(draft.get("booking_id", "")))
            if state == State.MANAGE_ACTION and booking:
                self._show_booking(conv, booking)
            elif state == State.MANAGE_CANCEL_CONFIRM and booking:
                self._ask_cancel_confirm(conv, booking)
            else:
                self._start_manage(conv)
        else:
            self._send_menu(conv.wa_id, conv.lang)

    # =============================================================== info
    def _answer_info(self, conv: Conversation, text: str) -> bool:
        """Answer a free-text question. Returns True if an answer was sent."""
        wa_id, lang = conv.wa_id, conv.lang
        context = self.kb.context(text, k=4)

        if self.llm.available:
            answer = self._llm_answer(wa_id, lang, text, context)
            if answer:
                self._send_text(wa_id, answer)
                return True

        # Offline / LLM-failed fallback: return the single best snippet.
        top = self.kb.search(text, k=1)
        if top:
            body = f"{t('fallback_prefix', lang)}\n\n{top[0].text.strip()[:900]}"
            self._send_text(wa_id, body)
            return True

        # Nothing matched. Offer a person instead of muting the bot with an
        # automatic handoff: the customer decides.
        self._send_buttons(
            wa_id,
            t("no_answer", lang, business=self.settings.business_name),
            [
                {"id": "menu:human", "title": t("menu_human", lang)},
                {"id": "menu:main", "title": t("menu_main", lang)},
            ],
        )
        return False

    def _llm_answer(self, wa_id: str, lang: str, text: str, context: str) -> Optional[str]:
        lang_name = "English" if lang == "en" else "Spanish"
        system = (
            f"You are the WhatsApp assistant for {self.settings.business_name}, "
            f"{self.settings.business_description}. Answer ONLY using the CONTEXT "
            "below. Be warm and concise (2-4 short sentences) with WhatsApp-friendly "
            f"formatting. Always reply in {lang_name}. If the answer is not in the "
            "context, say you are not sure and offer to connect the customer with a "
            "person; never invent details. If the customer seems ready to book, "
            "invite them to reply 'reservar' or 'book'."
        )
        history = self.store.history(wa_id, limit=6)
        messages = [{"role": "system", "content": system}]
        for turn in history[:-1]:  # exclude the just-added user turn
            if turn["role"] in ("user", "assistant"):
                messages.append(turn)
        messages.append(
            {"role": "user", "content": f"CONTEXT:\n{context or '(no matching FAQ)'}\n\nQUESTION: {text}"}
        )
        return self.llm.chat(messages, temperature=0.3, max_tokens=400)

    # ========================================================= compliance
    def _opt_out(self, conv: Conversation, msg: InboundMessage, text: str) -> None:
        wa_id = conv.wa_id
        lang = _keyword_lang(text) or conv.lang
        if lang != conv.lang:
            self.store.set_lang(wa_id, lang)
            conv.lang = lang
        # Drop whatever was in progress. Staff cannot message an opted-out
        # contact either, so an open handoff is closed too.
        if conv.handoff:
            self.store.set_handoff(wa_id, False)
            self.store.resolve_handoffs(wa_id, by="opt_out")
            conv.handoff = False
        self._reset(conv)
        self.store.set_opted_out(wa_id, True)
        conv.opted_out = True
        self.wa.mark_read(msg.message_id)
        self._send_text(wa_id, t("optout_ack", lang))

    def _opt_in(self, conv: Conversation, msg: InboundMessage, text: str) -> None:
        wa_id = conv.wa_id
        lang = _keyword_lang(text) or conv.lang
        if lang != conv.lang:
            self.store.set_lang(wa_id, lang)
            conv.lang = lang
        self.store.set_opted_out(wa_id, False)
        conv.opted_out = False
        self.wa.mark_read(msg.message_id)
        self._send_text(wa_id, t("optin_ack", lang))
        self._send_menu(wa_id, lang)

    # ============================================================ handoff
    def _escalate(self, conv: Conversation, reason: str, announce: bool = True) -> None:
        wa_id = conv.wa_id
        self.store.clear_draft(wa_id)
        conv.draft = {}
        self._goto(conv, State.HUMAN)
        self.store.set_handoff(wa_id, True)
        conv.handoff = True
        self.store.create_handoff(wa_id, reason)
        if announce:
            self._send_text(wa_id, t("handoff", conv.lang, business=self.settings.business_name))

    # =============================================================== menu
    def _send_menu(self, wa_id: str, lang: str) -> None:
        body = t("menu_body", lang, business=self.settings.business_name)
        if self._upcoming(wa_id):
            # Four choices do not fit in three reply buttons: use a list.
            rows = [
                {"id": "menu:booking", "title": t("menu_book", lang)},
                {"id": "menu:manage", "title": t("menu_manage", lang)},
                {"id": "menu:info", "title": t("menu_info", lang)},
                {"id": "menu:human", "title": t("menu_human", lang)},
            ]
            self._send_list(None, body, t("menu_button", lang), t("menu_section", lang), rows, wa_id=wa_id)
            return
        self._send_buttons(
            wa_id,
            body,
            [
                {"id": "menu:booking", "title": t("menu_book", lang)},
                {"id": "menu:info", "title": t("menu_info", lang)},
                {"id": "menu:human", "title": t("menu_human", lang)},
            ],
        )

    # ========================================================= outbound
    # Every outbound message is also written to the transcript (author=bot),
    # so staff see the whole conversation in /admin.
    def _send_text(self, wa_id: str, body: str) -> None:
        self.wa.send_text(wa_id, body)
        self.store.add_message(wa_id, "assistant", body, author="bot")

    def _send_buttons(
        self, wa_id: str, body: str, buttons: list[dict], conv: Optional[Conversation] = None
    ) -> None:
        self.wa.send_buttons(wa_id, body=body, buttons=buttons)
        titles = " | ".join(b["title"] for b in buttons)
        self.store.add_message(wa_id, "assistant", f"{body}\n[{titles}]", author="bot")
        if conv is not None:
            self._set_draft(conv, _offered=[[b["id"], b["title"], ""] for b in buttons])

    def _send_list(
        self,
        conv: Optional[Conversation],
        body: str,
        button_text: str,
        section_title: str,
        rows: list[dict],
        wa_id: Optional[str] = None,
    ) -> None:
        wa_id = wa_id or conv.wa_id  # type: ignore[union-attr]
        self.wa.send_list(wa_id, body=body, button_text=button_text, sections=[{"title": section_title, "rows": rows}])
        titles = " | ".join(r["title"] for r in rows)
        self.store.add_message(wa_id, "assistant", f"{body}\n[{titles}]", author="bot")
        if conv is not None:
            self._set_draft(conv, _offered=[[r["id"], r["title"], r.get("description", "")] for r in rows])

    # ========================================================= routing/util
    def _route_intent(self, text: str) -> str:
        keyword = self._keyword_intent(text)
        if keyword == "manage":
            return "manage"  # specific phrasing ("cancel my appointment"); no LLM needed
        if not self.llm.available:
            return keyword or "info"

        system = (
            "You are an intent classifier for the WhatsApp assistant of "
            f"{self.settings.business_name}, {self.settings.business_description}. "
            "Classify the user's message into exactly one label: 'booking' (wants "
            "to schedule a new appointment), 'manage' (wants to see, change, "
            "reschedule or cancel an existing appointment), 'human' (wants a real "
            "person, a complaint, or something a bot should not handle), or 'info' "
            "(a question about services, prices, hours, location, policies, "
            "promotions, or general chit-chat). Reply with only the single word."
        )
        out = self.llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.0,
            max_tokens=4,
        )
        if out:
            low = out.strip().lower()
            for label in ("booking", "manage", "human", "info"):
                if label in low:
                    return label
        return keyword or "info"

    @staticmethod
    def _keyword_intent(text: str) -> Optional[str]:
        toks = _tokens(text)
        if toks & HUMAN_KW:
            return "human"
        if is_manage_request(text):
            return "manage"
        if toks & BOOK_KW:
            return "booking"
        return None

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        toks = _tokens(text)
        return "?" in text or "¿" in text or bool(toks & QUESTION_WORDS) or bool(toks & INFO_WORDS)

    @staticmethod
    def _detect_lang(text: str) -> Optional[str]:
        return detect_lang(text)

    def _transcribe(self, msg: InboundMessage) -> Optional[str]:
        if not msg.media_id:
            return None
        data = self.wa.download_media(msg.media_id)
        if not data:
            return None
        return transcribe_audio(data, model_size=self.settings.whisper_model)

    @staticmethod
    def _describe_inbound(msg: InboundMessage, text: Optional[str]) -> str:
        if msg.type == "interactive":
            return f"[button] {msg.interactive_title or msg.interactive_id}"
        if msg.type == "image":
            return f"[image] {text or ''}".strip()
        if msg.type == "audio":
            return f"[voice] {text or '(untranscribed)'}"
        return text or f"[{msg.type}]"

    # --------------------------------------------------------------- time
    def _now(self) -> datetime:
        """Current time in the business timezone, from the injectable clock."""
        return self.clock().astimezone(self.tz)

    def _all_slots(self, day: date_cls) -> list[str]:
        """Every bookable hour of a day per WEEKLY_HOURS, minus hours already past."""
        hours = WEEKLY_HOURS.get(day.weekday())
        if hours is None:
            return []
        open_h, close_h = hours
        now = self._now()
        slots = []
        for hour in range(open_h, close_h):
            # Drop past hours (and the current one) if the day is today.
            if day == now.date() and hour <= now.hour:
                continue
            slots.append(f"{hour:02d}:00")
        return slots

    def _free_slots(self, day: date_cls) -> list[str]:
        booked = self.store.booked_times(day.isoformat())
        return [s for s in self._all_slots(day) if s not in booked]

    # Kept for callers of the old name.
    _slots_for_date = _free_slots

    def _candidate_days(self) -> list[date_cls]:
        """Open days from today through the booking horizon that still have hours left."""
        today = self._now().date()
        return [
            today + timedelta(days=offset)
            for offset in range(BOOKING_HORIZON_DAYS)
            if self._all_slots(today + timedelta(days=offset))
        ]

    def _bookable_days(self, count: int) -> list[date_cls]:
        """The first ``count`` candidate days that still have a free slot."""
        days = self._candidate_days()
        if not days:
            return []
        booked = self.store.booked_times_between(days[0].isoformat(), days[-1].isoformat())
        free = [d for d in days if set(self._all_slots(d)) - booked.get(d.isoformat(), set())]
        return free[:count]

    def _open_days(self, count: int) -> list[date_cls]:
        return self._bookable_days(count)

    def _valid_day(self, iso: str) -> Optional[date_cls]:
        """The date if it is open, not past and inside the horizon; else None."""
        try:
            day = date_cls.fromisoformat(iso)
        except (TypeError, ValueError):
            return None
        return day if day in self._candidate_days() else None

    @staticmethod
    def _date_title(day: date_cls, lang: str) -> str:
        abbr = DAY_ABBR.get(lang, DAY_ABBR["es"])[day.weekday()]
        month = MONTH_ABBR.get(lang, MONTH_ABBR["es"])[day.month - 1]
        return f"{abbr} {day.day} {month}"

    def _pretty(self, iso: str, lang: str) -> str:
        try:
            return self._date_title(date_cls.fromisoformat(iso), lang)
        except (TypeError, ValueError):
            return iso
