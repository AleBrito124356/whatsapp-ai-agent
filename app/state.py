"""Per-contact conversation state machine and message history.

``State`` names every stage a conversation can be in and ``TRANSITIONS``
declares the legal moves between them. The agent routes *every* state change
through :func:`can_transition` (see ``Agent._goto``), and interactive replies
are only accepted in the step that produced them (:data:`PAYLOAD_STATES`), so a
stale or forged button can never push a conversation somewhere it should not
be.

``Store`` persists the current state, the in-progress draft, the language, the
handoff and opt-out flags, the 24-hour-window timestamp and the full message
transcript in SQLite (see ``data/schema.sql``). It inherits the connection,
dedupe, booking, handoff and outbox plumbing from ``app.db.Database``.
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
    MANAGE_SELECT = "manage_select"
    MANAGE_ACTION = "manage_action"
    MANAGE_CANCEL_CONFIRM = "manage_cancel_confirm"
    HUMAN = "human"


ALL_STATES: frozenset[str] = frozenset(
    {
        State.IDLE,
        State.BOOKING_SERVICE,
        State.BOOKING_DATE,
        State.BOOKING_TIME,
        State.BOOKING_NAME,
        State.BOOKING_CONFIRM,
        State.MANAGE_SELECT,
        State.MANAGE_ACTION,
        State.MANAGE_CANCEL_CONFIRM,
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

# Steps of the "my appointments" (cancel / reschedule) sub-flow.
MANAGE_STEPS: tuple[str, ...] = (
    State.MANAGE_SELECT,
    State.MANAGE_ACTION,
    State.MANAGE_CANCEL_CONFIRM,
)

# States in which the bot is waiting for the customer to finish something.
IN_FLOW_STATES: frozenset[str] = frozenset(BOOKING_STEPS + MANAGE_STEPS)

# Flows that can be (re)started from any non-human state: "book" from the menu,
# or "my appointments". Everything else must follow TRANSITIONS.
ENTRY_STATES: frozenset[str] = frozenset(
    {State.BOOKING_SERVICE, State.MANAGE_SELECT, State.MANAGE_ACTION}
)

# Legal transitions. Every state may return to idle (cancel/reset) or escalate
# to a human; those targets are added implicitly by ``can_transition``.
TRANSITIONS: dict[str, set[str]] = {
    State.IDLE: set(),
    State.BOOKING_SERVICE: {State.BOOKING_DATE},
    State.BOOKING_DATE: {State.BOOKING_TIME},
    # BOOKING_DATE: the chosen day has no free slot left.
    # BOOKING_CONFIRM: rescheduling skips the name (we already know it).
    State.BOOKING_TIME: {State.BOOKING_NAME, State.BOOKING_DATE, State.BOOKING_CONFIRM},
    State.BOOKING_NAME: {State.BOOKING_CONFIRM},
    # BOOKING_TIME: the slot was taken while confirming; BOOKING_DATE: "change"
    # while rescheduling, or the slot is now in the past.
    State.BOOKING_CONFIRM: {State.BOOKING_TIME, State.BOOKING_DATE},
    State.MANAGE_SELECT: {State.MANAGE_ACTION},
    State.MANAGE_ACTION: {State.MANAGE_CANCEL_CONFIRM, State.BOOKING_DATE},
    State.MANAGE_CANCEL_CONFIRM: set(),
    State.HUMAN: set(),
}

# Which interactive payload kinds ("kind:value") each state accepts. ``None``
# means "any state except human". A payload arriving in any other state is a
# stale or forged button and is rejected with "option_expired".
PAYLOAD_STATES: dict[str, Optional[frozenset[str]]] = {
    "menu": None,
    "svc": frozenset({State.BOOKING_SERVICE}),
    "date": frozenset({State.BOOKING_DATE}),
    "time": frozenset({State.BOOKING_TIME}),
    "times_page": frozenset({State.BOOKING_TIME}),
    "name": frozenset({State.BOOKING_NAME}),
    "confirm": frozenset({State.BOOKING_CONFIRM}),
    "appt": frozenset({State.MANAGE_SELECT}),
    "appt_cancel": frozenset({State.MANAGE_ACTION}),
    "appt_resched": frozenset({State.MANAGE_ACTION}),
    "appt_cancel_yes": frozenset({State.MANAGE_CANCEL_CONFIRM}),
    "appt_keep": frozenset({State.MANAGE_ACTION, State.MANAGE_CANCEL_CONFIRM}),
}


def can_transition(current: str, target: str) -> bool:
    """True if moving current -> target is allowed by the machine."""
    if target not in ALL_STATES or current not in ALL_STATES:
        return False
    # Cancel/reset to idle and escalation to a human are always permitted.
    if target in (State.IDLE, State.HUMAN):
        return True
    if current == target:
        return True  # re-prompting the same step
    if target in ENTRY_STATES and current != State.HUMAN:
        return True
    return target in TRANSITIONS.get(current, set())


def payload_allowed(kind: str, state: str) -> bool:
    """True if an interactive reply of this kind belongs to ``state``."""
    if kind not in PAYLOAD_STATES or state == State.HUMAN:
        return False
    allowed = PAYLOAD_STATES[kind]
    return allowed is None or state in allowed


class IllegalTransition(RuntimeError):
    pass


@dataclass
class Conversation:
    wa_id: str
    state: str
    lang: str
    draft: dict
    handoff: bool
    profile_name: Optional[str]
    opted_out: bool = False
    last_user_at: Optional[str] = None


class Store(Database):
    """SQLite-backed conversation store: state, draft, language and history."""

    # ---------------------------------------------------------- conversations
    def ensure_conversation(self, wa_id: str, profile_name: Optional[str] = None) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO conversations (wa_id, profile_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (wa_id, profile_name, self._ts(), self._ts()),
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
                "SELECT wa_id, state, lang, draft, handoff, profile_name, opted_out, last_user_at "
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
            opted_out=bool(row["opted_out"]),
            last_user_at=row["last_user_at"],
        )

    def conversation_exists(self, wa_id: str) -> bool:
        with self._connect() as con:
            row = con.execute("SELECT 1 FROM conversations WHERE wa_id = ?", (wa_id,)).fetchone()
        return row is not None

    def _update(self, wa_id: str, column: str, value) -> None:
        with self._connect() as con:
            con.execute(
                f"UPDATE conversations SET {column} = ?, updated_at = ? WHERE wa_id = ?",
                (value, self._ts(), wa_id),
            )

    def set_state(self, wa_id: str, state: str) -> None:
        self._update(wa_id, "state", state)

    def set_lang(self, wa_id: str, lang: str) -> None:
        self._update(wa_id, "lang", lang)

    def set_handoff(self, wa_id: str, value: bool) -> None:
        self._update(wa_id, "handoff", 1 if value else 0)

    def set_opted_out(self, wa_id: str, value: bool) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE conversations SET opted_out = ?, opted_out_at = ?, updated_at = ? "
                "WHERE wa_id = ?",
                (1 if value else 0, self._ts() if value else None, self._ts(), wa_id),
            )

    def touch_user(self, wa_id: str, when: Optional[str] = None) -> None:
        """Record the customer's latest inbound message (opens the 24h window)."""
        self._update(wa_id, "last_user_at", when or self._ts())

    def update_draft(self, wa_id: str, patch: dict) -> dict:
        with self._connect() as con:
            row = con.execute(
                "SELECT draft FROM conversations WHERE wa_id = ?", (wa_id,)
            ).fetchone()
            draft = json.loads(row["draft"] or "{}") if row else {}
            for key, value in patch.items():
                if value is None:
                    draft.pop(key, None)
                else:
                    draft[key] = value
            con.execute(
                "UPDATE conversations SET draft = ?, updated_at = ? WHERE wa_id = ?",
                (json.dumps(draft, ensure_ascii=False), self._ts(), wa_id),
            )
            return draft

    def clear_draft(self, wa_id: str) -> None:
        self._update(wa_id, "draft", "{}")

    # -------------------------------------------------------------- messages
    def add_message(self, wa_id: str, role: str, content: str, author: Optional[str] = None) -> None:
        author = author or ("customer" if role == "user" else "bot")
        with self._connect() as con:
            con.execute(
                "INSERT INTO messages (wa_id, role, content, author, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (wa_id, role, content, author, self._ts()),
            )

    def history(self, wa_id: str, limit: int = 10) -> list[dict]:
        """Last ``limit`` turns as LLM chat messages (role/content)."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT role, content FROM messages WHERE wa_id = ? ORDER BY id DESC LIMIT ?",
                (wa_id, limit),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def transcript(self, wa_id: str, limit: int = 50) -> list[dict]:
        """Last ``limit`` messages with author and timestamp (staff console)."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, author, role, content, created_at FROM messages "
                "WHERE wa_id = ? ORDER BY id DESC LIMIT ?",
                (wa_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
