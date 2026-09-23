"""Service wiring.

``build_services`` assembles the store, knowledge base, LLM client, WhatsApp
client and agent from a ``Settings`` object. Nothing is built at import time:
the FastAPI app creates its services on startup (or on the first request), and
tests or the chat simulator build their own with a temp database, a fake
WhatsApp client and a frozen clock. Importing ``app.webhook`` or ``app.main``
therefore never creates ``data/bookings.sqlite`` as a side effect.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from .agent import Agent
from .clock import Clock, system_clock
from .config import Settings, get_settings
from .kb import KnowledgeBase
from .llm import LLMClient
from .state import Store
from .wa_client import DryRunTransport, Transport, WhatsAppClient

log = logging.getLogger("whatsapp_agent.deps")


@dataclass
class Services:
    settings: Settings
    store: Store
    kb: KnowledgeBase
    llm: LLMClient
    wa: WhatsAppClient
    agent: Agent
    clock: Clock


def build_services(
    settings: Optional[Settings] = None,
    *,
    db_path: Optional[Path] = None,
    clock: Optional[Clock] = None,
    llm=None,
    wa=None,
    transport: Optional[Transport] = None,
    http_client: Optional[httpx.Client] = None,
) -> Services:
    """Wire up the app. Every collaborator can be swapped for a fake."""
    settings = settings or get_settings()
    clock = clock or system_clock
    store = Store(db_path or settings.db_path, settings.schema_path, clock=clock)
    kb = KnowledgeBase(settings.kb_dir)
    llm = llm if llm is not None else LLMClient(settings)
    if wa is None:
        if transport is None and settings.dry_run:
            transport = DryRunTransport(store)
        wa = WhatsAppClient(settings, transport=transport, http_client=http_client)
    agent = Agent(settings=settings, wa=wa, store=store, kb=kb, llm=llm, clock=clock)
    return Services(settings=settings, store=store, kb=kb, llm=llm, wa=wa, agent=agent, clock=clock)


# --------------------------------------------------------------- default app
_default: Optional[Services] = None
_lock = threading.Lock()


def default_services() -> Services:
    """Process-wide services built from the environment, created on first use."""
    global _default
    if _default is None:
        with _lock:
            if _default is None:
                _default = build_services(get_settings())
    return _default


def __getattr__(name: str):
    # Backwards compatibility with the old module-level singletons
    # (``from app.deps import agent, store``), now built lazily.
    if name in {"settings", "store", "kb", "llm", "wa", "agent"}:
        return getattr(default_services(), name)
    raise AttributeError(name)
