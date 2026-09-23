"""WhatsApp compliance basics: opt-out/opt-in, and never muting the bot by accident."""

from __future__ import annotations

from app.catalog import t

from .conftest import tap_msg, text_msg

ANA = "50760000001"


def test_baja_opts_out_and_silences_everything_until_alta(agent, store, wa):
    agent.handle_message(text_msg(ANA, "hola"))
    agent.handle_message(text_msg(ANA, "BAJA"))
    assert wa.last("text")["body"] == t("optout_ack", "es")
    assert store.get_conversation(ANA).opted_out is True

    wa.sent.clear()
    for later in ("hola", "quiero una cita", "¿cuánto cuesta?"):
        agent.handle_message(text_msg(ANA, later))
    agent.handle_message(tap_msg(ANA, "menu:booking"))
    assert wa.sent == []  # not even read receipts

    agent.handle_message(text_msg(ANA, "ALTA"))
    assert store.get_conversation(ANA).opted_out is False
    assert wa.outbound()[0]["body"] == t("optin_ack", "es")
    assert wa.last_options() == ["menu:booking", "menu:info", "menu:human"]


def test_stop_and_start_in_english(agent, store, wa):
    agent.handle_message(text_msg(ANA, "hello"))
    agent.handle_message(text_msg(ANA, "STOP"))
    assert wa.last("text")["body"] == t("optout_ack", "en")
    agent.handle_message(text_msg(ANA, "Start"))
    assert wa.outbound()[-2]["body"] == t("optin_ack", "en")


def test_unsubscribe_phrases(agent, store):
    for i, phrase in enumerate(("darme de baja", "Unsubscribe", "stop.", "cancelar suscripción")):
        wa_id = f"5076000010{i}"
        agent.handle_message(text_msg(wa_id, phrase))
        assert store.get_conversation(wa_id).opted_out is True, phrase


def test_words_inside_a_question_are_not_opt_outs(agent, store, wa):
    agent.handle_message(text_msg(ANA, "¿el local está en planta baja?"))
    assert store.get_conversation(ANA).opted_out is False
    agent.handle_message(text_msg(ANA, "can you stop by at 5?"))
    assert store.get_conversation(ANA).opted_out is False


def test_opt_out_during_handoff_closes_the_queue_entry(agent, store, wa):
    agent.handle_message(text_msg(ANA, "quiero hablar con una persona"))
    assert len(store.open_handoffs()) == 1
    agent.handle_message(text_msg(ANA, "baja"))
    conv = store.get_conversation(ANA)
    assert conv.opted_out is True and conv.handoff is False
    assert store.open_handoffs() == []


def test_opt_out_mid_booking_drops_the_draft(agent, store, wa):
    agent.handle_message(text_msg(ANA, "quiero una cita"))
    agent.handle_message(tap_msg(ANA, "svc:svc_corte"))
    agent.handle_message(text_msg(ANA, "STOP"))
    conv = store.get_conversation(ANA)
    assert conv.state == "idle" and conv.draft == {}


def test_unanswerable_question_offers_a_person_instead_of_muting_the_bot(agent, store, wa):
    """Audit: offline 'no match' created a permanent handoff and muted the bot."""
    agent.handle_message(text_msg(ANA, "¿venden guitarras eléctricas?"))
    last = wa.last("buttons")
    assert last["body"] == t("no_answer", "es", business="Barbería Studio Norte")
    assert wa.last_options() == ["menu:human", "menu:main"]
    assert store.open_handoffs() == []
    assert store.get_conversation(ANA).handoff is False

    # The bot keeps working...
    agent.handle_message(text_msg(ANA, "hola"))
    assert wa.last_options() == ["menu:booking", "menu:info", "menu:human"]
    # ...and the customer can still ask for a person.
    agent.handle_message(tap_msg(ANA, "menu:human"))
    assert store.open_handoffs()[0]["reason"] == "requested"
