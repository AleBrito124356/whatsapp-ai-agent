"""Shared test fixtures.

Everything runs offline: a temp SQLite database, the real knowledge base, a
fake WhatsApp client that records outbound messages, and a stub LLM so intent
routing exercises the keyword fallback path deterministically.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import Agent
from app.config import get_settings
from app.kb import KnowledgeBase
from app.state import Store

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeWhatsApp:
    """Records outbound calls instead of hitting the Graph API."""

    def __init__(self):
        self.sent: list[dict] = []

    def mark_read(self, message_id, typing=True):
        self.sent.append({"kind": "mark_read", "message_id": message_id})

    def send_text(self, to, body, preview_url=False):
        self.sent.append({"kind": "text", "to": to, "body": body})

    def send_image(self, to, link, caption=""):
        self.sent.append({"kind": "image", "to": to, "link": link, "caption": caption})

    def send_buttons(self, to, body, buttons, header=""):
        self.sent.append({"kind": "buttons", "to": to, "body": body, "buttons": buttons})

    def send_list(self, to, body, button_text, sections, header="", footer=""):
        self.sent.append({"kind": "list", "to": to, "body": body, "sections": sections})

    def download_media(self, media_id):
        return None

    # convenience helpers for assertions
    def kinds(self):
        return [m["kind"] for m in self.sent if m["kind"] != "mark_read"]

    def last(self, kind):
        for msg in reversed(self.sent):
            if msg["kind"] == kind:
                return msg
        return None


class OfflineLLM:
    available = False

    def chat(self, messages, temperature=0.3, max_tokens=600):
        return None


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test.sqlite"
    return Store(db, REPO_ROOT / "data" / "schema.sql")


@pytest.fixture
def kb():
    return KnowledgeBase(REPO_ROOT / "kb")


@pytest.fixture
def wa():
    return FakeWhatsApp()


@pytest.fixture
def agent(store, kb, wa):
    return Agent(settings=get_settings(), wa=wa, store=store, kb=kb, llm=OfflineLLM())
