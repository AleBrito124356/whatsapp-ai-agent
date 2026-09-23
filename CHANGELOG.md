# Changelog

## 1.1.0

The first release described several things the code did not do. This release implements them and adds a way to try everything offline.

### Booking

- A stale or forged button is now accepted only in the step that sent it. Its value is checked again against the live calendar: the service exists, the day is open, not in the past and at most 21 days ahead, and the time is a real slot. Before this, starting from idle, `svc:svc_tinte`, then a Sunday, then `03:00`, then `confirm:yes` stored a booking. Now the customer gets *"Esa opción ya no está disponible"* and the current step again.
- There can be only one confirmed booking per slot. A partial `UNIQUE` index enforces this, and the agent handles `SlotTaken`. Before this, two customers could both confirm 10:00.
- Time lists are paginated with a "Más horarios / More times" row, so Friday 19:00 is offered. "Hoy" and fully booked days are hidden.
- New **Mis citas / My appointments** flow: list, cancel (with confirmation) and reschedule (one atomic move). Before this, "cancelar mi cita" showed the menu and "I want to cancel my appointment" started a *new* booking.
- The name step handles reset and cancel words before taking the text as a name. Before this, "cancelar" was booked as the customer's name. Obvious non-names are rejected, and the WhatsApp profile name is offered as a button.
- Typed answers ("2", "10am", "mañana", "sábado", "sí") select the offered options.

### Language and FAQ

- The language is inferred only from free text that carries evidence. Before this, tapping a date such as "Thu 24 Sep" switched English customers to Spanish.
- Price and hours questions are answered exactly from `app/catalog.py`, in the customer's language (`app/facts.py`).
- Bilingual retrieval: stemming, a heading boost, the H1 title carried into every section, and the editable glossary `kb/_lexicon.json`. Top-1 accuracy on 40 labelled ES/EN questions went from 18/40 to 38/40.
- The LLM prompt uses the new `BUSINESS_DESCRIPTION` instead of a hardcoded "barbershop in Panama City", and it gets the catalog facts as authoritative context.
- Knowledge-base snippets are formatted for WhatsApp (bold headings, table rows) instead of raw Markdown.

### Handoff and compliance

- New staff API under `/admin` (bearer `ADMIN_TOKEN`): the handoff queue, transcripts, replies, resolve (the bot comes back), and bookings with cancellation. It enforces the 24-hour window (`409 template_required`) and opt-outs.
- BAJA, STOP, UNSUBSCRIBE and similar words opt the contact out, and ALTA or START opts them back in. Before this, "BAJA" returned the "planta baja" accessibility article and "STOP" opened a handoff.
- An unanswerable question now offers [Hablar con alguien] [Menú] instead of opening a handoff automatically, which used to mute the bot permanently.
- Every outbound message is recorded in the transcript, with `author` set to customer, bot or staff.

### Developer experience

- **Dry-run mode.** When WhatsApp credentials are empty or are the `.env.example` placeholders, messages go to an `outbox` table (`GET /dev/outbox`) instead of graph.facebook.com. Before this, `cp .env.example .env` made the app call the Graph API with a fake token.
- `python -m app chat` is a terminal simulator for the real agent. It supports `--script`, `--now`, `--offline` and staff commands. `python -m app bookings|handoffs` are quick database views.
- `scripts/send_webhook.py` prints the bot's actual reply and supports `--button`.
- Nothing is built at import time (`create_app`, `build_services`), so `pytest` no longer writes `data/bookings.sqlite`.
- Schema v2 with an in-place migration of v1 databases. Legacy double bookings are kept and marked `status='conflict'`.
- New `.dockerignore`, so a local `.env` is never copied into the Docker image.

### Behaviour changes to note when upgrading

- Interactive replies that do not belong to the current step are rejected.
- A conversation no longer goes into handoff when the knowledge base has no match.
- When the contact has upcoming bookings, the main menu is a list with four rows, because the fourth option "Mis citas" does not fit in three reply buttons.
- `app.deps` no longer builds singletons at import. Use `build_services()` or `default_services()`. The old attribute names still resolve lazily.
- `WhatsAppClient` now delivers through a transport. Without credentials it logs instead of calling Graph.

## 1.0.0

- Initial release.
