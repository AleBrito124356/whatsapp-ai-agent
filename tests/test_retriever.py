"""BM25 retriever over the knowledge base."""

from __future__ import annotations


def test_kb_loaded(kb):
    # 8 markdown articles, each split into multiple sections.
    assert len(kb.chunks) >= 8
    sources = {c.source for c in kb.chunks}
    assert "prices.md" in sources
    assert "hours.md" in sources


def test_price_question_retrieves_prices(kb):
    hits = kb.search("cuánto cuesta un corte de cabello", k=3)
    assert hits
    joined = " ".join(h.source for h in hits)
    assert "prices.md" in joined or "services.md" in joined


def test_hours_question_retrieves_hours(kb):
    hits = kb.search("a qué hora abren el sábado", k=3)
    assert hits
    assert any(h.source == "hours.md" for h in hits)


def test_accent_insensitive_match(kb):
    # "ubicacion" (no accent) should still find location.md ("ubicación").
    hits = kb.search("cual es la ubicacion", k=3)
    assert any(h.source == "location.md" for h in hits)


def test_context_is_nonempty_string(kb):
    ctx = kb.context("promociones y descuentos", k=2)
    assert isinstance(ctx, str) and len(ctx) > 0
