"""WhatsApp Cloud API client (Graph API).

Wraps the ``/{phone_number_id}/messages`` endpoint for the message types this
agent uses: text, images, interactive reply buttons and lists, plus read
receipts with a typing indicator. Also downloads inbound media (voice notes)
so the agent can transcribe them.

All calls are synchronous (httpx.Client). The webhook runs the agent in a
FastAPI background task, off the request path, so blocking here is fine.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .config import Settings

log = logging.getLogger("whatsapp_agent.wa_client")


class WhatsAppClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.Client(timeout=30.0)

    # ------------------------------------------------------------- internals
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.settings.whatsapp_token}",
            "Content-Type": "application/json",
        }

    def _post(self, payload: dict) -> Optional[dict]:
        if not self.settings.whatsapp_token or not self.settings.phone_number_id:
            log.warning("WhatsApp not configured; would send: %s", payload)
            return None
        try:
            resp = self._client.post(
                self.settings.messages_url, headers=self._headers(), json=payload
            )
            if resp.status_code >= 400:
                log.error("Graph API %s: %s", resp.status_code, resp.text)
                return None
            return resp.json()
        except httpx.HTTPError as exc:
            log.error("Graph API request failed: %s", exc)
            return None

    @staticmethod
    def _base(to: str) -> dict:
        return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}

    # --------------------------------------------------------------- senders
    def send_text(self, to: str, body: str, preview_url: bool = False) -> Optional[dict]:
        payload = self._base(to)
        payload["type"] = "text"
        payload["text"] = {"preview_url": preview_url, "body": body[:4096]}
        return self._post(payload)

    def send_image(self, to: str, link: str, caption: str = "") -> Optional[dict]:
        payload = self._base(to)
        payload["type"] = "image"
        image: dict = {"link": link}
        if caption:
            image["caption"] = caption[:1024]
        payload["image"] = image
        return self._post(payload)

    def send_buttons(self, to: str, body: str, buttons: list[dict], header: str = "") -> Optional[dict]:
        """buttons: list of {"id": str, "title": str} (max 3, title <= 20 chars)."""
        action_buttons = [
            {"type": "reply", "reply": {"id": b["id"][:256], "title": b["title"][:20]}}
            for b in buttons[:3]
        ]
        interactive: dict = {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {"buttons": action_buttons},
        }
        if header:
            interactive["header"] = {"type": "text", "text": header[:60]}
        payload = self._base(to)
        payload["type"] = "interactive"
        payload["interactive"] = interactive
        return self._post(payload)

    def send_list(
        self,
        to: str,
        body: str,
        button_text: str,
        sections: list[dict],
        header: str = "",
        footer: str = "",
    ) -> Optional[dict]:
        """sections: [{"title": str, "rows": [{"id","title","description"}]}].

        WhatsApp limits: <=10 sections, <=10 rows total, title <= 24 chars,
        description <= 72 chars, button_text <= 20 chars.
        """
        clean_sections = []
        for section in sections:
            rows = []
            for row in section.get("rows", []):
                entry = {"id": row["id"][:200], "title": row["title"][:24]}
                if row.get("description"):
                    entry["description"] = row["description"][:72]
                rows.append(entry)
            clean_sections.append({"title": section.get("title", "")[:24], "rows": rows})

        interactive: dict = {
            "type": "list",
            "body": {"text": body[:1024]},
            "action": {"button": button_text[:20], "sections": clean_sections},
        }
        if header:
            interactive["header"] = {"type": "text", "text": header[:60]}
        if footer:
            interactive["footer"] = {"text": footer[:60]}
        payload = self._base(to)
        payload["type"] = "interactive"
        payload["interactive"] = interactive
        return self._post(payload)

    def mark_read(self, message_id: str, typing: bool = True) -> Optional[dict]:
        """Mark a message read and (optionally) show the typing indicator.

        The typing indicator auto-dismisses when you send the next message or
        after ~25 seconds.
        """
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        return self._post(payload)

    # ----------------------------------------------------------------- media
    def download_media(self, media_id: str) -> Optional[bytes]:
        """Resolve a media id to a temporary URL, then download the bytes.

        Used for inbound voice notes. Hand the returned bytes to
        ``app.transcribe.transcribe_audio`` (faster-whisper) to get text.
        """
        if not self.settings.whatsapp_token:
            log.warning("WhatsApp not configured; cannot download media %s", media_id)
            return None
        try:
            meta = self._client.get(
                f"{self.settings.graph_base_url}/{media_id}",
                headers={"Authorization": f"Bearer {self.settings.whatsapp_token}"},
            )
            meta.raise_for_status()
            url = meta.json().get("url")
            if not url:
                return None
            media = self._client.get(
                url, headers={"Authorization": f"Bearer {self.settings.whatsapp_token}"}
            )
            media.raise_for_status()
            return media.content
        except httpx.HTTPError as exc:
            log.error("Media download failed for %s: %s", media_id, exc)
            return None

    def close(self) -> None:
        self._client.close()
