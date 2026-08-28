# Razorpay AI Revenue Recovery Agent

An autonomous agent that **recovers failed Razorpay payments**: it listens to
`payment.failed` webhooks, diagnoses *why* the payment failed with an LLM,
creates a fresh payment link, reaches the customer on Telegram,
and keeps a complete audit trail — with a live recovery dashboard.

```
payment.failed ──▶ verify ──▶ diagnose (Groq LLM + rules fallback)
                       │                 │
                  idempotent        fresh Razorpay link
                       │                 │
                       ▼                 ▼
              SQLite audit trail ◀── Telegram Bot API outreach
                       │
              Streamlit dashboard
                       │
              sendreminder.py (re-attempt scheduler)
                       │
              re-dispatch due failures ──▶ orchestrator ──▶ Telegram
```

## Features

- **Secure webhook ingestion** — HMAC-SHA256 signature verification on raw bytes, idempotent processing, fast 2xx acks (heavy work runs in background tasks)
- **LLM failure diagnosis** — Groq `llama-3.1-8b-instant` (configurable via `GROQ_MODEL`) classifies failures into 6 reasons; automatic rule-based fallback if the API is down
- **Fresh payment links** — Razorpay Payment Links API with 24h expiry, retry/backoff, and campaign notes for traceability
- **Telegram outreach** — Telegram Bot API (`sendMessage`) with shared sliding-window rate limiter + per-payment attempt cap (anti-spam); retry/backoff on 429 & 5xx, fail-fast on permanent errors
- **Per-reason interventions** — tailored remedy copy for each failure type (insufficient funds, bank timeout, card declined, etc.)
- **Escalation ladder** — 3-stage tone progression: gentle nudge → expiry urgency → final notice with promise to stop
- **Recovered-money loop** — `payment_link.paid` webhooks close the recovery loop, flip status to `recovered`, record recovered amount; idempotent on duplicates
- **Re-attempt scheduler** — periodic background dispatcher (`sendreminder.py`) that re-triggers recovery for stuck payments after per-reason cooldown windows
- **Full audit trail** — every attempt persisted (`channel`, `message`, `outcome`, `link_id`) plus structured JSON logs with `correlation_id = payment_id`
- **Recovery dashboard** — Streamlit metrics: recovery rate, channel breakdown, top failure reasons, recent attempts
- **Batch demo** — `batch_demo.py` fires N varied failures + optional recoveries in one command for aggregate dashboard demos
- **Tested** — 195 tests (~93% coverage) with all external APIs mocked (`pytest --cov`)

## Project Structure

```
razorpay-recovery-agent/
├── README.md · requirements.txt · .env.example · .gitignore · config.py · main.py
├── app/
│   ├── webhook_listener.py       # POST /webhook: signature check, idempotency, paid-loop
│   ├── failure_classifier.py     # Groq LLM + rule-based fallback
│   ├── recovery_orchestrator.py  # channel selection, reason-aware msg, escalation, delivery
│   ├── payment_link_creator.py   # Razorpay links w/ retries
│   ├── sendreminder.py           # Re-attempt scheduler with per-reason cooldowns
│   ├── channel_clients/          # telegram_client (+ shared RateLimiter)
│   ├── audit_logger.py           # RecoveryAttempt persistence
│   ├── database.py · models.py   # SQLAlchemy layer
│   ├── retry.py                  # shared async exponential backoff
│   └── logging_config.py         # JSON logs + correlation IDs
├── tests/                        # 195 tests (externals mocked)
├── dashboard/app.py · utils.py   # Streamlit ops UI (KPIs, funnel, drill-downs)
├── scripts/simulate_failure.py   # Fire signed synthetic webhooks (failure or paid)
├── scripts/batch_demo.py         # Fire N varied failures + optional recoveries
└── docs/                         # architecture · api_reference · deployment_guide · demo_guide
```

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in your keys

uvicorn main:app --reload --port 8000
curl localhost:8000/health
```

Expose it to Razorpay with ngrok, subscribe to `payment.failed` **and**
`payment_link.paid`, set `RAZORPAY_WEBHOOK_SECRET` in both places — full
details in [`docs/deployment_guide.md`](docs/deployment_guide.md).

Dashboard:

```bash
streamlit run dashboard/app.py
```

Trigger a failure on demand (and close the loop as if the customer paid):

```bash
python scripts/simulate_failure.py --amount 2499 --reason-code INSUFFICIENT_FUNDS
python scripts/batch_demo.py --count 12 --recover-every 2   # aggregate demo
```

Run the re-attempt scheduler (sends reminders for stuck payments):

```bash
python -m app.sendreminder                     # one-shot scan
python -m app.sendreminder --dry-run           # show eligible payments
python -m app.sendreminder --poll --interval 300  # continuous loop (5 min)
```

Full walkthrough — running the stack, three ways to fire a failure, what to
expect, troubleshooting — in [`docs/demo_guide.md`](docs/demo_guide.md).

## How Recovery Works

| Step | Module | Detail |
|---|---|---|
| 1. Ingest | `app/webhook_listener.py` | Raw-body HMAC-SHA256 check → insert `FailedPayment(pending)` → ack in <2s |
| 2. Diagnose | `app/failure_classifier.py` | LLM → one of `insufficient_funds`, `bank_timeout`, `invalid_vpa`, `card_declined`, `otp_timeout`, `other`; falls back to keyword rules |
| 3. Link | `app/payment_link_creator.py` | Fresh Razorpay link, expires in 24h, notes carry original payment id + reason |
| 4. Select channel | `app/recovery_orchestrator.py` | Telegram-only today; `select_channel()` is the seam for future multi-channel routing |
| 5. Deliver | `app/channel_clients/*` | Message personalised by diagnosis (`REASON_INTERVENTIONS`) and escalation stage (`ESCALATION_TONES`: nudge → expiry reminder → final notice) via Telegram Bot API; fallback-chain structure ready for more channels |
| 6. Audit | `app/audit_logger.py` | Row in `recovery_attempts` + JSON log event per attempt |
| 7. Close loop | `app/webhook_listener.py` | `payment_link.paid` webhook flips status → `recovered`, records a `paid` attempt with `recovery_amount` (idempotent); dashboard KPIs update automatically |
| 8. Re-try | `app/sendreminder.py` | Background scheduler scans for `pending`/`in_progress` payments past their per-reason cooldown, re-triggers orchestrator up to the attempt cap |

## Per-Reason Cooldowns

The re-attempt scheduler uses configurable cooldown windows to avoid spamming
customers whose banks may still be down:

| Failure Reason | Cooldown | Rationale |
|---|---|---|
| `insufficient_funds` | 2 hours | Customer may need time to top up |
| `bank_timeout` | 30 minutes | Transient; retry sooner |
| `invalid_vpa` | 1 hour | Customer may correct the VPA |
| `card_declined` | 2 hours | Customer may switch payment method |
| `otp_timeout` | 30 minutes | Just timed out; quick retry is fine |
| `other` | 1 hour | Conservative default |

## Models & LLM

- **Primary LLM**: Groq `llama-3.1-8b-instant` (configurable via `GROQ_MODEL` env var)
- **Fallback**: Rule-based keyword classifier covering all 6 failure categories
- **Prompt**: Structured classification prompt with payment method, error code, and error description → returns one of 6 categories
- **Hardening**: `max_tokens=512` for reasoning models, `<think>` block stripping, tolerant category extraction

## Testing & Coverage

```bash
pytest --cov=app --cov=dashboard --cov-report=term-missing
```

**195 tests, ~93% coverage.** Covers:

- Valid/invalid/missing signatures, idempotency, malformed payloads
- End-to-end webhook→diagnosis→recovery→audit flow
- LLM success/failure/garbage fallbacks, all rule branches
- Retry exhaustion, channel selection matrix, fallback delivery
- Attempt caps, rate limiter, JSON logging
- Per-reason interventions, escalation ladder
- `payment_link.paid` loop-closing
- Re-attempt scheduler: cooldown windows, eligibility, dispatch, CLI dry-run
- Demo scripts' payload builders

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — system diagram, data flow, design decisions
- [`docs/api_reference.md`](docs/api_reference.md) — endpoints, schemas, error taxonomy
- [`docs/demo_guide.md`](docs/demo_guide.md) — runbook: start everything, simulate failures, read the dashboard

## Security Notes

- All credentials come from environment variables — never committed.
- Webhook signatures verified over the exact raw request bytes before parsing.
- Customer PII is persisted only in the audit tables, never logged; messages
  contain only what's needed for recovery.
- Outbound messaging is rate-limited and capped per payment to prevent spam;
  escalation copy stays compliant (final notice promises to stop).


