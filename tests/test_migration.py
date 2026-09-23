"""Databases created by the first release are upgraded in place."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import SlotTaken
from app.state import Store

from .conftest import REPO_ROOT


def _legacy_db(path):
    con = sqlite3.connect(path)
    con.executescript((REPO_ROOT / "tests" / "data" / "schema_v1.sql").read_text(encoding="utf-8"))
    con.executemany(
        "INSERT INTO bookings (wa_id, customer_name, service_id, service_label, date, time) VALUES (?,?,?,?,?,?)",
        [
            ("1", "Ana", "svc_corte", "Corte", "2026-09-24", "10:00"),
            ("2", "Luis", "svc_corte", "Corte", "2026-09-24", "10:00"),  # the old double booking bug
            ("3", "Eva", "svc_barba", "Barba", "2026-09-24", "11:00"),
        ],
    )
    con.execute("INSERT INTO conversations (wa_id, handoff) VALUES ('1', 1)")
    con.execute("INSERT INTO messages (wa_id, role, content) VALUES ('1', 'user', 'hola')")
    con.execute("INSERT INTO messages (wa_id, role, content) VALUES ('1', 'assistant', 'menu')")
    con.execute("INSERT INTO handoffs (wa_id, reason) VALUES ('1', 'requested')")
    con.commit()
    con.close()


def test_legacy_database_is_migrated(tmp_path, clock):
    path = tmp_path / "legacy.sqlite"
    _legacy_db(path)

    store = Store(path, REPO_ROOT / "data" / "schema.sql", clock=clock)

    rows = {r["customer_name"]: r["status"] for r in store.list_bookings(status=None)}
    assert rows == {"Ana": "confirmed", "Luis": "conflict", "Eva": "confirmed"}

    # New columns exist and old data was backfilled sensibly.
    conv = store.get_conversation("1")
    assert conv.handoff is True and conv.opted_out is False and conv.last_user_at is None
    assert [m["author"] for m in store.transcript("1")] == ["customer", "bot"]
    assert store.open_handoffs()[0]["wa_id"] == "1"
    assert store.outbox_cursor() == 0

    # The unique slot index is now enforced.
    with pytest.raises(SlotTaken):
        store.create_booking("9", "Zoe", "svc_corte", "Corte", "2026-09-24", "11:00")

    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2

    # Opening it again is a no-op.
    Store(path, REPO_ROOT / "data" / "schema.sql", clock=clock)


def test_fresh_database_enforces_one_confirmed_booking_per_slot(store):
    first = store.create_booking("1", "Ana", "svc_corte", "Corte", "2026-09-24", "10:00")
    with pytest.raises(SlotTaken):
        store.create_booking("2", "Luis", "svc_corte", "Corte", "2026-09-24", "10:00")
    # A cancelled booking frees the slot.
    assert store.cancel_booking(first, by="customer", wa_id="1")
    store.create_booking("2", "Luis", "svc_corte", "Corte", "2026-09-24", "10:00")
    assert store.booked_times("2026-09-24") == {"10:00"}
