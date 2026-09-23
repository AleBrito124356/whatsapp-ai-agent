"""Staff API: auth, handoff queue, replies, the 24-hour window, resolve, bookings."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.catalog import t
from app.config import Settings
from app.deps import build_services
from app.main import create_app

from .conftest import FakeWhatsApp, OfflineLLM, tap_msg, text_msg

TOKEN = "".join(["staff", "-", "token", "-", "for", "-", "tests"])
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ANA = "50760000001"


@pytest.fixture
def wa():
    return FakeWhatsApp()


@pytest.fixture
def services(tmp_path, clock, wa):
    settings = Settings(admin_token=TOKEN)
    return build_services(settings, db_path=tmp_path / "admin.sqlite", clock=clock, llm=OfflineLLM(), wa=wa)


@pytest.fixture
def client(services):
    with TestClient(create_app(services)) as c:
        yield c


def say(services, body, wa_id=ANA, name="Ana Gómez"):
    services.agent.handle_message(text_msg(wa_id, body, profile_name=name))


def test_admin_is_disabled_without_a_token(tmp_path, clock):
    services = build_services(Settings(), db_path=tmp_path / "x.sqlite", clock=clock, llm=OfflineLLM(), wa=FakeWhatsApp())
    with TestClient(create_app(services)) as c:
        resp = c.get("/admin/handoffs", headers=AUTH)
    assert resp.status_code == 503
    assert "ADMIN_TOKEN" in resp.json()["detail"]


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"}],
    ids=["missing", "wrong-token", "no-scheme", "wrong-scheme"],
)
def test_bad_credentials_are_401(client, headers):
    resp = client.get("/admin/handoffs", headers=headers)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


def test_handoff_shows_up_in_the_queue_with_context(client, services):
    say(services, "hola")
    say(services, "quiero hablar con una persona")
    body = client.get("/admin/handoffs", headers=AUTH).json()
    assert body["count"] == 1
    item = body["handoffs"][0]
    assert item["wa_id"] == ANA and item["profile_name"] == "Ana Gómez"
    assert item["reason"] == "requested" and item["handoff"] is True
    assert item["window"]["open"] is True
    authors = [m["author"] for m in item["last_messages"]]
    assert authors == ["customer", "bot", "customer", "bot"]
    assert item["last_messages"][-1]["content"].startswith("Con gusto.")


def test_staff_reply_goes_out_through_whatsapp_and_is_logged(client, services, wa):
    say(services, "tengo una queja")
    wa.sent.clear()
    resp = client.post(f"/admin/conversations/{ANA}/reply", json={"text": "Hola Ana, soy Carlos. ¿Qué pasó?"}, headers=AUTH)
    assert resp.status_code == 200 and resp.json()["sent"] is True
    assert wa.outbound() == [
        {"kind": "text", "to": ANA, "body": "Hola Ana, soy Carlos. ¿Qué pasó?", "payload": wa.outbound()[0]["payload"]}
    ]
    convo = client.get(f"/admin/conversations/{ANA}", headers=AUTH).json()
    assert convo["messages"][-1] == {**convo["messages"][-1], "author": "staff", "content": "Hola Ana, soy Carlos. ¿Qué pasó?"}

    # The bot stays quiet while staff handle the chat.
    wa.sent.clear()
    say(services, "el corte quedó disparejo")
    assert wa.outbound() == []


def test_staff_reply_outside_the_24h_window_needs_a_template(client, services, clock, wa):
    say(services, "quiero hablar con alguien")
    clock.advance(hours=25)
    wa.sent.clear()
    resp = client.post(f"/admin/conversations/{ANA}/reply", json={"text": "¿Sigues ahí?"}, headers=AUTH)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "template_required" and "25.0 h ago" in detail["message"]
    assert wa.outbound() == []
    # A new customer message reopens the window.
    say(services, "sí, aquí estoy")
    assert client.post(f"/admin/conversations/{ANA}/reply", json={"text": "¡Perfecto!"}, headers=AUTH).status_code == 200


def test_staff_cannot_message_an_opted_out_customer(client, services, wa):
    say(services, "BAJA")
    resp = client.post(f"/admin/conversations/{ANA}/reply", json={"text": "hola"}, headers=AUTH)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "opted_out"


def test_reply_errors(client, services, wa):
    assert client.post("/admin/conversations/000/reply", json={"text": "hi"}, headers=AUTH).status_code == 404
    say(services, "hola")
    assert client.post(f"/admin/conversations/{ANA}/reply", json={"text": ""}, headers=AUTH).status_code == 422
    wa.fail = True
    resp = client.post(f"/admin/conversations/{ANA}/reply", json={"text": "hola"}, headers=AUTH)
    assert resp.status_code == 502


def test_staff_can_step_in_without_a_handoff(client, services, wa):
    say(services, "quiero una cita")  # mid-booking
    assert client.post(f"/admin/conversations/{ANA}/reply", json={"text": "Te ayudo yo"}, headers=AUTH).status_code == 200
    conv = services.store.get_conversation(ANA)
    assert conv.handoff is True and conv.state == "human" and conv.draft == {}
    assert client.get("/admin/handoffs", headers=AUTH).json()["handoffs"][0]["reason"] == "staff"


def test_resolve_hands_the_chat_back_to_the_bot(client, services, wa):
    say(services, "quiero hablar con una persona")
    wa.sent.clear()
    resp = client.post(f"/admin/handoffs/{ANA}/resolve", json={"notify": True}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"wa_id": ANA, "resolved": 1, "bot_active": True, "notified": True, "note": None}
    assert wa.last("text")["body"] == t("handoff_resolved", "es", business="Barbería Studio Norte")
    assert client.get("/admin/handoffs", headers=AUTH).json()["count"] == 0

    conv = services.store.get_conversation(ANA)
    assert conv.handoff is False and conv.state == "idle"

    # The next customer message gets a bot reply again.
    wa.sent.clear()
    say(services, "hola")
    assert wa.last_options() == ["menu:booking", "menu:info", "menu:human"]


def test_resolve_without_notification_and_outside_the_window(client, services, clock, wa):
    say(services, "quiero hablar con una persona")
    clock.advance(hours=30)
    resp = client.post(f"/admin/handoffs/{ANA}/resolve", headers=AUTH).json()
    assert resp["resolved"] == 1 and resp["notified"] is False and "24-hour" in resp["note"]
    assert client.post("/admin/handoffs/000/resolve", headers=AUTH).status_code == 404


def test_resolving_a_chat_that_was_never_handed_off_changes_nothing(client, services, wa):
    say(services, "quiero una cita")  # mid-booking, never handed off
    wa.sent.clear()
    resp = client.post(f"/admin/handoffs/{ANA}/resolve", headers=AUTH).json()
    assert resp["resolved"] == 0 and resp["notified"] is False
    assert wa.outbound() == []
    assert services.store.get_conversation(ANA).state == "booking_service"


def test_bookings_listing_and_staff_cancellation(client, services, wa):
    agent = services.agent
    say(services, "quiero una cita")
    agent.handle_message(tap_msg(ANA, "svc:svc_corte"))
    agent.handle_message(tap_msg(ANA, "date:2026-09-25"))
    agent.handle_message(tap_msg(ANA, "time:15:00"))
    agent.handle_message(tap_msg(ANA, "name:profile"))
    agent.handle_message(tap_msg(ANA, "confirm:yes"))

    listing = client.get("/admin/bookings", params={"date": "2026-09-25"}, headers=AUTH).json()
    assert listing["count"] == 1
    booking = listing["bookings"][0]
    assert (booking["customer_name"], booking["time"], booking["status"]) == ("Ana Gómez", "15:00", "confirmed")
    assert client.get("/admin/bookings", headers=AUTH).json()["count"] == 1  # default: today onwards
    assert client.get("/admin/bookings", params={"date": "25/09"}, headers=AUTH).status_code == 422

    wa.sent.clear()
    resp = client.post(f"/admin/bookings/{booking['id']}/cancel", json={"notify": True}, headers=AUTH).json()
    assert resp["booking"]["status"] == "cancelled" and resp["booking"]["cancelled_by"] == "staff"
    assert resp["notified"] is True
    assert wa.last("text")["body"].startswith("Hola Ana Gómez, el equipo de Barbería Studio Norte canceló tu cita")
    assert services.store.booked_times("2026-09-25") == set()

    again = client.post(f"/admin/bookings/{booking['id']}/cancel", headers=AUTH)
    assert again.status_code == 409
    cancelled = client.get("/admin/bookings", params={"status": "cancelled"}, headers=AUTH).json()
    assert cancelled["count"] == 1
    assert client.post("/admin/bookings/999/cancel", headers=AUTH).status_code == 404
