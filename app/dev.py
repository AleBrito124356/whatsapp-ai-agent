"""Dry-run helpers: read what the bot *would* have sent.

Mounted by ``create_app`` only when the app runs in dry-run mode (no real
WhatsApp credentials, or ``WHATSAPP_DRY_RUN=1``). ``scripts/send_webhook.py``
polls this endpoint to print the bot's actual replies after each webhook.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

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
    services: Services = Depends(get_services),
) -> dict:
    if not services.settings.dry_run:
        raise HTTPException(status_code=404, detail="Outbox exists only in dry-run mode")
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
