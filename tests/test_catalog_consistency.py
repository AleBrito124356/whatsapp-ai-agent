"""The catalog (app/catalog.py) and the knowledge base must not drift apart.

Prices, durations and opening hours live in two places: catalog.py drives the
booking flow and the bot's exact answers, and the kb/*.md articles feed the
retriever and the LLM. If someone edits one and forgets the other, these tests
fail.
"""

from __future__ import annotations

import re

from app.catalog import SERVICES, WEEKLY_HOURS, t
from app.facts import DAY_NAMES, _fmt_hour

from .conftest import REPO_ROOT

KB = REPO_ROOT / "kb"


def _table(markdown: str, heading: str) -> dict[str, str]:
    section = markdown.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 2 and not set(cells[0]) <= {"-", " "}:
            rows[cells[0]] = cells[1]
    return rows


def test_prices_table_matches_catalog():
    table = _table((KB / "prices.md").read_text(encoding="utf-8"), "Lista de precios")
    for svc in SERVICES:
        assert table.get(svc["es"]) == f"${svc['price']}", svc["id"]


def test_services_articles_match_catalog():
    text = (KB / "services.md").read_text(encoding="utf-8")
    for svc in SERVICES:
        body = text.split(f"## {svc['es']}\n", 1)[1].split("\n## ", 1)[0]
        assert re.search(rf"Duración[^:]*: {svc['minutes']} minutos", body), svc["id"]
        assert f"Precio: ${svc['price']}." in body, svc["id"]


def test_hours_table_matches_weekly_hours():
    table = _table((KB / "hours.md").read_text(encoding="utf-8"), "Horario de atención")
    for weekday, hours in WEEKLY_HOURS.items():
        name = DAY_NAMES["es"][weekday].capitalize()
        expected = "Cerrado" if hours is None else f"{_fmt_hour(hours[0], 'es')} - {_fmt_hour(hours[1], 'es')}"
        assert table.get(name) == expected, name


def test_service_titles_fit_whatsapp_limits():
    for svc in SERVICES:
        for lang in ("es", "en"):
            assert len(svc[lang]) <= 24, (svc["id"], lang)  # list row title


def test_button_labels_fit_whatsapp_limits():
    keys = [
        "menu_book", "menu_manage", "menu_info", "menu_human", "menu_main", "confirm_yes",
        "confirm_edit", "confirm_cancel", "appt_cancel_btn", "appt_resched_btn", "appt_back_btn",
        "cancel_yes", "cancel_no", "menu_button", "service_button", "dates_button", "times_button",
        "manage_button",
    ]
    for key in keys:
        for lang in ("es", "en"):
            assert len(t(key, lang)) <= 20, (key, lang)
