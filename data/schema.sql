-- Schema for the WhatsApp AI agent.
-- Applied automatically on startup (see app/db.py -> Database._init_schema).
-- The .sqlite file itself is generated and git-ignored; this file is the
-- source of truth for its structure. Databases created by older versions are
-- upgraded in place by Database._migrate (missing columns are added before
-- this script runs, so the indexes below always find their columns).
--
-- Timestamps are UTC text 'YYYY-MM-DD HH:MM:SS' (SQLite's datetime('now')
-- format). The app writes them from its injectable clock.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Idempotency: WhatsApp retries webhooks, so we record every message id we have
-- already accepted and drop duplicates before they reach the agent.
-- handled_at is set once the agent has finished replying (used by the dry-run
-- tester to know when the bot's answer is complete).
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id   TEXT PRIMARY KEY,
    wa_id        TEXT NOT NULL,
    received_at  TEXT NOT NULL DEFAULT (datetime('now')),
    handled_at   TEXT
);

-- One row per WhatsApp contact: their place in the state machine, language,
-- the in-progress draft (JSON), whether a human has taken over, whether they
-- opted out (BAJA/STOP), and when they last wrote to us (24-hour window).
CREATE TABLE IF NOT EXISTS conversations (
    wa_id        TEXT PRIMARY KEY,
    state        TEXT NOT NULL DEFAULT 'idle',
    lang         TEXT NOT NULL DEFAULT 'es',
    draft        TEXT NOT NULL DEFAULT '{}',
    handoff      INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    opted_out    INTEGER NOT NULL DEFAULT 0,
    opted_out_at TEXT,
    last_user_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Full conversation transcript, used for LLM context and the staff console.
-- role is the LLM-facing role; author says who actually wrote it
-- ('customer', 'bot' or 'staff').
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id      TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT 'bot',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_wa_id ON messages (wa_id, id);

-- Appointments. `date` is ISO (YYYY-MM-DD), `time` is HH:MM (24h).
-- status: confirmed | cancelled | conflict (a legacy double booking found
-- during migration; kept for staff to sort out).
CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id         TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    service_id    TEXT NOT NULL,
    service_label TEXT NOT NULL,
    date          TEXT NOT NULL,
    time          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'confirmed',
    cancelled_by  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_bookings_slot ON bookings (date, time, status);
CREATE INDEX IF NOT EXISTS idx_bookings_contact ON bookings (wa_id, status, date);
-- One confirmed appointment per slot. This is what makes booking race-safe:
-- two customers confirming the same slot at once cannot both succeed.
CREATE UNIQUE INDEX IF NOT EXISTS ux_bookings_confirmed_slot
    ON bookings (date, time) WHERE status = 'confirmed';

-- Escalation queue worked by staff through /admin (see app/admin.py).
-- resolved flips to 1 when staff hand the chat back to the bot, or when the
-- customer resets the conversation themselves.
CREATE TABLE IF NOT EXISTS handoffs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id       TEXT NOT NULL,
    reason      TEXT,
    resolved    INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT,
    resolved_by TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_handoffs_open ON handoffs (resolved, created_at);

-- Dry-run outbox: exact Graph API payloads the bot would have sent, stored
-- instead of calling graph.facebook.com (see app/wa_client.py).
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id      TEXT NOT NULL,
    msg_type   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_outbox_wa_id ON outbox (wa_id, id);

PRAGMA user_version = 2;
