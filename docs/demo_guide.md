# Demo & Run Guide — Razorpay AI Revenue Recovery Agent

End-to-end walkthrough for running the agent live, triggering a failed
payment on demand, and watching the recovery happen in real time.

```
Webhook ──▶ Verify HMAC ──▶ Save FailedPayment ──▶ Groq diagnosis
                                                        │
        Dashboard ◀── SQLite audit ◀── Telegram DM ◀── Payment link (24h)
              ▲
              └──── payment_link.paid webhook ──▶ recovered ₹ recorded
```

---

## 0. Prerequisites

| Need | Where |
|---|---|
| Python 3.12 venv with deps | `.venv/` (see step 1) |
| `.env` with real creds | Razorpay test keys + webhook secret + Groq key + `TELEGRAM_BOT_TOKEN` |
| A Telegram chat that already messaged the bot | Bots cannot start chats. Open `@rzrrrrrrrrrbot` in Telegram and send `/start` once; get your chat_id via `getUpdates` |
| Optional public URL for real Razorpay webhooks | ngrok / cloudflared tunnel |

## 1. One-time setup

```bash
cd razorpay-recovery-agent
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in real values
```

`.env` keys that must be non-empty:

```
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...
GROQ_API_KEY=...
GROQ_MODEL=openai/gpt-oss-20b     # llama-3.1-8b-instant was retired
TELEGRAM_BOT_TOKEN=123456:ABC...
```

## 2. Start the API

```bash
uvicorn main:app --reload --port 8000
curl localhost:8000/health
# {"status":"ok","config_ok":true}
```

Structured JSON logs stream here — every line carries
`correlation_id = payment_id`.

## 3. Start the dashboard

New terminal:

```bash
streamlit run dashboard/app.py
```

You'll see KPI cards (failed/recovered/rate/in-progress/unrecoverable +
rupee exposure), failures-vs-outreach trend, recovery funnel, top failure
reasons, channel/outcome breakdowns and drill-down tables. Use **Auto-refresh**
to follow a demo live.

## 4. Trigger a failure — three ways

### A. Local simulator (fastest)

```bash
python scripts/simulate_failure.py                       # random pay_SIM_xxx
python scripts/simulate_failure.py \
    --amount 2499 --reason-code INSUFFICIENT_FUNDS --name "Aryan"
python scripts/simulate_failure.py --dry-run             # inspect, don't send
python scripts/simulate_failure.py --base-url https://<your-tunnel>/webhook
# Close the loop: mark a payment as paid via its recovery link
python scripts/simulate_failure.py --event payment_link.paid \
    --payment-id <the pay_SIM id above> --amount 2499
```

It signs the payload with `RAZORPAY_WEBHOOK_SECRET` exactly like Razorpay,
POSTs it, and prints next steps.

### B. Batch demo (aggregate metrics in one shot)

```bash
python scripts/batch_demo.py                             # 8 varied failures
python scripts/batch_demo.py --count 12 --recover-every 2   # 50% recover
python scripts/batch_demo.py --dry-run                   # plan only
```

Fires N failures cycling reasons (insufficient funds / card declined /
gateway / authentication), amounts, methods and customer names — then sends
matching `payment_link.paid` events for every Nth one so the dashboard shows
recovered ₹, recovery rate and the funnel filling up live.

### C. Through a tunnel (real-looking origin)

```bash
ngrok http 8000
# add the https URL in Razorpay Dashboard → Settings → Webhooks,
# events: payment.failed, secret = RAZORPAY_WEBHOOK_SECRET
# then make any test payment fail from Checkout / Payment Links / API
python scripts/simulate_failure.py --base-url https://<sub>.ngrok-free.dev/webhook
```

### D. Razorpay Dashboard

Dashboard → Payments → create a test payment and let it fail — if your
webhook endpoint is registered, Razorpay fires the same event. Register the
`payment_link.paid` event too (Settings → Webhooks) so real customer payments
close the recovery loop automatically.

> The customer contact field is what reaches Telegram. Digit-only values are
> treated as a chat_id passthrough; anything else is normalised to E.164.

## 5. What you should see (~5–10 seconds)

1. **API logs** (correlated by payment_id):
   `webhook_received → failure_classified → payment_link_created → telegram_message_sent`
2. **Telegram**: a personalised DM that names the diagnosed reason
   ("your bank's server timed out…", "there wasn't enough balance…") with a
   live `rzp.io` link (expires in 24h). Repeat attempts escalate: gentle nudge
   → expiry reminder → final notice promising to stop.
3. **Dashboard**: new row appears; status flips
   `pending → in_progress`; funnel/outcome charts update.
4. Pay the link in test mode → the `payment_link.paid` webhook flips the row
   to `recovered`, records a `paid` attempt with the amount, and the
   dashboard's recovered ₹ / recovery-rate KPIs move up automatically.
   No Razorpay event needed for demos: fire it with
   `python scripts/simulate_failure.py --event payment_link.paid --payment-id <id>`.

## 6. Interpreting statuses

| Status | Meaning |
|---|---|
| `pending` | Saved, diagnosis/recovery not finished yet |
| `in_progress` | Outreach delivered; awaiting customer payment |
| `recovered` | Customer paid (set by the `payment_link.paid` webhook) |
| `unrecoverable` | Attempt cap (3) hit or permanently undeliverable |

Attempt outcomes: `delivered · clicked · paid · failed · escalated`.
Retries: transient errors retried 3× with backoff; permanent Telegram 4xx
fail fast. Outbound sends share a sliding-window rate limiter
(30/min default).

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| HTTP 401 from `/webhook` | Secret mismatch — compare `.env` vs dashboard webhook secret |
| 200 but `webhook_ignored` in logs | Payload didn't carry a usable payment entity — real Razorpay events nest under `payload.payment.entity`; make sure you're running the fixed parser |
| Row saved, `delivery_skipped_missing_contact` | The failed payment had no phone — create the payment link with a customer contact |
| `channel_send_failed ... chat not found` then delivered | Normal: Razorpay serves the contact as E.164 (`+91…`); the agent falls back to bare digits (your chat_id) automatically |
| Simulator can't connect | API not running? `uvicorn main:app --port 8000` |
| No Telegram message | Bot never met the user — send `/start`, confirm chat_id via `getUpdates`; bots can't DM raw phone numbers |
| Groq 404 model not found | Update `GROQ_MODEL` (rules fallback still works meanwhile) |
| Duplicate payment_id ignored | Idempotency — pass a fresh `--payment-id` |
| Dashboard empty | Wrong CWD — run Streamlit from the project root so `recovery_agent.db` resolves |

## 8. Tests

```bash
pytest --cov=app --cov=dashboard --cov-report=term-missing
```

160 tests, ~95% coverage. All externals (Razorpay/Groq/Telegram) mocked;
the dashboard has an AppTest render smoke test against a seeded SQLite DB.

## 9. Webhook events handled

| Event | Behaviour |
|---|---|
| `payment.failed` | Store → diagnose (Groq + rules fallback) → fresh link → Telegram DM |
| `payment_link.paid` | Flip original payment to `recovered`, record `paid` attempt with amount (idempotent on repeats) |
| anything else | Acknowledged 200, ignored |

Subscribe to both in Razorpay Dashboard → Settings → Webhooks. The link's
`notes.original_payment_id` ties the payment back to the failed payment row.
