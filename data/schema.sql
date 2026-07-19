-- Schema for the WhatsApp AI agent.
-- Applied automatically on startup (see app/db.py -> Store._init_schema).
-- The .sqlite file itself is generated and git-ignored; this file is the
-- source of truth for its structure.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Idempotency: WhatsApp retries webhooks, so we record every message id we have
-- already accepted and drop duplicates before they reach the agent.
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id   TEXT PRIMARY KEY,
    wa_id        TEXT NOT NULL,
    received_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per WhatsApp contact: their place in the state machine, language,
-- the in-progress booking draft (JSON), and whether they are queued for a human.
CREATE TABLE IF NOT EXISTS conversations (
    wa_id       TEXT PRIMARY KEY,
    state       TEXT NOT NULL DEFAULT 'idle',
    lang        TEXT NOT NULL DEFAULT 'es',
    draft       TEXT NOT NULL DEFAULT '{}',
    handoff     INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Full conversation transcript, used for context windows and auditing.
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id      TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_wa_id ON messages (wa_id, id);

-- Confirmed appointments. `date` is ISO (YYYY-MM-DD), `time` is HH:MM (24h).
CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id         TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    service_id    TEXT NOT NULL,
    service_label TEXT NOT NULL,
    date          TEXT NOT NULL,
    time          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'confirmed',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bookings_slot ON bookings (date, time, status);

-- Escalation queue. A human agent works this table; `resolved` flips to 1 when
-- someone has picked the conversation up.
CREATE TABLE IF NOT EXISTS handoffs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id      TEXT NOT NULL,
    reason     TEXT,
    resolved   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_handoffs_open ON handoffs (resolved, created_at);
