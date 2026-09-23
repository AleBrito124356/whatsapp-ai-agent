"""Dry-run helpers: read what the bot *would* have sent.

Mounted by ``create_app`` only when the app runs in dry-run mode (no real
WhatsApp credentials, or ``WHATSAPP_DRY_RUN=1``). ``scripts/send_webhook.py``
polls this endpoint to print the bot's actual replies after each webhook.
When ``ADMIN_TOKEN`` is set, the endpoint requires it as a bearer token.
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from .deps import Services
from .wa_payloads import render
from .webhook import get_services

router = APIRouter(prefix="/dev", tags=["dev"])


@router.get("/outbox")
def outbox(
    wa_id: Optional[str] = None,
    after: int = Query(0, ge=0, description="Only messages with id > after"),
    for_message: Optional[str] = Query(
        None, description="An inbound message id; the response says if it is fully handled"
    ),
    limit: int = Query(100, ge=1, le=500),
    authorization: Optional[str] = Header(default=None),
    services: Services = Depends(get_services),
) -> dict:
    settings = services.settings
    if not settings.dry_run:
        raise HTTPException(status_code=404, detail="Outbox exists only in dry-run mode")
    # The outbox holds customer conversations. On a machine you only reach
    # locally that is fine; if ADMIN_TOKEN is set (e.g. a shared staging
    # server), the outbox requires it too.
    if settings.admin_enabled:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), settings.admin_token.encode()):
            raise HTTPException(status_code=401, detail="Bearer ADMIN_TOKEN required", headers={"WWW-Authenticate": "Bearer"})
    rows = services.store.outbox_after(after=after, wa_id=wa_id, limit=limit)
    messages = []
    for row in rows:
        view = render(row["payload"]).to_dict()
        view.update({"id": row["id"], "created_at": row["created_at"], "payload": row["payload"]})
        messages.append(view)
    return {
        "dry_run": True,
        "cursor": services.store.outbox_cursor(),
        "handled": services.store.is_handled(for_message) if for_message else None,
        "messages": messages,
    }
