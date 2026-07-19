"""End-to-end agent behaviour: booking flow, routing, handoff, language."""

from __future__ import annotations

import itertools

from app.models import InboundMessage

_ids = itertools.count(1)
WA_ID = "50760001234"


def _text(body: str) -> InboundMessage:
    return InboundMessage(
        wa_id=WA_ID, message_id=f"wamid.{next(_ids)}", type="text", text=body
    )


def _interactive(pid: str, title: str = "") -> InboundMessage:
    return InboundMessage(
        wa_id=WA_ID,
        message_id=f"wamid.{next(_ids)}",
        type="interactive",
        interactive_id=pid,
        interactive_title=title,
    )


def test_full_booking_flow_persists_a_booking(agent, store, wa):
    # 1. User asks to book -> service list.
    agent.handle_message(_text("quiero una cita"))
    assert store.get_conversation(WA_ID).state == "booking_service"
    assert wa.last("list") is not None

    # 2. Pick a service -> date list.
    agent.handle_message(_interactive("svc:svc_corte", "Corte de cabello"))
    assert store.get_conversation(WA_ID).state == "booking_date"

    # 3. Pick a date -> time list.
    agent.handle_message(_interactive("date:2026-12-14", "Lun 14 dic"))
    assert store.get_conversation(WA_ID).state == "booking_time"

    # 4. Pick a time -> ask for name.
    agent.handle_message(_interactive("time:10:00", "10:00"))
    conv = store.get_conversation(WA_ID)
    assert conv.state == "booking_name"

    # 5. Provide name -> confirm buttons.
    agent.handle_message(_text("Juan Pérez"))
    conv = store.get_conversation(WA_ID)
    assert conv.state == "booking_confirm"
    assert conv.draft["name"] == "Juan Pérez"
    assert wa.last("buttons") is not None

    # 6. Confirm -> persisted, back to idle.
    agent.handle_message(_interactive("confirm:yes", "Confirmar"))
    conv = store.get_conversation(WA_ID)
    assert conv.state == "idle"
    assert "10:00" in store.booked_times("2026-12-14")

    done = wa.last("text")
    assert "Juan Pérez" in done["body"]


def test_cancel_during_confirm(agent, store, wa):
    agent.handle_message(_text("reservar"))
    agent.handle_message(_interactive("svc:svc_barba", "Arreglo de barba"))
    agent.handle_message(_interactive("date:2026-12-15", "Mar 15 dic"))
    agent.handle_message(_interactive("time:11:00", "11:00"))
    agent.handle_message(_text("Ana"))
    agent.handle_message(_interactive("confirm:cancel", "Cancelar"))
    conv = store.get_conversation(WA_ID)
    assert conv.state == "idle"
    assert conv.draft == {}
    assert "11:00" not in store.booked_times("2026-12-15")


def test_greeting_shows_menu(agent, store, wa):
    agent.handle_message(_text("hola"))
    assert wa.last("buttons") is not None
    ids = [b["id"] for b in wa.last("buttons")["buttons"]]
    assert "menu:booking" in ids and "menu:human" in ids


def test_human_request_triggers_handoff(agent, store, wa):
    agent.handle_message(_text("quiero hablar con una persona"))
    conv = store.get_conversation(WA_ID)
    assert conv.handoff is True
    assert conv.state == "human"

    # While handed off, the bot stays quiet (only mark_read, no new replies).
    wa.sent.clear()
    agent.handle_message(_text("sigo esperando"))
    assert wa.kinds() == []

    # An explicit reset re-activates the bot.
    agent.handle_message(_text("menu"))
    assert store.get_conversation(WA_ID).handoff is False


def test_language_detection_switches_to_english(agent, store, wa):
    agent.handle_message(_text("how much is a haircut?"))
    assert store.get_conversation(WA_ID).lang == "en"
    assert wa.last("text") is not None  # fallback KB answer was sent


def test_image_without_caption_escalates(agent, store, wa):
    msg = InboundMessage(
        wa_id=WA_ID, message_id=f"wamid.{next(_ids)}", type="image", media_id="media.1"
    )
    agent.handle_message(msg)
    assert store.get_conversation(WA_ID).handoff is True
