"""Pure builders for WhatsApp Cloud API message payloads, plus a renderer.

The builders produce the exact JSON the Graph API ``/messages`` endpoint
expects and enforce WhatsApp's size limits (button titles <= 20 chars, list
rows <= 10, row titles <= 24, and so on). They never do I/O, so the same
payloads are POSTed to Meta in live mode, stored in the outbox in dry-run
mode, and printed by the chat simulator.

``render`` turns any payload back into something a person can read: the body
text plus the options a customer could tap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_BUTTONS = 3
MAX_LIST_ROWS = 10


def _base(to: str) -> dict:
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}


def text_payload(to: str, body: str, preview_url: bool = False) -> dict:
    payload = _base(to)
    payload["type"] = "text"
    payload["text"] = {"preview_url": preview_url, "body": body[:4096]}
    return payload


def image_payload(to: str, link: str, caption: str = "") -> dict:
    payload = _base(to)
    payload["type"] = "image"
    image: dict = {"link": link}
    if caption:
        image["caption"] = caption[:1024]
    payload["image"] = image
    return payload


def buttons_payload(to: str, body: str, buttons: list[dict], header: str = "") -> dict:
    """buttons: list of {"id": str, "title": str} (max 3, title <= 20 chars)."""
    action_buttons = [
        {"type": "reply", "reply": {"id": b["id"][:256], "title": b["title"][:20]}}
        for b in buttons[:MAX_BUTTONS]
    ]
    interactive: dict = {
        "type": "button",
        "body": {"text": body[:1024]},
        "action": {"buttons": action_buttons},
    }
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    payload = _base(to)
    payload["type"] = "interactive"
    payload["interactive"] = interactive
    return payload


def list_payload(
    to: str,
    body: str,
    button_text: str,
    sections: list[dict],
    header: str = "",
    footer: str = "",
) -> dict:
    """sections: [{"title": str, "rows": [{"id","title","description"}]}].

    WhatsApp limits: <=10 sections, <=10 rows in total, row title <= 24 chars,
    description <= 72 chars, button text <= 20 chars.
    """
    clean_sections = []
    budget = MAX_LIST_ROWS
    for section in sections[:10]:
        rows = []
        for row in section.get("rows", []):
            if budget <= 0:
                break
            entry = {"id": row["id"][:200], "title": row["title"][:24]}
            if row.get("description"):
                entry["description"] = row["description"][:72]
            rows.append(entry)
            budget -= 1
        if rows:
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
    payload = _base(to)
    payload["type"] = "interactive"
    payload["interactive"] = interactive
    return payload


def read_payload(message_id: str, typing: bool = True) -> dict:
    """Mark a message read and (optionally) show the typing indicator."""
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    if typing:
        payload["typing_indicator"] = {"type": "text"}
    return payload


# ---------------------------------------------------------------- rendering
@dataclass
class Option:
    id: str
    title: str
    description: str = ""


@dataclass
class Rendered:
    kind: str  # text | buttons | list | image | status | other
    to: str
    body: str
    options: list[Option] = field(default_factory=list)
    button_text: str = ""

    def as_text(self) -> str:
        """One-string summary, used for transcripts."""
        if not self.options:
            return self.body
        titles = " | ".join(o.title for o in self.options)
        return f"{self.body}\n[{titles}]"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "to": self.to,
            "body": self.body,
            "button_text": self.button_text,
            "options": [
                {"id": o.id, "title": o.title, "description": o.description} for o in self.options
            ],
        }


def render(payload: dict) -> Rendered:
    """Human-readable view of a payload built by this module."""
    to = payload.get("to", "")
    if payload.get("status") == "read":
        return Rendered("status", to, f"(read receipt for {payload.get('message_id')})")
    ptype = payload.get("type")
    if ptype == "text":
        return Rendered("text", to, payload["text"]["body"])
    if ptype == "image":
        image = payload["image"]
        caption = image.get("caption", "")
        return Rendered("image", to, f"[image] {image.get('link', '')} {caption}".strip())
    if ptype == "interactive":
        inter = payload["interactive"]
        body = inter.get("body", {}).get("text", "")
        if inter.get("type") == "button":
            options = [
                Option(b["reply"]["id"], b["reply"]["title"]) for b in inter["action"]["buttons"]
            ]
            return Rendered("buttons", to, body, options)
        if inter.get("type") == "list":
            options = [
                Option(r["id"], r["title"], r.get("description", ""))
                for s in inter["action"]["sections"]
                for r in s["rows"]
            ]
            return Rendered("list", to, body, options, button_text=inter["action"].get("button", ""))
    return Rendered("other", to, str(payload))
