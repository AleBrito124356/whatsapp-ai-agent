"""Low-level SQLite plumbing shared by the persistence layer.

``Database`` owns the connection lifecycle and schema, plus the tables that are
not part of the conversation state machine: webhook dedupe, confirmed bookings,
and the human-handoff queue. The per-contact state machine and message history
live in ``app/state.py`` (the ``Store`` subclass).

Each call opens its own short-lived connection so the store is safe to use from
FastAPI background tasks, which run on a thread pool. WAL mode keeps concurrent
readers/writers happy.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Database:
    def __init__(self, db_path: Path, schema_path: Path):
        self.db_path = Path(db_path)
        self.schema_path = Path(schema_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ setup
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode = WAL;")
        con.execute("PRAGMA busy_timeout = 5000;")
        try:
            yield con
        finally:
            con.close()

    def _init_schema(self) -> None:
        schema = self.schema_path.read_text(encoding="utf-8")
        with self._connect() as con:
            con.executescript(schema)

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
                    "INSERT INTO processed_messages (message_id, wa_id) VALUES (?, ?)",
                    (message_id, wa_id),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    # -------------------------------------------------------------- bookings
    def booked_times(self, date: str) -> set[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT time FROM bookings WHERE date = ? AND status = 'confirmed'",
                (date,),
            ).fetchall()
        return {r["time"] for r in rows}

    def create_booking(
        self,
        wa_id: str,
        customer_name: str,
        service_id: str,
        service_label: str,
        date: str,
        time: str,
    ) -> int:
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO bookings "
                "(wa_id, customer_name, service_id, service_label, date, time) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (wa_id, customer_name, service_id, service_label, date, time),
            )
            return int(cur.lastrowid)

    # -------------------------------------------------------------- handoffs
    def create_handoff(self, wa_id: str, reason: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO handoffs (wa_id, reason) VALUES (?, ?)",
                (wa_id, reason),
            )
