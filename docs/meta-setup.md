# Meta WhatsApp Cloud API — setup guide

This walks you from zero to a working webhook, then to production. Budget about
20 minutes. You need a Facebook account and a phone number for testing that is
**not** already registered on the WhatsApp consumer app.

## 0. Try it before you touch Meta

You don't need a Meta app to see the agent work:

```bash
python -m app chat                                   # the real agent, in your terminal
uvicorn app.main:app --port 8000                     # the real server, in dry-run mode
python scripts/send_webhook.py "quiero una cita"     # signed webhook + the bot's reply
```

While `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID` are empty or still the
`.env.example` placeholders, the server runs in **dry-run**. Every reply is
stored in a local outbox (`GET /dev/outbox`) and nothing is sent to
graph.facebook.com. `/health` shows `"dry_run": true`. As soon as you paste
real credentials it goes live. Set `WHATSAPP_DRY_RUN=1` to keep a
configured server in dry-run. If `ADMIN_TOKEN` is set, `/dev/outbox` requires
it as a bearer token; the tester reads it from your environment.

## 1. Create a Meta app

1. Go to <https://developers.facebook.com/apps> and click **Create app**.
2. Choose the **Business** app type.
3. Give it a name, and finish creation.
4. On the app dashboard, find **WhatsApp** in the product list and click **Set up**.

This provisions a **test business phone number** (a Meta-hosted sender) and a
sandbox you can message immediately.

## 2. Grab your credentials

From **WhatsApp -> API Setup**:

- **Temporary access token** — a 24-hour token for testing. Copy it into
  `WHATSAPP_TOKEN`. For anything longer-lived, create a **System User token**
  (see step 6).
- **Phone number ID** — the numeric id under the test number. Copy it into
  `WHATSAPP_PHONE_NUMBER_ID`. This is *not* the phone number itself.
- Add your own phone number under **To** as an allowed recipient so you can
  receive test messages.

From **App Settings -> Basic**:

- **App secret** — click **Show** and copy it into `WHATSAPP_APP_SECRET`. This
  keys the `X-Hub-Signature-256` HMAC on every webhook, which this app verifies.

Pick any random string for `WHATSAPP_VERIFY_TOKEN` (for example a UUID). You will
type the same value into the webhook config in the next step.

## 3. Expose your local server

The webhook must be reachable over HTTPS. For local development, tunnel it:

```bash
uvicorn app.main:app --reload --port 8000
# in another terminal:
ngrok http 8000
```

Copy the `https://<something>.ngrok-free.app` URL ngrok prints.

## 4. Configure the webhook

In **WhatsApp -> Configuration -> Webhook**, click **Edit** and enter:

- **Callback URL**: `https://<your-public-host>/webhook`
- **Verify token**: the exact value you set for `WHATSAPP_VERIFY_TOKEN`

Click **Verify and save**. Meta sends a `GET /webhook` with
`hub.mode=subscribe`, `hub.verify_token`, and `hub.challenge`. This app checks
the token and echoes the challenge, so verification succeeds.

Then, under **Webhook fields**, **Subscribe** to `messages`. That is the only
field this agent needs.

## 5. Send a test message

Message your test number from the phone you allow-listed in step 2. You should
see the agent reply with the main menu. Watch the server logs to trace the flow.

You can also drive the agent locally without a phone using the included tester.
It computes a valid signature from your `WHATSAPP_APP_SECRET`. In dry-run it
prints the bot's reply; in live mode the reply goes to the WhatsApp number in
`--from`.

```bash
python scripts/send_webhook.py "hola, cuánto cuesta un corte?"
python scripts/send_webhook.py --interactive svc:svc_corte "Corte de cabello"
python scripts/send_webhook.py --button confirm:yes "Confirmar ✅"
```

## 6. Going to production

Before you can message customers who have not messaged you first, and before you
lose the test sandbox, you need to graduate the app:

1. **Add a real phone number.** In **WhatsApp -> API Setup**, add and verify a
   number you own that is not on consumer WhatsApp. This becomes your business
   sender.
2. **Create a System User token.** In **Business Settings -> Users -> System
   users**, create a system user, assign your app, and generate a token with the
   `whatsapp_business_messaging` and `whatsapp_business_management` permissions.
   Use it as `WHATSAPP_TOKEN` — it does not expire in 24 hours.
3. **Complete Business Verification** in Business Settings. Required to raise
   messaging limits and to send template messages at scale.
4. **Submit for App Review** if you need advanced access for multiple numbers or
   higher tiers.

## 7. The 24-hour customer service window

WhatsApp splits messaging into two modes:

- **Service (session) messages.** Once a user messages you, a **24-hour window**
  opens. Inside it you may reply freely with any message type — text, images,
  interactive lists and buttons. This agent operates entirely inside that
  window: it only ever *responds*.
- **Template (proactive) messages.** To message a user **outside** the 24-hour
  window — appointment reminders, promotions, follow-ups — you must send a
  **pre-approved message template**. Create templates in **WhatsApp -> Message
  templates**; each is reviewed by Meta (usually minutes to a day). Marketing
  templates also require the user to have opted in.

Practical implication for a booking bot: you can confirm an appointment
instantly (in-window), but a "your appointment is tomorrow at 10:00" reminder the
next day must go out as an approved **utility** template. Sending templates is
not implemented yet; this repo covers the in-window conversation. The staff
API (next section) refuses free-form replies outside the window with
`409 template_required`, so it never pretends to send something Meta would
reject.

## 7b. Staff workflow: working the handoff queue

When a customer asks for a person, complains or sends a photo, the conversation
is queued and the bot stops replying to them. Staff work the queue through the
`/admin` API:

1. Set `ADMIN_TOKEN` to a long random value and restart. Without it, `/admin`
   answers `503`.
2. List the queue:
   `curl -H "Authorization: Bearer $ADMIN_TOKEN" https://<host>/admin/handoffs`.
   Each entry shows the profile name, the reason, the last messages and
   whether the 24-hour window is still open (`window.open`, `window.closes_at`).
3. Read the whole conversation: `GET /admin/conversations/{wa_id}`.
4. Reply: `POST /admin/conversations/{wa_id}/reply` with `{"text": "..."}`.
   The message goes out through the same number and is saved with
   `author=staff`. Replying to a contact who is not in the queue takes the
   chat over, so the bot will not talk over you.
5. Hand it back: `POST /admin/handoffs/{wa_id}/resolve` (optionally
   `{"notify": false}`). The queue entry is closed and the bot answers the
   customer's next message again.

Bookings can be checked and cancelled from the same API: `GET /admin/bookings`
and `POST /admin/bookings/{id}/cancel`. The customer is notified when the
window allows it. To rehearse the whole routine offline, use
`python -m app chat` with `/staff <text>`, `/resolve`, `/queue` and
`/advance 25h`.

## 8. Opt-in and compliance

- Only message people who have **opted in** to hear from your business on
  WhatsApp. A user messaging you first counts as opt-in for the service window.
- Keep an easy opt-out. The agent honors BAJA, STOP, UNSUBSCRIBE and "darme de
  baja" (matched against the whole message, so "¿está en planta baja?" is not
  an opt-out). The contact gets one confirmation and then nothing, not even
  read receipts, until they send ALTA or START. The staff API also refuses to
  message them (`409 opted_out`).
- Do not send unsolicited marketing. Meta enforces this and can restrict your
  number.
- See Meta's WhatsApp Business Messaging Policy for the current rules.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Webhook verify fails | `WHATSAPP_VERIFY_TOKEN` mismatch, or server not reachable over HTTPS. |
| 403 on POST | Signature mismatch — `WHATSAPP_APP_SECRET` is wrong or the body was re-encoded by a proxy. |
| Replies never arrive | Wrong `WHATSAPP_PHONE_NUMBER_ID`, expired token, or recipient not allow-listed (in sandbox). |
| "Re-engagement" / 24h errors | You are trying to message outside the 24-hour window without a template. |
| `/health` says `"dry_run": true` | `WHATSAPP_TOKEN` or `WHATSAPP_PHONE_NUMBER_ID` is empty or still a placeholder (or `WHATSAPP_DRY_RUN=1`). Replies are in `/dev/outbox`. |
| `/admin/...` returns 503 | `ADMIN_TOKEN` is not set. |
