"""Retrieval quality on a labelled, bilingual FAQ set (tests/data/faq_eval.json).

40 real-world questions (20 ES, 20 EN) with the knowledge-base section(s) that
answer them. The first release scored 18/40 top-1 (ES 15/20, EN 3/20: most
English questions retrieved nothing at all). The bar below keeps it honest.
"""

from __future__ import annotations

import json

from .conftest import REPO_ROOT

EVAL = json.loads((REPO_ROOT / "tests" / "data" / "faq_eval.json").read_text(encoding="utf-8"))["questions"]


def _hit(kb, item) -> tuple[bool, object]:
    hits = kb.search(item["q"], k=1)
    top = (hits[0].source, hits[0].title) if hits else None
    ok = top is not None and any(top[0] == src and sec in ("*", top[1]) for src, sec in item["expected"])
    return ok, top


def test_eval_set_is_balanced():
    langs = [item["lang"] for item in EVAL]
    assert len(EVAL) >= 30
    assert langs.count("es") >= 15 and langs.count("en") >= 15


def test_top1_accuracy_is_at_least_90_percent(kb):
    misses = []
    for item in EVAL:
        ok, top = _hit(kb, item)
        if not ok:
            misses.append(f"[{item['lang']}] {item['q']} -> {top}")
    accuracy = 1 - len(misses) / len(EVAL)
    assert accuracy >= 0.9, f"top-1 accuracy {accuracy:.2f}; misses:\n" + "\n".join(misses)


def test_each_language_is_served(kb):
    for lang in ("es", "en"):
        items = [i for i in EVAL if i["lang"] == lang]
        correct = sum(_hit(kb, i)[0] for i in items)
        assert correct / len(items) >= 0.85, (lang, correct, len(items))


def test_expected_sections_exist(kb):
    """Guard the labels themselves: every expected section is a real chunk."""
    sections = {(c.source, c.title) for c in kb.chunks}
    sources = {c.source for c in kb.chunks}
    for item in EVAL:
        for src, sec in item["expected"]:
            assert (src, sec) in sections if sec != "*" else src in sources, (item["q"], src, sec)


def test_lexicon_matches_plurals(kb):
    assert kb.search("is there a discount on mondays?", k=1)[0].title == "Lunes y martes de descuento"
    assert kb.expand("credit cards")["tarjeta"] > 0


def test_unrelated_questions_retrieve_nothing(kb):
    for question in ("do you sell guitars?", "¿tienen wifi?", "can I bring my dog?"):
        assert kb.search(question) == [], question
