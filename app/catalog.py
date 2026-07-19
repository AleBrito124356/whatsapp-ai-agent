"""Business catalog, opening hours, and bilingual UI strings.

Everything a small business would tweak lives here: the service menu (must stay
in sync with kb/services.md and kb/prices.md), the weekly schedule that drives
slot generation, and the EN/ES copy the bot sends. No LLM is required for any of
the fixed conversational scaffolding, only for free-text FAQ answers.
"""

from __future__ import annotations

from typing import Optional

# --- Service menu -----------------------------------------------------------
# Keep ids stable; they are stored on bookings and encoded in interactive reply
# payloads (e.g. "svc:svc_corte"). Titles must be <= 24 chars for WhatsApp rows.
SERVICES: list[dict] = [
    {"id": "svc_corte", "es": "Corte de cabello", "en": "Haircut", "price": 12, "minutes": 30},
    {"id": "svc_corte_barba", "es": "Corte + barba", "en": "Haircut + beard", "price": 18, "minutes": 45},
    {"id": "svc_afeitado", "es": "Afeitado clásico", "en": "Classic shave", "price": 10, "minutes": 30},
    {"id": "svc_barba", "es": "Arreglo de barba", "en": "Beard trim", "price": 8, "minutes": 20},
    {"id": "svc_infantil", "es": "Corte infantil", "en": "Kids haircut", "price": 9, "minutes": 30},
    {"id": "svc_tinte", "es": "Tinte / color", "en": "Hair color", "price": 25, "minutes": 60},
]

_SERVICE_BY_ID = {s["id"]: s for s in SERVICES}


def get_service(service_id: str) -> Optional[dict]:
    return _SERVICE_BY_ID.get(service_id)


def service_label(service_id: str, lang: str) -> str:
    svc = _SERVICE_BY_ID.get(service_id)
    if not svc:
        return service_id
    return svc.get(lang, svc["es"])


# --- Opening hours ----------------------------------------------------------
# datetime.weekday(): Monday=0 .. Sunday=6. Value is (open_hour, close_hour) in
# 24h local time, or None when closed. Slots are generated on the hour.
WEEKLY_HOURS: dict[int, Optional[tuple[int, int]]] = {
    0: (9, 19),   # Mon
    1: (9, 19),   # Tue
    2: (9, 19),   # Wed
    3: (9, 19),   # Thu
    4: (9, 20),   # Fri
    5: (8, 18),   # Sat
    6: None,      # Sun (closed)
}

DAY_ABBR = {
    "es": ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}
MONTH_ABBR = {
    "es": ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}


# --- Bilingual copy ---------------------------------------------------------
# Values are (es, en). Use ``t(key, lang, **kwargs)`` to render with .format().
STRINGS: dict[str, tuple[str, str]] = {
    "menu_body": (
        "¡Hola! 👋 Soy el asistente de {business}. ¿En qué te ayudo hoy?",
        "Hi! 👋 I'm the assistant for {business}. How can I help today?",
    ),
    "menu_book": ("Reservar cita", "Book appointment"),
    "menu_info": ("Info y precios", "Info & prices"),
    "menu_human": ("Hablar con alguien", "Talk to a person"),
    "info_prompt": (
        "Claro. Pregúntame lo que quieras: precios, horarios, ubicación, "
        "servicios o promociones. ✂️",
        "Sure. Ask me anything: prices, hours, location, services or promos. ✂️",
    ),
    "choose_service": (
        "Perfecto. ¿Qué servicio te gustaría reservar?",
        "Great. Which service would you like to book?",
    ),
    "service_button": ("Ver servicios", "See services"),
    "services_section": ("Servicios", "Services"),
    "choose_date": (
        "¿Para qué día? Estos son los próximos días disponibles:",
        "Which day works? Here are the next available days:",
    ),
    "dates_button": ("Elegir día", "Pick a day"),
    "dates_section": ("Días", "Days"),
    "today": ("Hoy", "Today"),
    "tomorrow": ("Mañana", "Tomorrow"),
    "choose_time": (
        "¿A qué hora te viene bien el {date}?",
        "What time suits you on {date}?",
    ),
    "times_button": ("Elegir hora", "Pick a time"),
    "times_section": ("Horarios", "Times"),
    "no_slots": (
        "No quedan horarios libres ese día. Elige otro, por favor:",
        "No free slots left that day. Please pick another one:",
    ),
    "ask_name": (
        "Ya casi. ¿A nombre de quién agendo la cita?",
        "Almost done. What name should I book it under?",
    ),
    "confirm_body": (
        "Confirmemos tu cita:\n\n"
        "✂️ Servicio: {service}\n"
        "📅 Fecha: {date}\n"
        "🕐 Hora: {time}\n"
        "🙍 Nombre: {name}\n\n"
        "¿Todo correcto?",
        "Let's confirm your appointment:\n\n"
        "✂️ Service: {service}\n"
        "📅 Date: {date}\n"
        "🕐 Time: {time}\n"
        "🙍 Name: {name}\n\n"
        "Is everything correct?",
    ),
    "confirm_yes": ("Confirmar ✅", "Confirm ✅"),
    "confirm_edit": ("Cambiar", "Change"),
    "confirm_cancel": ("Cancelar", "Cancel"),
    "booking_done": (
        "¡Listo, {name}! 🎉 Tu cita para {service} quedó agendada el {date} a las "
        "{time}. Te esperamos en {business}. Si necesitas cambiarla, escríbeme.",
        "All set, {name}! 🎉 Your {service} appointment is booked for {date} at "
        "{time}. See you at {business}. Message me if you need to change it.",
    ),
    "booking_cancelled": (
        "Sin problema, cancelé la reserva. Aquí estoy cuando quieras agendar. 👋",
        "No problem, I cancelled it. I'm here whenever you'd like to book. 👋",
    ),
    "booking_reset": (
        "Empecemos de nuevo. ",
        "Let's start over. ",
    ),
    "handoff": (
        "Con gusto. Un miembro del equipo de {business} continuará esta "
        "conversación contigo lo antes posible. 🙌 Mientras tanto puedes dejar "
        "tu mensaje aquí.",
        "Of course. Someone from the {business} team will pick up this chat as "
        "soon as possible. 🙌 Feel free to leave your message here.",
    ),
    "image_received": (
        "Recibí tu imagen, gracias. 📸 Un momento mientras un miembro del equipo "
        "la revisa.",
        "Got your image, thanks. 📸 One moment while a team member reviews it.",
    ),
    "audio_unsupported": (
        "Recibí tu nota de voz pero no pude transcribirla en este momento. "
        "¿Puedes escribirme el mensaje? 🙏",
        "I got your voice note but couldn't transcribe it right now. Could you "
        "type your message instead? 🙏",
    ),
    "audio_heard": (
        "Escuché tu nota de voz: \"{text}\"",
        "Here's what I heard: \"{text}\"",
    ),
    "fallback_prefix": (
        "Esto es lo que encontré:",
        "Here's what I found:",
    ),
    "no_answer": (
        "No estoy seguro de eso. Te paso con una persona del equipo de {business} "
        "para ayudarte mejor. 🙌",
        "I'm not sure about that. Let me connect you with someone from the "
        "{business} team who can help. 🙌",
    ),
    "reset_ack": (
        "Listo, volvamos a empezar. 👋",
        "Done, let's start fresh. 👋",
    ),
}


def t(key: str, lang: str, **kwargs) -> str:
    es, en = STRINGS[key]
    template = en if lang == "en" else es
    return template.format(**kwargs) if kwargs else template
