"""Deterministic answers from the catalog: prices and opening hours.

``app/catalog.py`` (SERVICES, WEEKLY_HOURS) is the source of truth for what a
service costs and when the shop is open. The booking flow already uses it;
this module makes the FAQ use it too, so "¿cuánto cuesta un corte?" or
"what time do you close on Friday?" get an exact answer in the customer's
language, offline, instead of whichever knowledge-base paragraph scores best.
When an LLM is configured, :func:`facts_context` is passed to it as
authoritative context.

Anything the catalog does not know (the price of a line design, holiday
hours...) returns ``None`` and falls through to knowledge-base retrieval.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date as date_cls
from datetime import timedelta
from typing import Optional

from .catalog import SERVICES, WEEKLY_HOURS, t

_WORD_RE = re.compile(r"[a-z0-9]+")

PRICE_WORDS = {
    "precio", "precios", "cuesta", "cuestan", "vale", "valen", "cobran", "cobras",
    "tarifa", "tarifas", "costo", "costos", "price", "prices", "pricing", "cost",
    "costs", "charge", "fee", "fees",
}
PRICE_PHRASES = ("how much", "cuanto es", "cuanto sale", "cuanto seria", "cuanto cobran")
HOURS_WORDS = {
    "horario", "horarios", "abren", "abre", "abierto", "abiertos", "cierran", "cierra",
    "cerrado", "cerrados", "atienden", "hours", "open", "opening", "close", "closes",
    "closing", "closed",
}
HOURS_PHRASES = ("a que hora", "what time", "que horario")
# Questions the catalog cannot answer even if they mention hours.
HOURS_OUT_OF_SCOPE = {"feriado", "feriados", "holiday", "holidays", "whatsapp", "espera", "wait"}

DAY_WORDS = {
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4,
    "sabado": 5, "sabados": 5, "domingo": 6, "domingos": 6,
    "monday": 0, "mondays": 0, "tuesday": 1, "tuesdays": 1, "wednesday": 2,
    "wednesdays": 2, "thursday": 3, "thursdays": 3, "friday": 4, "fridays": 4,
    "saturday": 5, "saturdays": 5, "sunday": 6, "sundays": 6,
}
RELATIVE_DAYS = {"hoy": 0, "today": 0, "ahora": 0, "now": 0, "manana": 1, "tomorrow": 1}

# Words that do not name a priced item: question words, articles, courtesy.
_FILLER = {
    "a", "al", "algo", "and", "are", "cada", "can", "con", "cual", "cuales", "cuanto",
    "cuanta", "de", "del", "do", "does", "el", "en", "es", "esta", "for", "hay", "hi",
    "hola", "how", "i", "is", "it", "la", "las", "list", "lista", "lo", "los", "me",
    "mi", "much", "of", "para", "por", "please", "que", "quisiera", "saber", "sus",
    "servicio", "servicios", "service", "services", "son", "su", "the", "their",
    "tienen", "todo", "todos", "tu", "un", "una", "what", "whats", "would", "you",
    "your", "y", "o", "or", "buenas", "buenos", "dias", "tardes", "hello", "gracias",
    "favor", "info", "informacion", "sobre", "about", "tell", "dime", "decir",
    "like", "know", "want", "quiero", "ustedes", "usted", "s", "an", "be", "will",
    "current", "actuales", "normal", "regular", "aprox", "aproximadamente",
}

DAY_NAMES = {
    "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}


def _norm_words(text: str) -> list[str]:
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn").lower()
    return _WORD_RE.findall(text)


def _has_phrase(words: list[str], phrases) -> bool:
    joined = " " + " ".join(words) + " "
    return any(f" {p} " in joined for p in phrases)


def detect_services(text: str) -> list[dict]:
    """Services named in the text, longest alias first ("corte y barba" > "corte")."""
    joined = " " + " ".join(_norm_words(text)) + " "
    candidates = sorted(
        ((" ".join(_norm_words(alias)), svc) for svc in SERVICES for alias in svc.get("aliases", [])),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    found: list[dict] = []
    for alias, svc in candidates:
        needle = f" {alias} "
        if needle in joined:
            if svc not in found:
                found.append(svc)
            joined = joined.replace(needle, " # ")
    return found


def _alias_words() -> set[str]:
    return {w for svc in SERVICES for alias in svc.get("aliases", []) for w in _norm_words(alias)}


def _fmt_hour(hour: int, lang: str) -> str:
    h12 = hour % 12 or 12
    if lang == "en":
        return f"{h12}:00 {'a.m.' if hour < 12 else 'p.m.'}"
    return f"{h12}:00 {'a. m.' if hour < 12 else 'p. m.'}"


def _service_line(svc: dict, lang: str) -> str:
    return f"✂️ {svc.get(lang, svc['es'])}: ${svc['price']} · {svc['minutes']} min"


# -------------------------------------------------------------- prices
def is_price_question(text: str) -> bool:
    words = _norm_words(text)
    return bool(set(words) & PRICE_WORDS) or _has_phrase(words, PRICE_PHRASES)


def price_answer(text: str, lang: str) -> Optional[str]:
    if not is_price_question(text):
        return None
    words = _norm_words(text)
    services = detect_services(text)
    leftovers = set(words) - PRICE_WORDS - _FILLER - _alias_words()
    if not services and leftovers:
        return None  # asks about something the catalog does not price
    lines = [_service_line(svc, lang) for svc in (services or SERVICES)]
    header = [] if services else [t("price_header", lang)]
    return "\n".join(header + lines + ["", t("price_footer", lang)])


# --------------------------------------------------------------- hours
def is_hours_question(text: str) -> bool:
    words = _norm_words(text)
    return bool(set(words) & HOURS_WORDS) or _has_phrase(words, HOURS_PHRASES)


def _day_in(words: list[str], today: date_cls) -> Optional[int]:
    for word in words:
        if word in DAY_WORDS:
            return DAY_WORDS[word]
        if word in RELATIVE_DAYS:
            return (today + timedelta(days=RELATIVE_DAYS[word])).weekday()
    return None


def _day_line(weekday: int, lang: str) -> str:
    hours = WEEKLY_HOURS.get(weekday)
    name = DAY_NAMES[lang][weekday]
    if hours is None:
        plural = name + "s" if lang == "en" or name.endswith("o") else name
        return t("hours_closed_day", lang, days=plural)
    open_h, close_h = hours
    return t(
        "hours_day",
        lang,
        day=name,
        open=_fmt_hour(open_h, lang),
        close=_fmt_hour(close_h, lang),
        last=_fmt_hour(close_h - 1, lang),
    )


def weekly_hours_lines(lang: str) -> list[str]:
    """'Lunes a jueves: 9:00 a. m. - 7:00 p. m.' style lines, grouping equal days."""
    lines, start = [], 0
    for day in range(1, 8):
        if day < 7 and WEEKLY_HOURS.get(day) == WEEKLY_HOURS.get(start):
            continue
        names = DAY_NAMES[lang]
        label = names[start] if day - 1 == start else t("day_range", lang, first=names[start], last=names[day - 1])
        hours = WEEKLY_HOURS.get(start)
        span = t("closed", lang) if hours is None else f"{_fmt_hour(hours[0], lang)} - {_fmt_hour(hours[1], lang)}"
        lines.append(f"🕐 {label[0].upper()}{label[1:]}: {span}")
        start = day
    return lines


def hours_answer(text: str, lang: str, today: date_cls) -> Optional[str]:
    if not is_hours_question(text):
        return None
    words = _norm_words(text)
    if set(words) & HOURS_OUT_OF_SCOPE:
        return None
    day = _day_in(words, today)
    if day is not None:
        return _day_line(day, lang)
    return "\n".join([t("hours_header", lang)] + weekly_hours_lines(lang) + ["", t("hours_footer", lang)])


def answer(text: str, lang: str, today: date_cls) -> Optional[str]:
    """A catalog-backed answer for price or hours questions, else None."""
    return price_answer(text, lang) or hours_answer(text, lang, today)


# ------------------------------------------------------------ LLM context
def facts_context(lang: str = "en") -> str:
    """Authoritative business facts for the LLM prompt (always in English)."""
    services = "; ".join(
        f"{s['es']} ({s['en']}): ${s['price']}, {s['minutes']} min" for s in SERVICES
    )
    hours = "; ".join(line.replace("🕐 ", "") for line in weekly_hours_lines("en"))
    return (
        "BUSINESS FACTS (authoritative, prices in USD):\n"
        f"- Services: {services}.\n"
        f"- Opening hours: {hours}. The last appointment starts one hour before closing."
    )
