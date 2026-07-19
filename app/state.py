"""Per-contact conversation state machine and message history.

``State`` names every stage a conversation can be in and declares the legal
transitions between them, so the flow is documented in one place rather than
scattered as bare strings. ``Store`` persists the current state, the in-progress
booking draft, the language, the handoff flag, and the full message transcript
in SQLite (see ``data/schema.sql``). It inherits the connection/dedupe/booking
plumbing from ``app.db.Database``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .db import Database


class State:
    IDLE = "idle"
    BOOKING_SERVICE = "booking_service"
    BOOKING_DATE = "booking_date"
    BOOKING_TIME = "booking_time"
    BOOKING_NAME = "booking_name"
    BOOKING_CONFIRM = "booking_confirm"
    HUMAN = "human"


ALL_STATES: frozenset[str] = frozenset(
    {
        State.IDLE,
        State.BOOKING_SERVICE,
        State.BOOKING_DATE,
        State.BOOKING_TIME,
        State.BOOKING_NAME,
        State.BOOKING_CONFIRM,
        State.HUMAN,
    }
)

# Ordered steps of the booking sub-flow.
BOOKING_STEPS: tuple[str, ...] = (
    State.BOOKING_SERVICE,
    State.BOOKING_DATE,
    State.BOOKING_TIME,
    State.BOOKING_NAME,
    State.BOOKING_CONFIRM,
)

# Legal transitions. Every state may return to idle (cancel/reset) or escalate to
# a human, so those targets are added implicitly by ``can_transition``.
TRANSITIONS: dict[str, set[str]] = {
    State.IDLE: {State.BOOKING_SERVICE},
    State.BOOKING_SERVICE: {State.BOOKING_DATE},
    State.BOOKING_DATE: {State.BOOKING_TIME},
    State.BOOKING_TIME: {State.BOOKING_NAME},
    State.BOOKING_NAME: {State.BOOKING_CONFIRM},
    State.BOOKING_CONFIRM: {State.BOOKING_SERVICE},
    State.HUMAN: set(),
}


def can_transition(current: str, target: str) -> bool:
    """True if moving current -> target is allowed by the machine."""
    if target not in ALL_STATES:
        return False
    # Cancel/reset to idle and escalation to a human are always permitted.
    if target in (State.IDLE, State.HUMAN):
        return True
    return target in TRANSITIONS.get(current, set())


@dataclass
class Conversation:
    wa_id: str
    state: str
    lang: str
    draft: dict
    handoff: bool
    profile_name: Optional[str]


class Store(Database):
    """SQLite-backed conversation store: state, draft, language and history."""

    # ---------------------------------------------------------- conversations
    def ensure_conversation(self, wa_id: str, profile_name: Optional[str] = None) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO conversations (wa_id, profile_name) VALUES (?, ?)",
                (wa_id, profile_name),
            )
            if profile_name:
                con.execute(
                    "UPDATE conversations SET profile_name = ? "
                    "WHERE wa_id = ? AND (profile_name IS NULL OR profile_name = '')",
                    (profile_name, wa_id),
                )

    def get_conversation(self, wa_id: str) -> Conversation:
        with self._connect() as con:
            row = con.execute(
                "SELECT wa_id, state, lang, draft, handoff, profile_name "
                "FROM conversations WHERE wa_id = ?",
                (wa_id,),
            ).fetchone()
        if row is None:
            return Conversation(wa_id, State.IDLE, "es", {}, False, None)
        return Conversation(
            wa_id=row["wa_id"],
            state=row["state"],
            lang=row["lang"],
            draft=json.loads(row["draft"] or "{}"),
            handoff=bool(row["handoff"]),
            profile_name=row["profile_name"],
        )

    def set_state(self, wa_id: str, state: str) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE conversations SET state = ?, updated_at = datetime('now') WHERE wa_id = ?",
                (state, wa_id),
            )

    def set_lang(self, wa_id: str, lang: str) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE conversations SET lang = ?, updated_at = datetime('now') WHERE wa_id = ?",
                (lang, wa_id),
            )

    def set_handoff(self, wa_id: str, value: bool) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE conversations SET handoff = ?, updated_at = datetime('now') WHERE wa_id = ?",
                (1 if value else 0, wa_id),
            )

    def update_draft(self, wa_id: str, patch: dict) -> dict:
        with self._connect() as con:
            row = con.execute(
                "SELECT draft FROM conversations WHERE wa_id = ?", (wa_id,)
            ).fetchone()
            draft = json.loads(row["draft"] or "{}") if row else {}
            draft.update(patch)
            con.execute(
                "UPDATE conversations SET draft = ?, updated_at = datetime('now') WHERE wa_id = ?",
                (json.dumps(draft, ensure_ascii=False), wa_id),
            )
            return draft

    def clear_draft(self, wa_id: str) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE conversations SET draft = '{}', updated_at = datetime('now') WHERE wa_id = ?",
                (wa_id,),
            )

    # -------------------------------------------------------------- messages
    def add_message(self, wa_id: str, role: str, content: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO messages (wa_id, role, content) VALUES (?, ?, ?)",
                (wa_id, role, content),
            )

    def history(self, wa_id: str, limit: int = 10) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT role, content FROM messages WHERE wa_id = ? ORDER BY id DESC LIMIT ?",
                (wa_id, limit),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
