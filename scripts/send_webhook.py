#!/usr/bin/env python3
"""Send a correctly-signed fake webhook to your local server.

Lets you exercise the whole agent (routing, RAG, booking) without Meta or a
public tunnel. The signature is computed at runtime from WHATSAPP_APP_SECRET so
this file contains no secret-shaped strings.

Usage:
    # start the server first: uvicorn app.main:app --reload
    python scripts/send_webhook.py "hola, cuánto cuesta un corte?"
    python scripts/send_webhook.py --interactive svc:svc_corte "Corte de cabello"
    python scripts/send_webhook.py --from 50761234567 "quiero una cita"

Set TARGET_URL to point at a different host/port if needed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

# Make `import app...` work when run from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.security import compute_signature  # noqa: E402

load_dotenv()

TARGET_URL = os.getenv("TARGET_URL", "http://localhost:8000/webhook")


def build_payload(from_id: str, message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "0",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "contacts": [
                                {"profile": {"name": "Test User"}, "wa_id": from_id}
                            ],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a signed fake WhatsApp webhook.")
    parser.add_argument("body", help="Message text, or the reply title with --interactive.")
    parser.add_argument("title", nargs="?", default="", help="Interactive reply title.")
    parser.add_argument("--interactive", action="store_true", help="Treat body as a reply id.")
    parser.add_argument("--from", dest="from_id", default="50760001234", help="Sender wa_id.")
    args = parser.parse_args()

    message_id = f"wamid.LOCAL{int(time.time() * 1000)}"
    if args.interactive:
        message = {
            "from": args.from_id,
            "id": message_id,
            "timestamp": str(int(time.time())),
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {"id": args.body, "title": args.title or args.body},
            },
        }
    else:
        message = {
            "from": args.from_id,
            "id": message_id,
            "timestamp": str(int(time.time())),
            "type": "text",
            "text": {"body": args.body},
        }

    payload = build_payload(args.from_id, message)
    raw = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    app_secret = os.getenv("WHATSAPP_APP_SECRET", "")
    if app_secret:
        headers["X-Hub-Signature-256"] = compute_signature(app_secret, raw)
    else:
        print("WARNING: WHATSAPP_APP_SECRET not set; sending without a signature.")

    try:
        resp = httpx.post(TARGET_URL, content=raw, headers=headers, timeout=15.0)
    except httpx.HTTPError as exc:
        print(f"Request failed: {exc}")
        print("Is the server running?  uvicorn app.main:app --reload")
        return 1

    print(f"POST {TARGET_URL} -> {resp.status_code}")
    print(resp.text)
    return 0 if resp.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
