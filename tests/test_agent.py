"""End-to-end agent behaviour: booking flow, routing, handoff, language.

The clock is frozen at Thu 24 Sep 2026 08:00 (America/Panama); dates are taken
from what the bot actually offered, never hardcoded.
"""

from __future__ import annotations

from app.models import InboundMessage

from .conftest import tap_msg, text_msg

WA_ID = "50760001234"


def _text(body: str, profile_name: str | None = None) -> InboundMessage:
    return text_msg(WA_ID, body, profile_name)


def _tap(pid: str, title: str = "") -> InboundMessage:
    return tap_msg(WA_ID, pid, title)


def _first(wa, prefix: str) -> str:
    return next(pid for pid in wa.last_options() if pid.startswith(prefix))


def test_full_booking_flow_persists_a_booking(agent, store, wa):
    # 1. User asks to book -> service list.
    agent.handle_message(_text("quiero una cita"))
    assert store.get_conversation(WA_ID).state == "booking_service"
    assert "svc:svc_corte" in wa.last_options()

    # 2. Pick a service -> date list (starting today, Thu 24 Sep).
    agent.handle_message(_tap("svc:svc_corte", "Corte de cabello"))
    assert store.get_conversation(WA_ID).state == "booking_date"
    day = _first(wa, "date:")
    assert day == "date:2026-09-24"
    assert wa.last("list")["rows"][0]["description"] == "Hoy"

    # 3. Pick a date -> time list.
    agent.handle_message(_tap(day, "Jue 24 sep"))
    assert store.get_conversation(WA_ID).state == "booking_time"
    assert "time:10:00" in wa.last_options()

    # 4. Pick a time -> ask for name.
    agent.handle_message(_tap("time:10:00", "10:00"))
    assert store.get_conversation(WA_ID).state == "booking_name"

    # 5. Provide name -> confirm buttons.
    agent.handle_message(_text("Juan Pérez"))
    conv = store.get_conversation(WA_ID)
    assert conv.state == "booking_confirm"
    assert conv.draft["name"] == "Juan Pérez"
    assert wa.last_options() == ["confirm:yes", "confirm:edit", "confirm:cancel"]

    # 6. Confirm -> persisted, back to idle.
    agent.handle_message(_tap("confirm:yes", "Confirmar"))
    conv = store.get_conversation(WA_ID)
    assert conv.state == "idle"
    assert "10:00" in store.booked_times("2026-09-24")

    done = wa.last("text")["body"]
    assert "Juan Pérez" in done and "Jue 24 sep" in done and "mis citas" in done


def test_profile_name_button_fills_the_name(agent, store, wa):
    agent.handle_message(_text("quiero una cita", profile_name="Ana Gómez"))
    agent.handle_message(_tap("svc:svc_barba"))
    agent.handle_message(_tap(_first(wa, "date:")))
    agent.handle_message(_tap(_first(wa, "time:")))
    assert wa.last_options() == ["name:profile"]
    assert wa.last("buttons")["buttons"][0]["title"] == "Ana Gómez"
    agent.handle_message(_tap("name:profile", "Ana Gómez"))
    assert store.get_conversation(WA_ID).draft["name"] == "Ana Gómez"


def test_cancel_during_confirm(agent, store, wa):
    agent.handle_message(_text("reservar"))
    agent.handle_message(_tap("svc:svc_barba", "Arreglo de barba"))
    agent.handle_message(_tap("date:2026-09-25", "Vie 25 sep"))
    agent.handle_message(_tap("time:11:00", "11:00"))
    agent.handle_message(_text("Ana"))
    agent.handle_message(_tap("confirm:cancel", "Cancelar"))
    conv = store.get_conversation(WA_ID)
    assert conv.state == "idle"
    assert conv.draft == {}
    assert "11:00" not in store.booked_times("2026-09-25")


def test_greeting_shows_menu(agent, store, wa):
    agent.handle_message(_text("hola"))
    assert wa.last_options() == ["menu:booking", "menu:info", "menu:human"]


def test_human_request_triggers_handoff(agent, store, wa):
    agent.handle_message(_text("quiero hablar con una persona"))
    conv = store.get_conversation(WA_ID)
    assert conv.handoff is True
    assert conv.state == "human"
    assert store.open_handoffs()[0]["reason"] == "requested"

    # While handed off, the bot stays quiet (only mark_read, no new replies).
    wa.sent.clear()
    agent.handle_message(_text("sigo esperando"))
    assert wa.kinds() == []

    # An explicit reset re-activates the bot and closes the queue entry.
    agent.handle_message(_text("menu"))
    assert store.get_conversation(WA_ID).handoff is False
    assert store.open_handoffs() == []


def test_language_detection_switches_to_english(agent, store, wa):
    agent.handle_message(_text("hello, I want to book an appointment"))
    assert store.get_conversation(WA_ID).lang == "en"
    assert wa.last("list")["body"] == "Great. Which service would you like to book?"


def test_image_without_caption_escalates(agent, store, wa):
    msg = InboundMessage(wa_id=WA_ID, message_id="wamid.img1", type="image", media_id="media.1")
    agent.handle_message(msg)
    assert store.get_conversation(WA_ID).handoff is True


def test_transcript_records_both_sides(agent, store, wa):
    agent.handle_message(_text("hola"))
    rows = store.transcript(WA_ID)
    assert [r["author"] for r in rows] == ["customer", "bot"]
    assert rows[1]["content"].endswith("[Reservar cita | Info y precios | Hablar con alguien]")
