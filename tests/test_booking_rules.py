"""Booking rules: validation, race safety, language stability, cancel/reschedule.

Each test reproduces a bug found in the audit of the first release and asserts
the fix. The clock is frozen at Thu 24 Sep 2026 08:00 (America/Panama).
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest

from app.agent import Agent
from app.catalog import t

from .conftest import TZ, FakeWhatsApp, OfflineLLM, TEST_SETTINGS, tap_msg, text_msg

ANA, LUIS, EVE = "50760000001", "50760000002", "50760000003"
EXPIRED_ES = t("option_expired", "es")


@pytest.fixture(autouse=True)
def _no_crashes(caplog):
    """The agent swallows exceptions to keep the worker alive; fail loudly here."""
    caplog.set_level(logging.ERROR, logger="whatsapp_agent")
    yield
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]


def book_until_name(agent, wa_id, service="svc:svc_corte", day="date:2026-09-24", time="time:10:00"):
    agent.handle_message(text_msg(wa_id, "quiero una cita"))
    agent.handle_message(tap_msg(wa_id, service))
    agent.handle_message(tap_msg(wa_id, day))
    agent.handle_message(tap_msg(wa_id, time))


def book(agent, wa_id, name, **kw):
    book_until_name(agent, wa_id, **kw)
    agent.handle_message(text_msg(wa_id, name))
    agent.handle_message(tap_msg(wa_id, "confirm:yes"))


# ------------------------------------------------------------ race safety
def test_two_customers_cannot_confirm_the_same_slot(agent, store, wa):
    # Both reach the name step while 10:00 is still free for each of them...
    book_until_name(agent, ANA)
    book_until_name(agent, LUIS)
    agent.handle_message(text_msg(ANA, "Ana"))
    agent.handle_message(text_msg(LUIS, "Luis"))
    # ...but only the first confirmation wins.
    agent.handle_message(tap_msg(ANA, "confirm:yes"))
    agent.handle_message(tap_msg(LUIS, "confirm:yes"))

    rows = store.list_bookings(date="2026-09-24")
    assert [(r["customer_name"], r["time"]) for r in rows] == [("Ana", "10:00")]

    luis = [m for m in wa.outbound() if m["to"] == LUIS]
    assert luis[-2]["body"].startswith("¡Uy! Alguien acaba de reservar el Jue 24 sep a las 10:00")
    assert luis[-1]["kind"] == "list" and "time:10:00" not in wa.last_options()
    conv = store.get_conversation(LUIS)
    assert conv.state == "booking_time" and "time" not in conv.draft

    # Luis picks another hour and gets it.
    agent.handle_message(tap_msg(LUIS, "time:11:00"))
    agent.handle_message(tap_msg(LUIS, "confirm:yes"))
    assert store.booked_times("2026-09-24") == {"10:00", "11:00"}


def test_taken_slot_is_rejected_when_tapped_from_an_old_list(agent, store, wa):
    agent.handle_message(text_msg(LUIS, "quiero una cita"))
    agent.handle_message(tap_msg(LUIS, "svc:svc_corte"))
    agent.handle_message(tap_msg(LUIS, "date:2026-09-24"))  # list still shows 10:00
    book(agent, ANA, "Ana")  # Ana takes 10:00 meanwhile
    agent.handle_message(tap_msg(LUIS, "time:10:00"))
    assert store.get_conversation(LUIS).state == "booking_time"
    assert wa.outbound()[-2]["body"].startswith("¡Uy!")


# ------------------------------------------------- forged / stale payloads
def test_forged_payloads_from_idle_create_nothing(agent, store, wa):
    """Audit BUG C: svc:svc_tinte, a Sunday, 03:00, a name and confirm:yes."""
    for msg in (
        tap_msg(EVE, "svc:svc_tinte"),
        tap_msg(EVE, "date:2026-09-27"),
        tap_msg(EVE, "time:03:00"),
        text_msg(EVE, "Night Owl"),
        tap_msg(EVE, "confirm:yes"),
    ):
        agent.handle_message(msg)
    assert store.list_bookings(status=None) == []
    assert store.get_conversation(EVE).state != "booking_confirm"
    assert sum(1 for m in wa.outbound() if m["body"] == EXPIRED_ES) >= 3


@pytest.mark.parametrize(
    "bad_day",
    ["date:2026-09-27", "date:2026-09-23", "date:2027-01-15", "date:mañana", "date:"],
    ids=["sunday", "past", "beyond-horizon", "garbage", "empty"],
)
def test_invalid_dates_are_rejected_and_the_list_is_resent(agent, store, wa, bad_day):
    agent.handle_message(text_msg(ANA, "quiero una cita"))
    agent.handle_message(tap_msg(ANA, "svc:svc_corte"))
    agent.handle_message(tap_msg(ANA, bad_day))
    conv = store.get_conversation(ANA)
    assert conv.state == "booking_date" and "date" not in conv.draft
    assert wa.outbound()[-2]["body"] == EXPIRED_ES
    assert wa.last("list")["body"].startswith("¿Para qué día?")


@pytest.mark.parametrize("bad_time", ["time:03:00", "time:10:30", "time:19:00", "time:08:00"])
def test_invalid_times_are_rejected(agent, store, wa, bad_time):
    # Thursday: 09:00-18:00 slots. 19:00 is closing time, 08:00 is before opening.
    agent.handle_message(text_msg(ANA, "quiero una cita"))
    agent.handle_message(tap_msg(ANA, "svc:svc_corte"))
    agent.handle_message(tap_msg(ANA, "date:2026-09-24"))
    agent.handle_message(tap_msg(ANA, bad_time))
    conv = store.get_conversation(ANA)
    assert conv.state == "booking_time" and "time" not in conv.draft
    assert wa.outbound()[-2]["body"] == EXPIRED_ES


def test_stale_button_after_the_flow_moved_on_is_rejected(agent, store, wa):
    book_until_name(agent, ANA)  # now waiting for the name
    agent.handle_message(tap_msg(ANA, "svc:svc_tinte"))  # old service list
    agent.handle_message(tap_msg(ANA, "date:2026-09-25"))  # old date list
    conv = store.get_conversation(ANA)
    assert conv.state == "booking_name"
    assert conv.draft["service_id"] == "svc_corte" and conv.draft["date"] == "2026-09-24"
    assert wa.last("text")["body"] == t("ask_name", "es")  # re-prompted the current step


def test_confirm_button_from_a_finished_booking_does_not_double_book(agent, store, wa):
    book(agent, ANA, "Ana")
    agent.handle_message(tap_msg(ANA, "confirm:yes"))
    assert len(store.list_bookings()) == 1
    assert wa.outbound()[-2]["body"] == EXPIRED_ES


def test_slot_that_became_past_is_rechecked_at_confirmation(agent, store, wa, clock):
    book_until_name(agent, ANA, time="time:09:00")
    agent.handle_message(text_msg(ANA, "Ana"))
    clock.set(datetime(2026, 9, 24, 9, 30, tzinfo=TZ))  # she went for coffee
    agent.handle_message(tap_msg(ANA, "confirm:yes"))
    assert store.list_bookings() == []
    assert store.get_conversation(ANA).state == "booking_date"


# ------------------------------------------------------------ calendar
def test_friday_offers_19_00_through_pagination(agent, store, wa):
    """Audit BUG G: Friday has 11 slots but only 10 list rows were sent."""
    agent.handle_message(text_msg(ANA, "quiero una cita"))
    agent.handle_message(tap_msg(ANA, "svc:svc_corte"))
    agent.handle_message(tap_msg(ANA, "date:2026-09-25"))
    first = wa.last("list")["rows"]
    assert len(first) == 10
    assert [r["id"] for r in first[:9]] == [f"time:{h:02d}:00" for h in range(9, 18)]
    assert first[9]["id"] == "times_page:2026-09-25@9" and first[9]["title"] == "Más horarios ➡️"

    agent.handle_message(tap_msg(ANA, "times_page:2026-09-25@9"))
    assert wa.last_options() == ["time:18:00", "time:19:00"]
    agent.handle_message(tap_msg(ANA, "time:19:00"))
    assert store.get_conversation(ANA).draft["time"] == "19:00"


def test_today_is_hidden_once_its_last_slot_has_passed(store, kb, clock):
    clock.set(datetime(2026, 9, 24, 18, 30, tzinfo=TZ))  # Thursday, after the 18:00 slot
    wa = FakeWhatsApp()
    agent = Agent(settings=TEST_SETTINGS, wa=wa, store=store, kb=kb, llm=OfflineLLM(), clock=clock)
    agent.handle_message(text_msg(ANA, "quiero una cita"))
    agent.handle_message(tap_msg(ANA, "svc:svc_corte"))
    rows = wa.last("list")["rows"]
    assert rows[0]["id"] == "date:2026-09-25" and rows[0]["description"] == "Mañana"
    assert "date:2026-09-24" not in wa.last_options()
    assert "date:2026-09-27" not in wa.last_options()  # Sunday: closed
    agent.handle_message(tap_msg(ANA, "date:2026-09-24"))
    assert wa.outbound()[-2]["body"] == EXPIRED_ES


def test_fully_booked_day_is_not_offered(agent, store, wa):
    for hour in range(8, 18):  # Saturday 26: 08:00-17:00
        store.create_booking("x", "X", "svc_corte", "Corte", "2026-09-26", f"{hour:02d}:00")
    agent.handle_message(text_msg(ANA, "quiero una cita"))
    agent.handle_message(tap_msg(ANA, "svc:svc_corte"))
    assert "date:2026-09-26" not in wa.last_options()
    assert len(wa.last_options()) == 7


# ------------------------------------------------------------ language
def test_english_flow_stays_english(agent, store, wa):
    """Audit BUG A: date titles, times and names used to flip the language to es."""
    agent.handle_message(text_msg(ANA, "I want to book an appointment"))
    agent.handle_message(tap_msg(ANA, "svc:svc_corte", "Haircut"))
    agent.handle_message(tap_msg(ANA, "date:2026-09-24", "Thu 24 Sep"))
    assert wa.last("list")["body"] == "What time suits you on Thu 24 Sep?"
    agent.handle_message(tap_msg(ANA, "time:11:00", "11:00"))
    agent.handle_message(text_msg(ANA, "José Pérez"))
    assert wa.last("buttons")["body"].startswith("Let's confirm your appointment")
    agent.handle_message(tap_msg(ANA, "confirm:yes", "Confirm ✅"))
    assert store.get_conversation(ANA).lang == "en"
    assert wa.last("text")["body"].startswith("All set, José Pérez!")


def test_neutral_text_does_not_change_language(agent, store):
    agent.handle_message(text_msg(ANA, "hello"))
    for neutral in ("ok", "10:00", "Thu 24 Sep", "👍"):
        agent.handle_message(text_msg(ANA, neutral))
        assert store.get_conversation(ANA).lang == "en", neutral
    agent.handle_message(text_msg(ANA, "hola, gracias"))
    assert store.get_conversation(ANA).lang == "es"


# ------------------------------------------------------------ name step
def test_cancel_word_at_the_name_step_cancels_instead_of_becoming_the_name(agent, store, wa):
    """Audit: typing 'cancelar' booked the appointment under the name 'cancelar'."""
    book_until_name(agent, ANA, time="time:11:00")
    agent.handle_message(text_msg(ANA, "cancelar"))
    conv = store.get_conversation(ANA)
    assert conv.state == "idle" and conv.draft == {}
    assert wa.last("text")["body"] == t("booking_cancelled", "es")
    assert store.list_bookings() == []


def test_name_step_rejects_non_names_and_answers_questions(agent, store, wa):
    book_until_name(agent, ANA)
    agent.handle_message(text_msg(ANA, "12345"))
    assert wa.last("text")["body"] == t("name_invalid", "es")
    agent.handle_message(text_msg(ANA, "¿tienen estacionamiento?"))
    texts = [m["body"] for m in wa.outbound()[-2:]]
    assert "estacionamiento" in texts[0].lower()
    assert texts[1] == t("ask_name", "es")
    assert store.get_conversation(ANA).state == "booking_name"
    agent.handle_message(text_msg(ANA, "Ana María"))
    assert store.get_conversation(ANA).draft["name"] == "Ana María"


# ------------------------------------------------------------ typed options
def test_typed_answers_select_the_offered_options(agent, store, wa):
    agent.handle_message(text_msg(ANA, "quiero una cita"))
    agent.handle_message(text_msg(ANA, "2"))  # second service
    assert store.get_conversation(ANA).draft["service_id"] == "svc_corte_barba"
    agent.handle_message(text_msg(ANA, "mañana"))  # description of Fri 25
    assert store.get_conversation(ANA).draft["date"] == "2026-09-25"
    agent.handle_message(text_msg(ANA, "4pm"))
    assert store.get_conversation(ANA).draft["time"] == "16:00"
    agent.handle_message(text_msg(ANA, "Carla"))
    agent.handle_message(text_msg(ANA, "sí"))
    assert store.booked_times("2026-09-25") == {"16:00"}


def test_weekday_name_selects_that_day(agent, store, wa):
    agent.handle_message(text_msg(ANA, "quiero una cita"))
    agent.handle_message(tap_msg(ANA, "svc:svc_corte"))
    agent.handle_message(text_msg(ANA, "sábado"))
    assert store.get_conversation(ANA).draft["date"] == "2026-09-26"


# ------------------------------------------------ cancel / reschedule
def test_customer_cancels_their_appointment(agent, store, wa):
    """Audit BUG D: 'I want to cancel my appointment' started a NEW booking."""
    book(agent, ANA, "Ana")
    agent.handle_message(text_msg(ANA, "I want to cancel my appointment"))
    conv = store.get_conversation(ANA)
    assert conv.state == "manage_action" and conv.lang == "en"
    booking_id = store.list_bookings()[0]["id"]
    assert wa.last_options() == [f"appt_cancel:{booking_id}", f"appt_resched:{booking_id}", f"appt_keep:{booking_id}"]

    agent.handle_message(tap_msg(ANA, f"appt_cancel:{booking_id}"))
    assert wa.last_options() == [f"appt_cancel_yes:{booking_id}", f"appt_keep:{booking_id}"]
    agent.handle_message(tap_msg(ANA, f"appt_cancel_yes:{booking_id}"))

    assert store.get_booking(booking_id)["status"] == "cancelled"
    assert store.get_booking(booking_id)["cancelled_by"] == "customer"
    assert store.booked_times("2026-09-24") == set()
    assert wa.last("text")["body"].startswith("Done, I cancelled your Haircut appointment on Thu 24 Sep at 10:00")
    assert store.get_conversation(ANA).state == "idle"


def test_spanish_cancel_phrase_and_keep_button(agent, store, wa):
    book(agent, ANA, "Ana")
    agent.handle_message(text_msg(ANA, "cancelar mi cita"))
    booking_id = store.list_bookings()[0]["id"]
    agent.handle_message(text_msg(ANA, "cancelar"))  # typed = "Cancelar cita" button
    agent.handle_message(text_msg(ANA, "no"))  # typed = "No, mantener"
    assert store.get_booking(booking_id)["status"] == "confirmed"
    assert wa.last("text")["body"] == t("appt_kept", "es")


def test_customer_reschedules_atomically(agent, store, wa):
    book(agent, ANA, "Ana")
    booking_id = store.list_bookings()[0]["id"]
    agent.handle_message(text_msg(ANA, "quiero reprogramar mi cita"))
    agent.handle_message(tap_msg(ANA, f"appt_resched:{booking_id}"))
    assert wa.last("list")["body"] == "¿A qué día quieres mover tu cita de Corte de cabello?"
    agent.handle_message(tap_msg(ANA, "date:2026-09-26"))
    agent.handle_message(tap_msg(ANA, "time:12:00"))
    # The name is known: straight to the confirmation of the change.
    body = wa.last("buttons")["body"]
    assert "Antes: Jue 24 sep a las 10:00" in body and "Ahora: Sáb 26 sep a las 12:00" in body
    agent.handle_message(tap_msg(ANA, "confirm:yes"))

    moved = store.get_booking(booking_id)
    assert (moved["date"], moved["time"], moved["status"]) == ("2026-09-26", "12:00", "confirmed")
    assert len(store.list_bookings(status=None)) == 1  # same row, not a copy
    assert store.booked_times("2026-09-24") == set()
    assert wa.last("text")["body"].startswith("¡Listo, Ana! 🎉 Tu cita de Corte de cabello ahora es el Sáb 26 sep")


def test_reschedule_into_a_slot_taken_meanwhile_is_refused(agent, store, wa):
    book(agent, ANA, "Ana")
    booking_id = store.list_bookings()[0]["id"]
    agent.handle_message(text_msg(ANA, "reprogramar"))
    agent.handle_message(tap_msg(ANA, f"appt_resched:{booking_id}"))
    agent.handle_message(tap_msg(ANA, "date:2026-09-26"))
    agent.handle_message(tap_msg(ANA, "time:12:00"))
    store.create_booking(LUIS, "Luis", "svc_corte", "Corte", "2026-09-26", "12:00")
    agent.handle_message(tap_msg(ANA, "confirm:yes"))
    assert store.get_booking(booking_id)["date"] == "2026-09-24"  # untouched
    assert store.get_conversation(ANA).state == "booking_time"
    assert wa.outbound()[-2]["body"].startswith("¡Uy!")


def test_cancelling_a_reschedule_keeps_the_original(agent, store, wa):
    book(agent, ANA, "Ana")
    booking_id = store.list_bookings()[0]["id"]
    agent.handle_message(text_msg(ANA, "reprogramar"))
    agent.handle_message(tap_msg(ANA, f"appt_resched:{booking_id}"))
    agent.handle_message(tap_msg(ANA, "date:2026-09-26"))
    agent.handle_message(tap_msg(ANA, "time:12:00"))
    agent.handle_message(tap_msg(ANA, "confirm:cancel"))
    assert store.get_booking(booking_id)["date"] == "2026-09-24"
    assert wa.last("text")["body"] == "Sin problema, tu cita se mantiene el Jue 24 sep a las 10:00. 👍"


def test_several_bookings_are_listed_and_stale_ids_rejected(agent, store, wa):
    book(agent, ANA, "Ana")
    book(agent, ANA, "Ana", day="date:2026-09-25", time="time:15:00")
    agent.handle_message(text_msg(ANA, "mis citas"))
    ids = [b["id"] for b in store.list_bookings()]
    assert store.get_conversation(ANA).state == "manage_select"
    assert wa.last_options() == [f"appt:{i}" for i in ids]
    assert [r["title"] for r in wa.last("list")["rows"]] == ["Jue 24 sep · 10:00", "Vie 25 sep · 15:00"]

    agent.handle_message(tap_msg(ANA, f"appt:{ids[1]}"))
    # A cancel button for the *other* booking is stale here.
    agent.handle_message(tap_msg(ANA, f"appt_cancel_yes:{ids[0]}"))
    assert store.get_booking(ids[0])["status"] == "confirmed"
    assert wa.outbound()[-2]["body"] == EXPIRED_ES


def test_someone_elses_booking_cannot_be_selected(agent, store, wa):
    book(agent, LUIS, "Luis")
    book(agent, ANA, "Ana", time="time:11:00")
    book(agent, ANA, "Ana", time="time:12:00")
    luis_id = store.list_bookings()[0]["id"]
    agent.handle_message(text_msg(ANA, "mis citas"))
    agent.handle_message(tap_msg(ANA, f"appt:{luis_id}"))
    assert store.get_conversation(ANA).state == "manage_select"
    assert store.get_booking(luis_id)["status"] == "confirmed"


def test_manage_without_bookings_offers_to_book(agent, store, wa):
    agent.handle_message(text_msg(ANA, "mis citas"))
    assert wa.last_options() == ["menu:booking", "menu:main"]
    assert wa.last("buttons")["body"] == t("manage_none", "es")


def test_menu_offers_my_appointments_when_there_is_one(agent, store, wa):
    book(agent, ANA, "Ana")
    agent.handle_message(text_msg(ANA, "hola"))
    assert wa.last_options() == ["menu:booking", "menu:manage", "menu:info", "menu:human"]
    agent.handle_message(tap_msg(ANA, "menu:manage"))
    assert store.get_conversation(ANA).state == "manage_action"
