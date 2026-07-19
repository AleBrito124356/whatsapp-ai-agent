"""Application singletons.

Built once at import time from the active settings and shared across requests.
Tests construct their own instances instead of importing these, so nothing here
touches the network or a real database unless configured.
"""

from __future__ import annotations

from .agent import Agent
from .config import get_settings
from .kb import KnowledgeBase
from .llm import LLMClient
from .state import Store
from .wa_client import WhatsAppClient

settings = get_settings()
store = Store(settings.db_path, settings.schema_path)
kb = KnowledgeBase(settings.kb_dir)
llm = LLMClient(settings)
wa = WhatsAppClient(settings)
agent = Agent(settings=settings, wa=wa, store=store, kb=kb, llm=llm)
