"""FastAPI webhook: GET verification and POST message receipt.

Meta calls GET once to verify the endpoint (echo back hub.challenge) and POSTs a
signed payload for every event thereafter. We verify the signature, drop
duplicate message ids, and hand real messages to the agent on a background task
so we can return 200 immediately (Meta retries slow responses).
"""

from __future__ import annotations

import json
import logging
from typing import Iterator

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .config import get_settings
from .deps import agent, store
from .models import InboundMessage
from .security import verify_signature

log = logging.getLogger("whatsapp_agent.webhook")

router = APIRouter()
settings = get_settings()


@router.get("/webhook", response_class=PlainTextResponse)
async def verify(request: Request) -> PlainTextResponse:
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == settings.verify_token:
        log.info("Webhook verified by Meta.")
        return PlainTextResponse(challenge or "")
    log.warning("Webhook verification failed (mode=%s).", mode)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook")
async def receive(
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    raw = await request.body()

    if settings.signature_enforced:
        if not verify_signature(settings.app_secret, raw, x_hub_signature_256):
            log.warning("Rejected webhook with invalid signature.")
            raise HTTPException(status_code=403, detail="Invalid signature")
    else:
        log.warning(
            "WHATSAPP_APP_SECRET not set; skipping signature verification. "
            "Do NOT run like this in production."
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed JSON")

    accepted = 0
    for msg in parse_messages(payload):
        # mark_processed is an atomic INSERT; False means we have seen this id.
        if not store.mark_processed(msg.message_id, msg.wa_id):
            log.info("Skipping duplicate message %s", msg.message_id)
            continue
        background.add_task(agent.handle_message, msg)
        accepted += 1

    return {"status": "received", "accepted": accepted}


def parse_messages(payload: dict) -> Iterator[InboundMessage]:
    """Flatten Meta's nested webhook payload into InboundMessage objects.

    Ignores status callbacks (delivered/read receipts) and anything that is not a
    user-sent message.
    """
    if payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if "messages" not in value:
                continue  # statuses, errors, etc.

            # Contact profile name (best effort).
            profile_name = None
            contacts = value.get("contacts") or []
            if contacts:
                profile_name = contacts[0].get("profile", {}).get("name")

            for message in value["messages"]:
                parsed = _parse_one(message, profile_name)
                if parsed is not None:
                    yield parsed


def _parse_one(message: dict, profile_name: str | None) -> InboundMessage | None:
    wa_id = message.get("from")
    message_id = message.get("id")
    msg_type = message.get("type")
    if not wa_id or not message_id:
        return None

    base = dict(
        wa_id=wa_id,
        message_id=message_id,
        type=msg_type or "unsupported",
        timestamp=message.get("timestamp"),
        profile_name=profile_name,
    )

    if msg_type == "text":
        base["text"] = message.get("text", {}).get("body", "")
    elif msg_type == "interactive":
        interactive = message.get("interactive", {})
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        base["interactive_id"] = reply.get("id")
        base["interactive_title"] = reply.get("title")
    elif msg_type == "image":
        image = message.get("image", {})
        base["media_id"] = image.get("id")
        base["media_mime"] = image.get("mime_type")
        base["caption"] = image.get("caption")
    elif msg_type == "audio":
        audio = message.get("audio", {})
        base["media_id"] = audio.get("id")
        base["media_mime"] = audio.get("mime_type")
    elif msg_type in ("button",):
        # Legacy template quick-reply button.
        base["type"] = "text"
        base["text"] = message.get("button", {}).get("text", "")
    else:
        base["type"] = "unsupported"

    return InboundMessage(**base)
