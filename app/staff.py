"""The staff side of human handoff.

``StaffDesk`` holds everything a person on the team does: see the open handoff
queue, read a transcript, reply to the customer through the same WhatsApp
client the bot uses, give the conversation back to the bot, and look after
bookings. The HTTP API (``app/admin.py``) and the chat simulator
(``python -m app chat`` with ``/staff`` and ``/resolve``) both call it, so the
rules live in one place:

- Staff may only send free-form messages inside WhatsApp's 24-hour customer
  service window (counted from the customer's last message). Outside it Meta
  requires an approved template, so the reply is refused with an explanation.
- Nobody messages a contact who opted out (BAJA/STOP).
- A staff reply takes the conversation over (handoff on) so the bot never talks
  over a person. ``resolve`` hands it back to the bot.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import timedelta
from typing import Optional

from .catalog import DAY_ABBR, MONTH_ABBR, service_label, t
from .clock import from_db
from .state import State

WINDOW = timedelta(hours=24)


class StaffError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _pretty(iso: str, lang: str) -> str:
    try:
        day = date_cls.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{DAY_ABBR[lang][day.weekday()]} {day.day} {MONTH_ABBR[lang][day.month - 1]}"


class StaffDesk:
    def __init__(self, services):
        self.services = services
        self.store = services.store
        self.wa = services.wa
        self.settings = services.settings
        self.clock = services.clock

    # ------------------------------------------------------------ helpers
    def _conversation(self, wa_id: str):
        if not self.store.conversation_exists(wa_id):
            raise StaffError(404, "not_found", f"No conversation with {wa_id}")
        return self.store.get_conversation(wa_id)

    def window(self, conv) -> dict:
        """Where the contact stands with WhatsApp's 24-hour service window."""
        if not conv.last_user_at:
            return {"open": False, "last_user_at": None, "hours_since": None, "closes_at": None}
        last = from_db(conv.last_user_at)
        since = self.clock() - last
        return {
            "open": since < WINDOW,
            "last_user_at": conv.last_user_at,
            "hours_since": round(since.total_seconds() / 3600, 1),
            "closes_at": (last + WINDOW).strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _can_message(self, conv) -> tuple[bool, Optional[str]]:
        if conv.opted_out:
            return False, "The customer opted out (BAJA/STOP); do not message them."
        window = self.window(conv)
        if not window["open"]:
            ago = f"{window['hours_since']} h ago" if window["hours_since"] is not None else "never"
            return False, (
                f"Outside WhatsApp's 24-hour window (the customer last wrote {ago}). "
                "Free-form messages are not allowed; send an approved message template "
                "instead (see docs/meta-setup.md, section 7)."
            )
        return True, None

    def _send(self, wa_id: str, body: str, author: str) -> None:
        if self.wa.send_text(wa_id, body) is None:
            raise StaffError(502, "send_failed", "The WhatsApp API did not accept the message.")
        self.store.add_message(wa_id, "assistant", body, author=author)

    def _summary(self, conv) -> dict:
        return {
            "wa_id": conv.wa_id,
            "profile_name": conv.profile_name,
            "lang": conv.lang,
            "state": conv.state,
            "handoff": conv.handoff,
            "opted_out": conv.opted_out,
            "window": self.window(conv),
        }

    # -------------------------------------------------------------- queue
    def queue(self, last_messages: int = 5) -> list[dict]:
        items = []
        for row in self.store.open_handoffs():
            conv = self.store.get_conversation(row["wa_id"])
            item = self._summary(conv)
            item.update(
                {
                    "handoff_id": row["id"],
                    "reason": row["reason"],
                    "queued_at": row["created_at"],
                    "last_messages": self.store.transcript(row["wa_id"], limit=last_messages),
                }
            )
            items.append(item)
        return items

    def conversation(self, wa_id: str, limit: int = 50) -> dict:
        conv = self._conversation(wa_id)
        out = self._summary(conv)
        out["draft"] = {k: v for k, v in conv.draft.items() if not k.startswith("_")}
        out["bookings"] = [b for b in self.store.list_bookings(status=None) if b["wa_id"] == wa_id]
        out["messages"] = self.store.transcript(wa_id, limit=limit)
        return out

    # ------------------------------------------------------------- actions
    def reply(self, wa_id: str, text: str) -> dict:
        text = text.strip()
        if not text:
            raise StaffError(422, "empty", "The reply text is empty.")
        conv = self._conversation(wa_id)
        ok, why = self._can_message(conv)
        if not ok:
            code = "opted_out" if conv.opted_out else "template_required"
            raise StaffError(409, code, why or "")
        if not conv.handoff:
            # Staff stepped in on their own: silence the bot until resolved.
            self.store.set_handoff(wa_id, True)
            self.store.set_state(wa_id, State.HUMAN)
            self.store.clear_draft(wa_id)
            self.store.create_handoff(wa_id, "staff")
        self._send(wa_id, text, author="staff")
        return {"wa_id": wa_id, "sent": True, "window": self.window(self.store.get_conversation(wa_id))}

    def resolve(self, wa_id: str, notify: bool = True) -> dict:
        conv = self._conversation(wa_id)
        closed = self.store.resolve_handoffs(wa_id, by="staff")
        notified, note = False, None
        if not (conv.handoff or closed):
            # Nothing to hand back; leave whatever the customer is doing alone.
            return {"wa_id": wa_id, "resolved": 0, "bot_active": True, "notified": False,
                    "note": "There was no open handoff."}
        self.store.set_handoff(wa_id, False)
        self.store.set_state(wa_id, State.IDLE)
        self.store.clear_draft(wa_id)
        if notify:
            ok, why = self._can_message(conv)
            if ok:
                self._send(wa_id, t("handoff_resolved", conv.lang, business=self.settings.business_name), author="bot")
                notified = True
            else:
                note = why
        return {"wa_id": wa_id, "resolved": closed, "bot_active": True, "notified": notified, "note": note}

    # ------------------------------------------------------------ bookings
    def bookings(self, day: Optional[str] = None, status: str = "confirmed") -> list[dict]:
        wanted = None if status == "all" else status
        if day:
            try:
                date_cls.fromisoformat(day)
            except ValueError:
                raise StaffError(422, "bad_date", "date must be YYYY-MM-DD")
            return self.store.list_bookings(date=day, status=wanted)
        today = self.clock().astimezone(self.services.agent.tz).date().isoformat()
        return self.store.list_bookings(from_date=today, status=wanted)

    def cancel_booking(self, booking_id: int, notify: bool = True) -> dict:
        booking = self.store.get_booking(booking_id)
        if booking is None:
            raise StaffError(404, "not_found", f"No booking {booking_id}")
        if booking["status"] != "confirmed" or not self.store.cancel_booking(booking_id, by="staff"):
            raise StaffError(409, "not_confirmed", f"Booking {booking_id} is {booking['status']}, not confirmed.")
        notified, note = False, None
        if notify and self.store.conversation_exists(booking["wa_id"]):
            conv = self.store.get_conversation(booking["wa_id"])
            ok, why = self._can_message(conv)
            if ok:
                lang = conv.lang
                body = t(
                    "staff_cancelled",
                    lang,
                    name=booking["customer_name"],
                    service=service_label(booking["service_id"], lang),
                    date=_pretty(booking["date"], lang),
                    time=booking["time"],
                    business=self.settings.business_name,
                )
                self._send(booking["wa_id"], body, author="staff")
                notified = True
            else:
                note = why
        return {"booking": self.store.get_booking(booking_id), "notified": notified, "note": note}
