"""Typed representation of an inbound WhatsApp message.

The webhook flattens Meta's nested payload into these objects; the agent only
ever sees ``InboundMessage``, never raw JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class InboundMessage:
    wa_id: str
    message_id: str
    type: str  # text | interactive | image | audio | unsupported
    timestamp: Optional[str] = None
    profile_name: Optional[str] = None

    # text
    text: Optional[str] = None

    # interactive (button_reply / list_reply)
    interactive_id: Optional[str] = None
    interactive_title: Optional[str] = None

    # media (image / audio)
    media_id: Optional[str] = None
    media_mime: Optional[str] = None
    caption: Optional[str] = None
