"""Payload parsing and dedupe primitives."""

from __future__ import annotations

from app.webhook import parse_messages


def _envelope(message: dict, contact_name: str = "Alex") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "PNID"},
                            "contacts": [
                                {"profile": {"name": contact_name}, "wa_id": "50760001234"}
                            ],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def test_parse_text_message():
    payload = _envelope(
        {
            "from": "50760001234",
            "id": "wamid.AAA",
            "timestamp": "1700000000",
            "type": "text",
            "text": {"body": "Hola"},
        }
    )
    messages = list(parse_messages(payload))
    assert len(messages) == 1
    msg = messages[0]
    assert msg.type == "text"
    assert msg.text == "Hola"
    assert msg.profile_name == "Alex"


def test_parse_interactive_list_reply():
    payload = _envelope(
        {
            "from": "50760001234",
            "id": "wamid.BBB",
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {"id": "svc:svc_corte", "title": "Corte de cabello"},
            },
        }
    )
    msg = next(parse_messages(payload))
    assert msg.type == "interactive"
    assert msg.interactive_id == "svc:svc_corte"
    assert msg.interactive_title == "Corte de cabello"


def test_parse_audio_message():
    payload = _envelope(
        {
            "from": "50760001234",
            "id": "wamid.CCC",
            "type": "audio",
            "audio": {"id": "media.123", "mime_type": "audio/ogg", "voice": True},
        }
    )
    msg = next(parse_messages(payload))
    assert msg.type == "audio"
    assert msg.media_id == "media.123"


def test_status_callbacks_are_ignored():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [{"id": "wamid.X", "status": "delivered"}],
                        },
                    }
                ]
            }
        ],
    }
    assert list(parse_messages(payload)) == []


def test_dedupe_via_store(store):
    assert store.mark_processed("wamid.dup", "50760001234") is True
    assert store.mark_processed("wamid.dup", "50760001234") is False
    assert store.is_duplicate("wamid.dup") is True
