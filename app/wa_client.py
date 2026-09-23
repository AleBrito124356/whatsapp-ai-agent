"""WhatsApp Cloud API client.

``WhatsAppClient`` exposes the message types this agent uses (text, images,
interactive reply buttons and lists, read receipts) and inbound media
download. Payloads are built by :mod:`app.wa_payloads`; *delivering* them is
the job of a transport:

- :class:`GraphTransport` POSTs to ``graph.facebook.com/{version}/{phone_id}/messages``.
- :class:`DryRunTransport` stores the exact payload in the local ``outbox``
  table instead. It is selected automatically when WhatsApp credentials are
  missing or are the ``.env.example`` placeholders (see ``Settings.dry_run``),
  so a fresh checkout never calls Meta with a fake token.
- :class:`CallbackTransport` hands each payload to a Python function (used by
  the terminal chat simulator and tests).

All calls are synchronous. The webhook runs the agent in a FastAPI background
task, off the request path, so blocking here is fine.
"""

from __future__ import annotations

import itertools
import logging
from typing import Callable, Optional, Protocol

import httpx

from . import wa_payloads as P
from .config import Settings

log = logging.getLogger("whatsapp_agent.wa_client")


class Transport(Protocol):
    def deliver(self, payload: dict) -> Optional[dict]:
        """Send one payload; return the Graph-style response or None on failure."""


class GraphTransport:
    """Live delivery through the Meta Graph API."""

    def __init__(self, settings: Settings, http_client: Optional[httpx.Client] = None):
        self.settings = settings
        self.http = http_client or httpx.Client(timeout=30.0)

    def deliver(self, payload: dict) -> Optional[dict]:
        try:
            resp = self.http.post(
                self.settings.messages_url,
                headers={
                    "Authorization": f"Bearer {self.settings.whatsapp_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 400:
                log.error("Graph API %s: %s", resp.status_code, resp.text[:500])
                return None
            return resp.json()
        except httpx.HTTPError as exc:
            log.error("Graph API request failed: %s", exc)
            return None


class DryRunTransport:
    """Writes outbound messages to the ``outbox`` table instead of sending them.

    Read receipts are not messages, so they are acknowledged but not stored.
    """

    def __init__(self, outbox):
        self.outbox = outbox  # anything with add_outbox(wa_id, payload) -> int

    def deliver(self, payload: dict) -> Optional[dict]:
        if payload.get("status") == "read":
            return {"success": True, "dry_run": True}
        outbox_id = self.outbox.add_outbox(payload.get("to", ""), payload)
        log.info("[dry-run] outbox #%s -> %s: %s", outbox_id, payload.get("to"), P.render(payload).as_text()[:120])
        return {
            "messaging_product": "whatsapp",
            "messages": [{"id": f"wamid.DRYRUN.{outbox_id}"}],
            "dry_run": True,
        }


class CallbackTransport:
    """Passes every payload to ``callback`` (simulator / tests)."""

    def __init__(self, callback: Callable[[dict], None]):
        self.callback = callback
        self._ids = itertools.count(1)

    def deliver(self, payload: dict) -> Optional[dict]:
        self.callback(payload)
        return {"messaging_product": "whatsapp", "messages": [{"id": f"wamid.LOCAL.{next(self._ids)}"}]}


class WhatsAppClient:
    def __init__(
        self,
        settings: Settings,
        transport: Optional[Transport] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        self.settings = settings
        if transport is None:
            if settings.dry_run:
                # No outbox to write to: just log. app.deps.build_services wires
                # a DryRunTransport backed by SQLite instead.
                transport = CallbackTransport(
                    lambda payload: log.warning(
                        "WhatsApp not configured (dry-run); would send: %s", payload
                    )
                )
            else:
                transport = GraphTransport(settings, http_client)
        self.transport = transport

    @property
    def dry_run(self) -> bool:
        return not isinstance(self.transport, GraphTransport)

    # --------------------------------------------------------------- senders
    def send_payload(self, payload: dict) -> Optional[dict]:
        return self.transport.deliver(payload)

    def send_text(self, to: str, body: str, preview_url: bool = False) -> Optional[dict]:
        return self.send_payload(P.text_payload(to, body, preview_url))

    def send_image(self, to: str, link: str, caption: str = "") -> Optional[dict]:
        return self.send_payload(P.image_payload(to, link, caption))

    def send_buttons(self, to: str, body: str, buttons: list[dict], header: str = "") -> Optional[dict]:
        """buttons: list of {"id": str, "title": str} (max 3, title <= 20 chars)."""
        return self.send_payload(P.buttons_payload(to, body, buttons, header))

    def send_list(
        self,
        to: str,
        body: str,
        button_text: str,
        sections: list[dict],
        header: str = "",
        footer: str = "",
    ) -> Optional[dict]:
        """sections: [{"title": str, "rows": [{"id","title","description"}]}]."""
        return self.send_payload(P.list_payload(to, body, button_text, sections, header, footer))

    def mark_read(self, message_id: str, typing: bool = True) -> Optional[dict]:
        """Mark a message read and (optionally) show the typing indicator.

        The typing indicator auto-dismisses when you send the next message or
        after ~25 seconds.
        """
        return self.send_payload(P.read_payload(message_id, typing))

    # ----------------------------------------------------------------- media
    def download_media(self, media_id: str) -> Optional[bytes]:
        """Resolve a media id to a temporary URL, then download the bytes.

        Used for inbound voice notes. Hand the returned bytes to
        ``app.transcribe.transcribe_audio`` (faster-whisper) to get text.
        Not available in dry-run mode (there is no real media to fetch).
        """
        if self.dry_run:
            log.info("[dry-run] cannot download media %s", media_id)
            return None
        http = self.transport.http  # type: ignore[attr-defined]
        auth = {"Authorization": f"Bearer {self.settings.whatsapp_token}"}
        try:
            meta = http.get(f"{self.settings.graph_base_url}/{media_id}", headers=auth)
            meta.raise_for_status()
            url = meta.json().get("url")
            if not url:
                return None
            media = http.get(url, headers=auth)
            media.raise_for_status()
            return media.content
        except httpx.HTTPError as exc:
            log.error("Media download failed for %s: %s", media_id, exc)
            return None

    def close(self) -> None:
        http = getattr(self.transport, "http", None)
        if http is not None:
            http.close()
