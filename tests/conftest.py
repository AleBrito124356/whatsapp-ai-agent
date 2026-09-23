"""Shared test fixtures.

Everything runs offline: a temp SQLite database, the real knowledge base, a
frozen clock, a fake WhatsApp client that records the *real* Graph payloads
(built by app.wa_payloads, so WhatsApp's size limits are exercised too), and a
stub LLM so intent routing exercises the keyword fallback path
deterministically. Settings are built explicitly, never from the developer's
environment or .env file.
"""

from __future__ import annotations

import itertools
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("America/Panama")
# Thursday 24 Sep 2026, 08:00 local: before opening, so every slot of the day is
# still bookable. Sat 26 is open, Sun 27 is closed, Wed 23 is in the past.
FROZEN_NOW = datetime(2026, 9, 24, 8, 0, tzinfo=TZ)


@pytest.fixture(autouse=True, scope="session")
def _hermetic_env(tmp_path_factory):
    """Belt and braces: nothing a test does may touch data/bookings.sqlite."""
    old = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("default-db") / "never.sqlite")
    yield
    if old is None:
        os.environ.pop("DB_PATH", None)
    else:
        os.environ["DB_PATH"] = old


from app.agent import Agent  # noqa: E402
from app.clock import FrozenClock  # noqa: E402
from app.config import Settings  # noqa: E402
from app.kb import KnowledgeBase  # noqa: E402
from app.models import InboundMessage  # noqa: E402
from app.state import Store  # noqa: E402
from app.wa_client import CallbackTransport, WhatsAppClient  # noqa: E402
from app.wa_payloads import render  # noqa: E402

TEST_SETTINGS = Settings(business_name="Barbería Studio Norte", timezone="America/Panama")


class FakeWhatsApp(WhatsAppClient):
    """Records every outbound payload instead of calling the Graph API."""

    def __init__(self, settings: Settings = TEST_SETTINGS):
        self.sent: list[dict] = []
        self.fail = False  # simulate Graph API errors
        super().__init__(settings, transport=CallbackTransport(self._record))

    def send_payload(self, payload: dict):
        if self.fail:
            return None
        return super().send_payload(payload)

    def _record(self, payload: dict) -> None:
        if payload.get("status") == "read":
            self.sent.append({"kind": "mark_read", "message_id": payload["message_id"]})
            return
        view = render(payload)
        entry = {"kind": view.kind, "to": view.to, "body": view.body, "payload": payload}
        if view.kind == "buttons":
            entry["buttons"] = [{"id": o.id, "title": o.title} for o in view.options]
        if view.kind == "list":
            entry["sections"] = payload["interactive"]["action"]["sections"]
            entry["rows"] = [{"id": o.id, "title": o.title, "description": o.description} for o in view.options]
        self.sent.append(entry)

    # convenience helpers for assertions
    def kinds(self) -> list[str]:
        return [m["kind"] for m in self.sent if m["kind"] != "mark_read"]

    def outbound(self) -> list[dict]:
        return [m for m in self.sent if m["kind"] != "mark_read"]

    def last(self, kind: str | None = None):
        for msg in reversed(self.sent):
            if msg["kind"] == "mark_read":
                continue
            if kind is None or msg["kind"] == kind:
                return msg
        return None

    def last_options(self) -> list[str]:
        """Payload ids offered by the most recent buttons/list message."""
        for msg in reversed(self.sent):
            if msg["kind"] == "buttons":
                return [b["id"] for b in msg["buttons"]]
            if msg["kind"] == "list":
                return [r["id"] for r in msg["rows"]]
        return []


class OfflineLLM:
    available = False

    def chat(self, messages, temperature=0.3, max_tokens=600):
        return None


class RecordingLLM:
    """A fake 'online' LLM: records the prompts and returns canned answers."""

    available = True

    def __init__(self, answer: str = "Respuesta del modelo.", intent: str = "info"):
        self.calls: list[list[dict]] = []
        self.answer = answer
        self.intent = intent

    def chat(self, messages, temperature=0.3, max_tokens=600):
        self.calls.append(messages)
        if max_tokens <= 8:  # the intent classifier asks for a single word
            return self.intent
        return self.answer


_ids = itertools.count(1)


def text_msg(wa_id: str, body: str, profile_name: str | None = None) -> InboundMessage:
    return InboundMessage(
        wa_id=wa_id, message_id=f"wamid.T{next(_ids)}", type="text", text=body, profile_name=profile_name
    )


def tap_msg(wa_id: str, payload_id: str, title: str = "") -> InboundMessage:
    return InboundMessage(
        wa_id=wa_id,
        message_id=f"wamid.T{next(_ids)}",
        type="interactive",
        interactive_id=payload_id,
        interactive_title=title or payload_id,
    )


@pytest.fixture
def clock():
    return FrozenClock(FROZEN_NOW)


@pytest.fixture
def settings():
    return TEST_SETTINGS


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "test.sqlite", REPO_ROOT / "data" / "schema.sql", clock=clock)


@pytest.fixture(scope="session")
def kb():
    return KnowledgeBase(REPO_ROOT / "kb")


@pytest.fixture
def wa():
    return FakeWhatsApp()


@pytest.fixture
def agent(store, kb, wa, clock):
    return Agent(settings=TEST_SETTINGS, wa=wa, store=store, kb=kb, llm=OfflineLLM(), clock=clock)
