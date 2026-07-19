"""The assistant: intent routing, FAQ RAG, and the booking state machine.

Flow per inbound message:

    parse  ->  language detect  ->  (handoff? interactive? mid-booking?)  ->
    intent route {info | booking | human}  ->  reply via interactive messages

Booking is a small, explicit state machine driven by button/list replies:

    idle -> booking_service -> booking_date -> booking_time
         -> booking_name -> booking_confirm -> (booked | cancelled | restart)

Everything the LLM does is optional: routing falls back to keywords and FAQ
answers fall back to the top knowledge-base snippet when no NIM key is set.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Optional

from .catalog import (
    DAY_ABBR,
    MONTH_ABBR,
    SERVICES,
    WEEKLY_HOURS,
    get_service,
    service_label,
    t,
)
from .config import Settings
from .kb import KnowledgeBase
from .llm import LLMClient
from .models import InboundMessage
from .state import State, Store
from .transcribe import transcribe_audio
from .wa_client import WhatsAppClient

log = logging.getLogger("whatsapp_agent.agent")

# --- Language / intent lexicons --------------------------------------------
_TOKEN_RE = re.compile(r"[a-z0-9]+")

ES_HINTS = {
    "hola", "gracias", "cita", "precio", "precios", "horario", "horarios",
    "cuanto", "donde", "quiero", "reservar", "buenas", "para", "una", "que",
    "como", "cuando", "tienen", "abierto", "abren", "corte", "barba",
}
EN_HINTS = {
    "hi", "hello", "thanks", "thank", "price", "prices", "hours", "where",
    "want", "book", "appointment", "how", "much", "please", "open", "today",
    "haircut", "beard", "when", "do", "you",
}
GREETING_KW = {
    "hola", "buenas", "buenos", "hey", "hi", "hello", "menu", "menú", "inicio",
    "start", "ola", "saludos",
}
BOOK_KW = {
    "cita", "reservar", "reserva", "agendar", "agenda", "turno", "appointment",
    "book", "booking", "schedule", "reservacion", "reservación", "agendame",
}
HUMAN_KW = {
    "humano", "persona", "agente", "asesor", "reclamo", "queja", "urgente",
    "emergencia", "human", "agent", "person", "manager", "complaint",
    "representative", "hablar",
}
RESET_KW = {"reset", "reiniciar", "inicio", "menu", "menú", "start", "cancelar"}


def _strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(_strip_accents(text).lower()))


class Agent:
    def __init__(
        self,
        settings: Settings,
        wa: WhatsAppClient,
        store: Store,
        kb: KnowledgeBase,
        llm: LLMClient,
    ):
        self.settings = settings
        self.wa = wa
        self.store = store
        self.kb = kb
        self.llm = llm

    # ================================================================= entry
    def handle_message(self, msg: InboundMessage) -> None:
        try:
            self._handle(msg)
        except Exception:  # pragma: no cover - defensive; keep worker alive
            log.exception("Failed to handle message %s from %s", msg.message_id, msg.wa_id)

    def _handle(self, msg: InboundMessage) -> None:
        wa_id = msg.wa_id
        self.store.ensure_conversation(wa_id, msg.profile_name)
        self.wa.mark_read(msg.message_id)
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
            if text:
                self.wa.send_text(wa_id, t("audio_heard", conv.lang, text=text))
        elif msg.type == "image":
            text = msg.caption

        # Language detection (sticky once inferred).
        if text:
            lang = self._detect_lang(text)
            if lang != conv.lang:
                self.store.set_lang(wa_id, lang)
                conv.lang = lang
        lang = conv.lang

        self.store.add_message(wa_id, "user", self._describe_inbound(msg, text))

        # If a human has taken over, stay out of the way (allow an explicit reset).
        if conv.handoff:
            if text and _tokens(text) & RESET_KW:
                self.store.set_handoff(wa_id, False)
                self.store.set_state(wa_id, State.IDLE)
                self.store.clear_draft(wa_id)
                self.wa.send_text(wa_id, t("reset_ack", lang))
                self._send_menu(wa_id, lang)
            return

        # Interactive button / list replies drive the booking machine.
        if payload_id and self._handle_payload(wa_id, lang, payload_id):
            return

        # Voice note we could not transcribe.
        if msg.type == "audio" and not text:
            self.wa.send_text(wa_id, t("audio_unsupported", lang))
            self._send_menu(wa_id, lang)
            return

        # Image without a caption we can act on -> route to a human.
        if msg.type == "image" and not text:
            self.wa.send_text(wa_id, t("image_received", lang))
            self._escalate(wa_id, lang, reason="image")
            return

        if not text:
            self._send_menu(wa_id, lang)
            return

        # Mid-booking: the only free-text step is collecting the name.
        conv = self.store.get_conversation(wa_id)
        if conv.state == State.BOOKING_NAME:
            self._capture_name(wa_id, lang, text)
            return

        # Greetings and explicit resets show the main menu.
        toks = _tokens(text)
        if toks & RESET_KW or (toks and toks <= GREETING_KW):
            self.store.set_state(wa_id, State.IDLE)
            self._send_menu(wa_id, lang)
            return

        # Otherwise, classify and dispatch.
        intent = self._route_intent(text, lang)
        if intent == "booking":
            self._start_booking(wa_id, lang)
        elif intent == "human":
            self._escalate(wa_id, lang, reason="requested")
        else:
            self._answer_info(wa_id, lang, text)

    # ============================================================ payloads
    def _handle_payload(self, wa_id: str, lang: str, payload_id: str) -> bool:
        kind, _, value = payload_id.partition(":")

        if kind == "menu":
            if value == "booking":
                self._start_booking(wa_id, lang)
            elif value == "info":
                self.wa.send_text(wa_id, t("info_prompt", lang))
                self.store.set_state(wa_id, State.IDLE)
            elif value == "human":
                self._escalate(wa_id, lang, reason="requested")
            return True

        if kind == "svc":
            if not get_service(value):
                return False
            self.store.update_draft(wa_id, {"service_id": value})
            self.store.set_state(wa_id, State.BOOKING_DATE)
            self._send_dates(wa_id, lang)
            return True

        if kind == "date":
            self.store.update_draft(wa_id, {"date": value})
            self.store.set_state(wa_id, State.BOOKING_TIME)
            self._send_times(wa_id, lang, value)
            return True

        if kind == "time":
            self.store.update_draft(wa_id, {"time": value})
            self.store.set_state(wa_id, State.BOOKING_NAME)
            self.wa.send_text(wa_id, t("ask_name", lang))
            return True

        if kind == "confirm":
            if value == "yes":
                self._finalize_booking(wa_id, lang)
            elif value == "edit":
                self.wa.send_text(wa_id, t("booking_reset", lang))
                self._start_booking(wa_id, lang)
            elif value == "cancel":
                self.store.set_state(wa_id, State.IDLE)
                self.store.clear_draft(wa_id)
                self.wa.send_text(wa_id, t("booking_cancelled", lang))
            return True

        return False

    # ============================================================= booking
    def _start_booking(self, wa_id: str, lang: str) -> None:
        self.store.clear_draft(wa_id)
        self.store.set_state(wa_id, State.BOOKING_SERVICE)
        rows = [
            {
                "id": f"svc:{svc['id']}",
                "title": svc[lang] if lang in svc else svc["es"],
                "description": f"${svc['price']} · {svc['minutes']} min",
            }
            for svc in SERVICES
        ]
        self.wa.send_list(
            wa_id,
            body=t("choose_service", lang),
            button_text=t("service_button", lang),
            sections=[{"title": t("services_section", lang), "rows": rows}],
        )

    def _send_dates(self, wa_id: str, lang: str) -> None:
        rows = []
        today = self._now().date()
        for day in self._open_days(count=7):
            iso = day.isoformat()
            desc = ""
            if day == today:
                desc = t("today", lang)
            elif day == today + timedelta(days=1):
                desc = t("tomorrow", lang)
            rows.append({"id": f"date:{iso}", "title": self._date_title(day, lang), "description": desc})
        self.wa.send_list(
            wa_id,
            body=t("choose_date", lang),
            button_text=t("dates_button", lang),
            sections=[{"title": t("dates_section", lang), "rows": rows}],
        )

    def _send_times(self, wa_id: str, lang: str, iso_date: str) -> None:
        try:
            day = date_cls.fromisoformat(iso_date)
        except ValueError:
            self.store.set_state(wa_id, State.BOOKING_DATE)
            self._send_dates(wa_id, lang)
            return

        slots = self._slots_for_date(day)
        if not slots:
            self.wa.send_text(wa_id, t("no_slots", lang))
            self.store.set_state(wa_id, State.BOOKING_DATE)
            self._send_dates(wa_id, lang)
            return

        rows = [{"id": f"time:{s}", "title": s} for s in slots[:10]]
        self.wa.send_list(
            wa_id,
            body=t("choose_time", lang, date=self._date_title(day, lang)),
            button_text=t("times_button", lang),
            sections=[{"title": t("times_section", lang), "rows": rows}],
        )

    def _capture_name(self, wa_id: str, lang: str, text: str) -> None:
        name = text.strip()[:60]
        if not name:
            self.wa.send_text(wa_id, t("ask_name", lang))
            return
        draft = self.store.update_draft(wa_id, {"name": name})
        self.store.set_state(wa_id, State.BOOKING_CONFIRM)
        self._send_confirm(wa_id, lang, draft)

    def _send_confirm(self, wa_id: str, lang: str, draft: dict) -> None:
        service = service_label(draft.get("service_id", ""), lang)
        day_iso = draft.get("date", "")
        try:
            pretty_date = self._date_title(date_cls.fromisoformat(day_iso), lang)
        except ValueError:
            pretty_date = day_iso
        self.wa.send_buttons(
            wa_id,
            body=t(
                "confirm_body",
                lang,
                service=service,
                date=pretty_date,
                time=draft.get("time", ""),
                name=draft.get("name", ""),
            ),
            buttons=[
                {"id": "confirm:yes", "title": t("confirm_yes", lang)},
                {"id": "confirm:edit", "title": t("confirm_edit", lang)},
                {"id": "confirm:cancel", "title": t("confirm_cancel", lang)},
            ],
        )

    def _finalize_booking(self, wa_id: str, lang: str) -> None:
        draft = self.store.get_conversation(wa_id).draft
        service_id = draft.get("service_id")
        day_iso = draft.get("date")
        time_str = draft.get("time")
        name = draft.get("name")

        if not (service_id and day_iso and time_str and name):
            # Draft went stale; restart cleanly.
            self.wa.send_text(wa_id, t("booking_reset", lang))
            self._start_booking(wa_id, lang)
            return

        label = service_label(service_id, lang)
        self.store.create_booking(
            wa_id=wa_id,
            customer_name=name,
            service_id=service_id,
            service_label=label,
            date=day_iso,
            time=time_str,
        )
        self.store.set_state(wa_id, State.IDLE)
        self.store.clear_draft(wa_id)
        try:
            pretty_date = self._date_title(date_cls.fromisoformat(day_iso), lang)
        except ValueError:
            pretty_date = day_iso
        self.wa.send_text(
            wa_id,
            t(
                "booking_done",
                lang,
                name=name,
                service=label,
                date=pretty_date,
                time=time_str,
                business=self.settings.business_name,
            ),
        )

    # =============================================================== info
    def _answer_info(self, wa_id: str, lang: str, text: str) -> None:
        context = self.kb.context(text, k=4)

        if self.llm.available:
            answer = self._llm_answer(wa_id, lang, text, context)
            if answer:
                self.store.add_message(wa_id, "assistant", answer)
                self.wa.send_text(wa_id, answer)
                return

        # Offline / LLM-failed fallback: return the single best snippet.
        top = self.kb.search(text, k=1)
        if top:
            body = f"{t('fallback_prefix', lang)}\n\n{top[0].text.strip()[:900]}"
            self.wa.send_text(wa_id, body)
            return

        self._escalate(wa_id, lang, reason="no_answer")

    def _llm_answer(self, wa_id: str, lang: str, text: str, context: str) -> Optional[str]:
        lang_name = "English" if lang == "en" else "Spanish"
        system = (
            f"You are the WhatsApp assistant for {self.settings.business_name}, a "
            "barbershop in Panama City. Answer ONLY using the CONTEXT below. Be "
            "warm and concise (2-4 short sentences) with WhatsApp-friendly "
            f"formatting. Always reply in {lang_name}. Prices are in USD. If the "
            "answer is not in the context, say you are not sure and offer to "
            "connect the customer with a person; never invent details. If the "
            "customer seems ready to book, invite them to reply 'reservar' or "
            "'book'."
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

    # ============================================================ handoff
    def _escalate(self, wa_id: str, lang: str, reason: str) -> None:
        self.store.set_handoff(wa_id, True)
        self.store.set_state(wa_id, State.HUMAN)
        self.store.create_handoff(wa_id, reason)
        message = t("handoff", lang, business=self.settings.business_name)
        self.store.add_message(wa_id, "assistant", message)
        self.wa.send_text(wa_id, message)

    # =============================================================== menu
    def _send_menu(self, wa_id: str, lang: str) -> None:
        self.wa.send_buttons(
            wa_id,
            body=t("menu_body", lang, business=self.settings.business_name),
            buttons=[
                {"id": "menu:booking", "title": t("menu_book", lang)},
                {"id": "menu:info", "title": t("menu_info", lang)},
                {"id": "menu:human", "title": t("menu_human", lang)},
            ],
        )

    # ========================================================= routing/util
    def _route_intent(self, text: str, lang: str) -> str:
        keyword = self._keyword_intent(text)
        if not self.llm.available:
            return keyword or "info"

        system = (
            "You are an intent classifier for a barbershop's WhatsApp assistant. "
            "Classify the user's message into exactly one label: 'booking' (wants "
            "to schedule, change or cancel an appointment), 'human' (wants a real "
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
            for label in ("booking", "human", "info"):
                if label in low:
                    return label
        return keyword or "info"

    @staticmethod
    def _keyword_intent(text: str) -> Optional[str]:
        toks = _tokens(text)
        if toks & HUMAN_KW:
            return "human"
        if toks & BOOK_KW:
            return "booking"
        return None

    @staticmethod
    def _detect_lang(text: str) -> str:
        toks = _tokens(text)
        es = len(toks & ES_HINTS)
        en = len(toks & EN_HINTS)
        if en > es:
            return "en"
        return "es"

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
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(self.settings.timezone))
        except Exception:  # pragma: no cover - missing tzdata / bad tz name
            return datetime.now()

    def _open_days(self, count: int) -> list[date_cls]:
        days: list[date_cls] = []
        cursor = self._now().date()
        horizon = 0
        while len(days) < count and horizon < 21:
            if WEEKLY_HOURS.get(cursor.weekday()) is not None:
                days.append(cursor)
            cursor += timedelta(days=1)
            horizon += 1
        return days

    def _slots_for_date(self, day: date_cls) -> list[str]:
        hours = WEEKLY_HOURS.get(day.weekday())
        if hours is None:
            return []
        open_h, close_h = hours
        now = self._now()
        booked = self.store.booked_times(day.isoformat())
        slots = []
        for hour in range(open_h, close_h):
            slot = f"{hour:02d}:00"
            if slot in booked:
                continue
            # Drop past hours if the requested day is today.
            if day == now.date() and hour <= now.hour:
                continue
            slots.append(slot)
        return slots

    @staticmethod
    def _date_title(day: date_cls, lang: str) -> str:
        abbr = DAY_ABBR.get(lang, DAY_ABBR["es"])[day.weekday()]
        month = MONTH_ABBR.get(lang, MONTH_ABBR["es"])[day.month - 1]
        return f"{abbr} {day.day} {month}"
