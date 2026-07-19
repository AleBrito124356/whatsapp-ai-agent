"""FastAPI application entrypoint.

Run locally:
    uvicorn app.main:app --reload --port 8000

Then expose it (e.g. `ngrok http 8000`) and point your Meta webhook at
https://<public-host>/webhook. See docs/meta-setup.md.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .config import get_settings
from .webhook import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

settings = get_settings()

app = FastAPI(
    title="WhatsApp AI Agent",
    version="1.0.0",
    description="Bilingual WhatsApp Business assistant on the Meta Cloud API.",
)

app.include_router(router)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "business": settings.business_name,
        "llm": "nim" if settings.llm_configured else "offline",
        "signature_verification": settings.signature_enforced,
    }


@app.get("/")
async def root() -> dict:
    return {
        "name": "whatsapp-ai-agent",
        "webhook": "/webhook",
        "health": "/health",
        "docs": "/docs",
    }
