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
# Button titles must fit in 20 characters, list row titles in 24.
STRINGS: dict[str, tuple[str, str]] = {
    # ---------------------------------------------------------------- menu
    "menu_body": (
        "¡Hola! 👋 Soy el asistente de {business}. ¿En qué te ayudo hoy?",
        "Hi! 👋 I'm the assistant for {business}. How can I help today?",
    ),
    "menu_book": ("Reservar cita", "Book appointment"),
    "menu_manage": ("Mis citas", "My appointments"),
    "menu_info": ("Info y precios", "Info & prices"),
    "menu_human": ("Hablar con alguien", "Talk to a person"),
    "menu_main": ("Menú", "Menu"),
    "menu_button": ("Ver opciones", "See options"),
    "menu_section": ("Opciones", "Options"),
    "info_prompt": (
        "Claro. Pregúntame lo que quieras: precios, horarios, ubicación, "
        "servicios o promociones. ✂️",
        "Sure. Ask me anything: prices, hours, location, services or promos. ✂️",
    ),
    "option_expired": (
        "Esa opción ya no está disponible. 🙏 Te muestro las opciones actuales:",
        "That option is no longer available. 🙏 Here are the current ones:",
    ),
    # ------------------------------------------------------------- booking
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
    "more_times": ("Más horarios ➡️", "More times ➡️"),
    "more_times_desc": ("Después de las {time}", "After {time}"),
    "no_slots": (
        "No quedan horarios libres ese día. Elige otro, por favor:",
        "No free slots left that day. Please pick another one:",
    ),
    "no_days": (
        "Lo siento, no quedan horarios libres en las próximas semanas. "
        "Te paso con el equipo para buscar una opción.",
        "Sorry, there are no free slots in the coming weeks. Let me pass you "
        "to the team to find an option.",
    ),
    "slot_taken": (
        "¡Uy! Alguien acaba de reservar el {date} a las {time}. Elige otra hora, por favor:",
        "Oops! Someone just booked {date} at {time}. Please pick another time:",
    ),
    "ask_name": (
        "Ya casi. ¿A nombre de quién agendo la cita?",
        "Almost done. What name should I book it under?",
    ),
    "ask_name_button": (
        "Ya casi. ¿A nombre de quién agendo la cita? Escríbelo o toca tu nombre:",
        "Almost done. What name should I book it under? Type it or tap your name:",
    ),
    "name_invalid": (
        "Solo necesito tu nombre, por ejemplo: María López 🙂",
        "I just need your name, for example: Maria Lopez 🙂",
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
        "{time}. Te esperamos en {business}. Si necesitas cambiarla o cancelarla, "
        "escribe *mis citas*.",
        "All set, {name}! 🎉 Your {service} appointment is booked for {date} at "
        "{time}. See you at {business}. To change or cancel it, type *my appointments*.",
    ),
    "booking_cancelled": (
        "Sin problema, dejé la reserva sin hacer. Aquí estoy cuando quieras agendar. 👋",
        "No problem, I dropped that booking. I'm here whenever you'd like to book. 👋",
    ),
    "booking_reset": (
        "Empecemos de nuevo. ",
        "Let's start over. ",
    ),
    # ------------------------------------------------- my appointments
    "manage_none": (
        "No tienes citas próximas. ¿Quieres reservar una?",
        "You don't have any upcoming appointments. Would you like to book one?",
    ),
    "manage_choose": (
        "Estas son tus próximas citas. ¿Cuál quieres gestionar?",
        "Here are your upcoming appointments. Which one do you want to manage?",
    ),
    "manage_button": ("Ver citas", "See appointments"),
    "manage_section": ("Tus citas", "Your appointments"),
    "manage_detail": (
        "Tu cita:\n\n✂️ {service}\n📅 {date}\n🕐 {time}\n🙍 {name}\n\n¿Qué quieres hacer?",
        "Your appointment:\n\n✂️ {service}\n📅 {date}\n🕐 {time}\n🙍 {name}\n\n"
        "What would you like to do?",
    ),
    "appt_cancel_btn": ("Cancelar cita", "Cancel it"),
    "appt_resched_btn": ("Cambiar fecha", "Reschedule"),
    "appt_back_btn": ("Dejarla así", "Keep it"),
    "cancel_confirm_body": (
        "¿Seguro que quieres cancelar tu cita de {service} del {date} a las {time}?",
        "Are you sure you want to cancel your {service} appointment on {date} at {time}?",
    ),
    "cancel_yes": ("Sí, cancelar", "Yes, cancel"),
    "cancel_no": ("No, mantener", "No, keep it"),
    "appt_cancelled": (
        "Listo, cancelé tu cita de {service} del {date} a las {time}. "
        "¡Esperamos verte pronto! 👋",
        "Done, I cancelled your {service} appointment on {date} at {time}. "
        "Hope to see you soon! 👋",
    ),
    "appt_kept": (
        "Perfecto, tu cita sigue en pie. 👍",
        "Great, your appointment stays as it is. 👍",
    ),
    "appt_gone": (
        "Esa cita ya no está activa.",
        "That appointment is no longer active.",
    ),
    "reschedule_choose_date": (
        "¿A qué día quieres mover tu cita de {service}?",
        "Which day would you like to move your {service} appointment to?",
    ),
    "reschedule_confirm_body": (
        "Confirmemos el cambio:\n\n"
        "✂️ {service}\n"
        "❌ Antes: {old_date} a las {old_time}\n"
        "✅ Ahora: {date} a las {time}\n\n"
        "¿Todo correcto?",
        "Let's confirm the change:\n\n"
        "✂️ {service}\n"
        "❌ Before: {old_date} at {old_time}\n"
        "✅ New: {date} at {time}\n\n"
        "Is everything correct?",
    ),
    "rescheduled_done": (
        "¡Listo, {name}! 🎉 Tu cita de {service} ahora es el {date} a las {time}.",
        "All set, {name}! 🎉 Your {service} appointment is now on {date} at {time}.",
    ),
    "reschedule_aborted": (
        "Sin problema, tu cita se mantiene el {date} a las {time}. 👍",
        "No problem, your appointment stays on {date} at {time}. 👍",
    ),
    # --------------------------------------------------------- handoff
    "handoff": (
        "Con gusto. Un miembro del equipo de {business} continuará esta "
        "conversación contigo lo antes posible. 🙌 Mientras tanto puedes dejar "
        "tu mensaje aquí.",
        "Of course. Someone from the {business} team will pick up this chat as "
        "soon as possible. 🙌 Feel free to leave your message here.",
    ),
    "handoff_resolved": (
        "El asistente automático de {business} está de vuelta. 🤖 Si necesitas "
        "algo más, escribe *menú*.",
        "The {business} assistant is back. 🤖 If you need anything else, type *menu*.",
    ),
    "staff_cancelled": (
        "Hola {name}, el equipo de {business} canceló tu cita de {service} del "
        "{date} a las {time}. Escribe *reservar* para elegir otro horario.",
        "Hi {name}, the {business} team cancelled your {service} appointment on "
        "{date} at {time}. Type *book* to pick another slot.",
    ),
    "no_answer": (
        "No encontré información sobre eso. ¿Quieres que te comunique con una "
        "persona del equipo de {business}?",
        "I couldn't find anything about that. Would you like me to connect you "
        "with someone from the {business} team?",
    ),
    # ------------------------------------------------------ compliance
    "optout_ack": (
        "Listo, no te enviaremos más mensajes por este chat. Si cambias de "
        "opinión, escribe ALTA (o START). 👋",
        "Done: you won't get any more messages from us here. If you change your "
        "mind, reply START (or ALTA). 👋",
    ),
    "optin_ack": (
        "¡Bienvenido de vuelta! Volverás a recibir nuestras respuestas. 🙌",
        "Welcome back! You'll receive our replies again. 🙌",
    ),
    # ------------------------------------------------------------ media
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
    # -------------------------------------------------------------- faq
    "fallback_prefix": (
        "Esto es lo que encontré:",
        "Here's what I found:",
    ),
    "thanks_reply": (
        "¡Con gusto! 🙌 Si necesitas algo más, escribe *menú*.",
        "You're welcome! 🙌 If you need anything else, type *menu*.",
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
