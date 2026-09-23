"""FAQ answers end to end: catalog facts, bilingual retrieval, LLM grounding."""

from __future__ import annotations

from dataclasses import replace

from app.agent import Agent

from .conftest import TEST_SETTINGS, OfflineLLM, RecordingLLM, tap_msg, text_msg

ANA = "50760000001"


def ask(agent, question, wa_id=ANA):
    agent.handle_message(text_msg(wa_id, question))


def test_spanish_price_question_is_answered_from_the_catalog(agent, store, wa):
    """Audit: 'cuánto cuesta un corte' used to return the *duration* article."""
    ask(agent, "hola, cuánto cuesta un corte?")
    body = wa.last("text")["body"]
    assert body.startswith("✂️ Corte de cabello: $12 · 30 min")
    assert store.open_handoffs() == []


def test_english_price_question_gets_an_english_answer_without_handoff(agent, store, wa):
    """Audit: 'how much is a haircut?' found nothing offline and handed off."""
    ask(agent, "how much is a haircut?")
    body = wa.last("text")["body"]
    assert "Haircut: $12" in body and "To book, type *book*" in body
    assert store.get_conversation(ANA).lang == "en"
    assert store.open_handoffs() == []


def test_full_price_list_and_unknown_items(agent, wa):
    ask(agent, "¿cuáles son sus precios?")
    body = wa.last("text")["body"]
    assert body.startswith("Nuestros precios (USD):") and body.count("✂️") == 6
    # Not in the catalog: falls back to the knowledge base (prices.md has it).
    ask(agent, "¿cuánto cuesta el diseño de líneas?")
    assert "desde $3" in wa.last("text")["body"]


def test_hours_questions(agent, wa):
    ask(agent, "¿a qué hora cierran el viernes?")
    assert wa.last("text")["body"] == "🕐 El viernes abrimos de 9:00 a. m. a 8:00 p. m. (última cita: 7:00 p. m.)."
    ask(agent, "what time do you close on friday?")
    assert "8:00 p.m." in wa.last("text")["body"]
    ask(agent, "are you open on sundays?")
    assert wa.last("text")["body"] == "🕐 We're closed on Sundays."
    ask(agent, "are you open today?")  # frozen clock: Thursday
    assert "On Thursday we're open from 9:00 a.m. to 7:00 p.m." in wa.last("text")["body"]


def test_english_questions_retrieve_spanish_articles(agent, store, wa):
    ask(agent, "where are you located?")
    body = wa.last("text")["body"]
    assert body.startswith("Here's what I found:") and "Calle 50" in body
    ask(agent, "do you accept credit cards?")
    assert "Visa" in wa.last("text")["body"]
    ask(agent, "¿cuánto dura un corte?")
    assert "30 minutos" in wa.last("text")["body"]
    assert store.open_handoffs() == []


def test_llm_gets_catalog_facts_and_the_configured_business(store, kb, wa, clock):
    llm = RecordingLLM(answer="Un corte cuesta $12.")
    settings = replace(TEST_SETTINGS, business_description="a family barbershop in David, Chiriquí")
    agent = Agent(settings=settings, wa=wa, store=store, kb=kb, llm=llm, clock=clock)
    ask(agent, "¿cuánto cuesta un corte?")
    assert wa.last("text")["body"] == "Un corte cuesta $12."

    answer_call = llm.calls[-1]
    system, user = answer_call[0]["content"], answer_call[-1]["content"]
    assert "a family barbershop in David, Chiriquí" in system and "Panama City" not in system
    assert "BUSINESS FACTS" in user and "Corte de cabello (Haircut): $12, 30 min" in user
    assert "Friday: 9:00 a.m. - 8:00 p.m." in user
    assert "Lista de precios" in user  # the KB context is still there


def test_llm_failure_falls_back_to_catalog_facts(store, kb, wa, clock):
    llm = RecordingLLM(answer=None)
    agent = Agent(settings=TEST_SETTINGS, wa=wa, store=store, kb=kb, llm=llm, clock=clock)
    ask(agent, "how much is a beard trim?")
    assert wa.last("text")["body"].startswith("✂️ Beard trim: $8 · 20 min")


def test_llm_intent_can_route_to_manage(store, kb, wa, clock):
    llm = RecordingLLM(intent="manage")
    agent = Agent(settings=TEST_SETTINGS, wa=wa, store=store, kb=kb, llm=llm, clock=clock)
    ask(agent, "necesito ver lo que tengo agendado")
    assert wa.last_options() == ["menu:booking", "menu:main"]  # no bookings yet


def test_offline_agent_never_calls_the_llm(agent, wa):
    assert isinstance(agent.llm, OfflineLLM)
    agent.handle_message(tap_msg(ANA, "menu:info"))
    assert wa.last("text")["body"].startswith("Claro. Pregúntame")
