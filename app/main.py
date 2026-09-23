"""FastAPI application entrypoint.

Run locally:
    uvicorn app.main:app --reload --port 8000

Then expose it (e.g. `ngrok http 8000`) and point your Meta webhook at
https://<public-host>/webhook. See docs/meta-setup.md.

Without real WhatsApp credentials the app runs in dry-run mode: replies are
stored in a local outbox (GET /dev/outbox) instead of being sent to Meta.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from . import __version__
from .config import Settings, get_settings
from .deps import Services, build_services

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("whatsapp_agent.main")


def create_app(services: Optional[Services] = None, settings: Optional[Settings] = None) -> FastAPI:
    """Build the FastAPI app.

    Pass ``services`` to run against a specific store / WhatsApp client / clock
    (tests do). Otherwise services are built from the environment on startup,
    or on the first request, whichever comes first.
    """
    settings = services.settings if services is not None else (settings or get_settings())
    holder: dict = {"services": services}
    lock = threading.Lock()

    def get_services() -> Services:
        if holder["services"] is None:
            with lock:
                if holder["services"] is None:
                    holder["services"] = build_services(settings)
        return holder["services"]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = get_services()
        _log_startup(svc.settings)
        yield

    app = FastAPI(
        title="WhatsApp AI Agent",
        version=__version__,
        description="Bilingual WhatsApp Business assistant on the Meta Cloud API.",
        lifespan=lifespan,
    )
    app.state.get_services = get_services

    from .admin import router as admin_router
    from .webhook import router as webhook_router

    app.include_router(webhook_router)
    app.include_router(admin_router)

    if settings.dry_run:
        from .dev import router as dev_router

        app.include_router(dev_router)

    @app.get("/health")
    async def health() -> dict:
        s = settings
        return {
            "status": "ok",
            "business": s.business_name,
            "llm": "nim" if s.llm_configured else "offline",
            "whatsapp": "dry_run" if s.dry_run else "live",
            "dry_run": s.dry_run,
            "signature_verification": s.signature_enforced,
            "admin_api": s.admin_enabled,
        }

    @app.get("/")
    async def root() -> dict:
        links = {
            "name": "whatsapp-ai-agent",
            "webhook": "/webhook",
            "health": "/health",
            "admin": "/admin/handoffs",
            "docs": "/docs",
        }
        if settings.dry_run:
            links["outbox"] = "/dev/outbox"
        return links

    return app


def _log_startup(settings: Settings) -> None:
    if settings.dry_run:
        log.warning(
            "WhatsApp DRY-RUN: no real credentials (or WHATSAPP_DRY_RUN=1). Replies are "
            "stored in the outbox (GET /dev/outbox), nothing is sent to graph.facebook.com."
        )
    if not settings.signature_enforced:
        log.warning("WHATSAPP_APP_SECRET is empty: webhook signatures are NOT verified.")
    if not settings.admin_enabled:
        log.info("Staff API (/admin) disabled: set ADMIN_TOKEN to enable it.")
    elif len(settings.admin_token) < 16:
        log.warning("ADMIN_TOKEN is shorter than 16 characters; use a long random value.")


app = create_app()
