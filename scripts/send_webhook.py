#!/usr/bin/env python3
"""Send a correctly-signed fake webhook to your local server and show the reply.

Exercises the real HTTP path (signature check, dedupe, background processing)
without Meta or a public tunnel. The signature is computed at runtime from
WHATSAPP_APP_SECRET, so this file contains no secret-shaped strings.

When the server runs in dry-run mode (no real WhatsApp credentials, the
default for a fresh checkout), the bot's replies go to the outbox. This script
waits until the agent has finished handling the message, then prints what the
bot answered: text, numbered buttons, and list rows with their reply ids.

Usage:
    # start the server first: uvicorn app.main:app --port 8000
    python scripts/send_webhook.py "hola, cuánto cuesta un corte?"
    python scripts/send_webhook.py "quiero una cita"
    python scripts/send_webhook.py --interactive svc:svc_corte "Corte de cabello"   # list reply
    python scripts/send_webhook.py --button confirm:yes "Confirmar"                   # button reply
    python scripts/send_webhook.py --from 50761234567 --name "Luis" "hola"

Set TARGET_URL to point at a different host/port (default
http://localhost:8000/webhook).
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

from app.cli import format_rendered  # noqa: E402
from app.security import compute_signature  # noqa: E402
from app.wa_payloads import Option, Rendered  # noqa: E402

load_dotenv()

TARGET_URL = os.getenv("TARGET_URL", "http://localhost:8000/webhook")


def build_payload(from_id: str, message: dict, name: str = "Test User") -> dict:
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
                            "contacts": [{"profile": {"name": name}, "wa_id": from_id}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def build_message(args: argparse.Namespace, message_id: str) -> dict:
    base = {"from": args.from_id, "id": message_id, "timestamp": str(int(time.time()))}
    if args.interactive or args.button:
        kind = "button_reply" if args.button else "list_reply"
        return {
            **base,
            "type": "interactive",
            "interactive": {"type": kind, kind: {"id": args.body, "title": args.title or args.body}},
        }
    return {**base, "type": "text", "text": {"body": args.body}}


def outbox_url(target: str) -> str:
    base = target[: -len("/webhook")] if target.endswith("/webhook") else target.rstrip("/")
    return f"{base}/dev/outbox"


def print_reply(messages: list[dict]) -> None:
    for m in messages:
        view = Rendered(
            kind=m["kind"],
            to=m["to"],
            body=m["body"],
            options=[Option(o["id"], o["title"], o.get("description", "")) for o in m["options"]],
            button_text=m.get("button_text", ""),
        )
        print(format_rendered(view))
        for i, o in enumerate(view.options, 1):
            flag = "--button" if view.kind == "buttons" else "--interactive"
            print(f"      ↳ {i}: {flag} {o.id} \"{o.title}\"")


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a signed fake WhatsApp webhook.")
    parser.add_argument("body", help="Message text, or the reply id with --interactive/--button.")
    parser.add_argument("title", nargs="?", default="", help="Interactive reply title.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--interactive", action="store_true", help="Send body as a list_reply id.")
    group.add_argument("--button", action="store_true", help="Send body as a button_reply id.")
    parser.add_argument("--from", dest="from_id", default="50760001234", help="Sender wa_id.")
    parser.add_argument("--name", default="Test User", help="Sender WhatsApp profile name.")
    parser.add_argument("--no-wait", action="store_true", help="Do not wait for / print the reply.")
    parser.add_argument("--timeout", type=float, default=15.0, help="Seconds to wait for the reply.")
    args = parser.parse_args()

    message_id = f"wamid.LOCAL{int(time.time() * 1000)}"
    payload = build_payload(args.from_id, build_message(args, message_id), args.name)
    raw = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    app_secret = os.getenv("WHATSAPP_APP_SECRET", "")
    if app_secret:
        headers["X-Hub-Signature-256"] = compute_signature(app_secret, raw)
    else:
        print("WARNING: WHATSAPP_APP_SECRET not set; sending without a signature.")

    outbox = outbox_url(TARGET_URL)
    cursor = None
    if not args.no_wait:
        try:
            probe = httpx.get(outbox, params={"limit": 1}, timeout=5.0)
            if probe.status_code == 200:
                cursor = probe.json()["cursor"]
            else:
                print("(server is in live mode: replies go to real WhatsApp, not the outbox)")
        except httpx.HTTPError:
            pass  # reported by the POST below

    try:
        resp = httpx.post(TARGET_URL, content=raw, headers=headers, timeout=15.0)
    except httpx.HTTPError as exc:
        print(f"Request failed: {exc}")
        print("Is the server running?  uvicorn app.main:app --port 8000")
        return 1

    print(f"POST {TARGET_URL} -> {resp.status_code} {resp.text}")
    if resp.status_code >= 400:
        return 1
    if cursor is None or args.no_wait:
        return 0
    if resp.json().get("accepted") == 0:
        print("(duplicate message id: nothing to process)")
        return 0

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        out = httpx.get(
            outbox,
            params={"wa_id": args.from_id, "after": cursor, "for_message": message_id},
            timeout=5.0,
        ).json()
        if out["handled"]:
            if out["messages"]:
                print_reply(out["messages"])
            else:
                print("(the bot sent nothing back: handed off to a person, or the contact opted out)")
            return 0
        time.sleep(0.2)
    print(f"(no reply within {args.timeout:.0f}s; check the server log)")
    return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
