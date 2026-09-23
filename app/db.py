"""Low-level SQLite plumbing shared by the persistence layer.

``Database`` owns the connection lifecycle, the schema and its migrations,
plus the tables that are not part of the conversation state machine: webhook
dedupe, bookings, the human-handoff queue and the dry-run outbox. The
per-contact state machine and message history live in ``app/state.py`` (the
``Store`` subclass).

Each call opens its own short-lived connection so the store is safe to use from
FastAPI background tasks, which run on a thread pool. WAL mode keeps concurrent
readers/writers happy. Timestamps come from an injectable clock so tests and
the chat simulator can freeze time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .clock import Clock, system_clock, to_db

log = logging.getLogger("whatsapp_agent.db")

SCHEMA_VERSION = 2

# Columns added after the first release. Old databases get them through
# ALTER TABLE before schema.sql runs (its indexes reference some of them).
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("processed_messages", "handled_at", "handled_at TEXT"),
    ("conversations", "opted_out", "opted_out INTEGER NOT NULL DEFAULT 0"),
    ("conversations", "opted_out_at", "opted_out_at TEXT"),
    ("conversations", "last_user_at", "last_user_at TEXT"),
    ("messages", "author", "author TEXT NOT NULL DEFAULT 'bot'"),
    ("bookings", "cancelled_by", "cancelled_by TEXT"),
    ("bookings", "updated_at", "updated_at TEXT"),
    ("handoffs", "resolved_at", "resolved_at TEXT"),
    ("handoffs", "resolved_by", "resolved_by TEXT"),
]


class SlotTaken(Exception):
    """Someone else already holds a confirmed booking for this date/time."""

    def __init__(self, date: str, time: str):
        super().__init__(f"slot {date} {time} is already booked")
        self.date = date
        self.time = time


class Database:
    def __init__(self, db_path: Path, schema_path: Path, clock: Optional[Clock] = None):
        self.db_path = Path(db_path)
        self.schema_path = Path(schema_path)
        self.clock: Clock = clock or system_clock
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ setup
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        # journal_mode=WAL is persistent (set once by schema.sql); the busy
        # timeout is per connection.
        con.execute("PRAGMA busy_timeout = 5000;")
        try:
            yield con
        finally:
            con.close()

    def _ts(self) -> str:
        return to_db(self.clock())

    def _init_schema(self) -> None:
        schema = self.schema_path.read_text(encoding="utf-8")
        with self._connect() as con:
            tables = {
                r["name"]
                for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if tables:
                self._migrate(con, tables)
            con.executescript(schema)

    def _migrate(self, con: sqlite3.Connection, tables: set[str]) -> None:
        """Bring a database created by an older version up to date, in place."""
        for table, column, ddl in _ADDED_COLUMNS:
            if table not in tables:
                continue
            columns = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if column in columns:
                continue
            con.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            log.info("Migrated: added %s.%s", table, column)
            if (table, column) == ("messages", "author"):
                con.execute("UPDATE messages SET author = 'customer' WHERE role = 'user'")

        if "bookings" in tables:
            # The unique slot index cannot be created while legacy double
            # bookings exist. Keep the earliest one confirmed and flag the rest
            # as 'conflict' so staff can see them in /admin/bookings.
            cur = con.execute(
                "UPDATE bookings SET status = 'conflict' "
                "WHERE status = 'confirmed' AND id NOT IN ("
                "  SELECT MIN(id) FROM bookings WHERE status = 'confirmed' GROUP BY date, time)"
            )
            if cur.rowcount:
                log.warning(
                    "Migration found %d double-booked slot(s); marked them status='conflict'.",
                    cur.rowcount,
                )

    # ----------------------------------------------------------- idempotency
    def is_duplicate(self, message_id: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            return row is not None

    def mark_processed(self, message_id: str, wa_id: str) -> bool:
        """Record a message id atomically. Returns False if already present."""
        with self._connect() as con:
            try:
                con.execute(
                    "INSERT INTO processed_messages (message_id, wa_id, received_at) "
                    "VALUES (?, ?, ?)",
                    (message_id, wa_id, self._ts()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_handled(self, message_id: str) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE processed_messages SET handled_at = ? WHERE message_id = ?",
                (self._ts(), message_id),
            )

    def is_handled(self, message_id: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                "SELECT handled_at FROM processed_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return bool(row and row["handled_at"])

    # -------------------------------------------------------------- bookings
    def booked_times(self, date: str) -> set[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT time FROM bookings WHERE date = ? AND status = 'confirmed'",
                (date,),
            ).fetchall()
        return {r["time"] for r in rows}

    def booked_times_between(self, first: str, last: str) -> dict[str, set[str]]:
        """{date: {times}} of confirmed bookings for first <= date <= last."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT date, time FROM bookings WHERE date BETWEEN ? AND ? AND status = 'confirmed'",
                (first, last),
            ).fetchall()
        out: dict[str, set[str]] = {}
        for r in rows:
            out.setdefault(r["date"], set()).add(r["time"])
        return out

    def create_booking(
        self,
        wa_id: str,
        customer_name: str,
        service_id: str,
        service_label: str,
        date: str,
        time: str,
    ) -> int:
        """Insert a confirmed booking. Raises SlotTaken if the slot is held.

        The partial UNIQUE index on (date, time) WHERE status='confirmed' makes
        this safe under concurrency: the INSERT is a single atomic statement,
        so of two simultaneous confirmations exactly one wins.
        """
        try:
            with self._connect() as con:
                cur = con.execute(
                    "INSERT INTO bookings "
                    "(wa_id, customer_name, service_id, service_label, date, time, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (wa_id, customer_name, service_id, service_label, date, time, self._ts()),
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                raise SlotTaken(date, time) from exc
            raise

    def get_booking(self, booking_id: int) -> Optional[dict]:
        with self._connect() as con:
            row = con.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return dict(row) if row else None

    def upcoming_bookings(self, wa_id: str, today: str, now_hhmm: str, limit: int = 10) -> list[dict]:
        """Confirmed bookings for a contact that have not started yet."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM bookings WHERE wa_id = ? AND status = 'confirmed' "
                "AND (date > ? OR (date = ? AND time > ?)) "
                "ORDER BY date, time LIMIT ?",
                (wa_id, today, today, now_hhmm, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_bookings(
        self,
        date: Optional[str] = None,
        from_date: Optional[str] = None,
        status: Optional[str] = "confirmed",
        limit: int = 200,
    ) -> list[dict]:
        clauses, params = [], []
        if date:
            clauses.append("date = ?")
            params.append(date)
        if from_date:
            clauses.append("date >= ?")
            params.append(from_date)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM bookings {where} ORDER BY date, time, id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def cancel_booking(self, booking_id: int, by: str, wa_id: Optional[str] = None) -> bool:
        """Cancel a confirmed booking. Returns False if it was not cancellable."""
        sql = (
            "UPDATE bookings SET status = 'cancelled', cancelled_by = ?, updated_at = ? "
            "WHERE id = ? AND status = 'confirmed'"
        )
        params: list = [by, self._ts(), booking_id]
        if wa_id is not None:
            sql += " AND wa_id = ?"
            params.append(wa_id)
        with self._connect() as con:
            return con.execute(sql, params).rowcount == 1

    def reschedule_booking(self, booking_id: int, wa_id: str, date: str, time: str) -> bool:
        """Move a confirmed booking to a new slot in one atomic UPDATE.

        Returns False if the booking is not the contact's or is no longer
        confirmed; raises SlotTaken if the new slot is held by someone else.
        """
        try:
            with self._connect() as con:
                cur = con.execute(
                    "UPDATE bookings SET date = ?, time = ?, updated_at = ? "
                    "WHERE id = ? AND wa_id = ? AND status = 'confirmed'",
                    (date, time, self._ts(), booking_id, wa_id),
                )
                return cur.rowcount == 1
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                raise SlotTaken(date, time) from exc
            raise

    # -------------------------------------------------------------- handoffs
    def create_handoff(self, wa_id: str, reason: str) -> bool:
        """Queue a contact for a human. No-op (False) if already queued."""
        with self._connect() as con:
            open_row = con.execute(
                "SELECT 1 FROM handoffs WHERE wa_id = ? AND resolved = 0", (wa_id,)
            ).fetchone()
            if open_row:
                return False
            con.execute(
                "INSERT INTO handoffs (wa_id, reason, created_at) VALUES (?, ?, ?)",
                (wa_id, reason, self._ts()),
            )
            return True

    def open_handoffs(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT h.id, h.wa_id, h.reason, h.created_at, c.profile_name, c.lang, "
                "c.state, c.last_user_at, c.opted_out "
                "FROM handoffs h LEFT JOIN conversations c ON c.wa_id = h.wa_id "
                "WHERE h.resolved = 0 ORDER BY h.created_at, h.id"
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_handoffs(self, wa_id: str, by: str) -> int:
        with self._connect() as con:
            return con.execute(
                "UPDATE handoffs SET resolved = 1, resolved_at = ?, resolved_by = ? "
                "WHERE wa_id = ? AND resolved = 0",
                (self._ts(), by, wa_id),
            ).rowcount

    # ---------------------------------------------------------------- outbox
    def add_outbox(self, wa_id: str, payload: dict) -> int:
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO outbox (wa_id, msg_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (
                    wa_id,
                    str(payload.get("type", "unknown")),
                    json.dumps(payload, ensure_ascii=False),
                    self._ts(),
                ),
            )
            return int(cur.lastrowid)

    def outbox_after(self, after: int = 0, wa_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM outbox WHERE id > ?"
        params: list = [after]
        if wa_id:
            sql += " AND wa_id = ?"
            params.append(wa_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out

    def outbox_cursor(self) -> int:
        with self._connect() as con:
            row = con.execute("SELECT COALESCE(MAX(id), 0) AS m FROM outbox").fetchone()
        return int(row["m"])
