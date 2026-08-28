# API Reference

Base URL (local): `http://localhost:8000`

## POST /webhook

Receives Razorpay webhook events. Only `payment.failed` events are processed;
all other verified events are acknowledged and ignored.

### Request

Headers:

| Header | Required | Description |
|---|---|---|
| `Content-Type` | yes | `application/json` |
| `X-Razorpay-Signature` | yes | HMAC-SHA256 hex digest of the raw body using `RAZORPAY_WEBHOOK_SECRET` |

Body — relevant subset of the official payload:

```json
{
  "entity": "payment",
  "event": "payment.failed",
  "contains": ["payment"],
  "payment": {
    "entity": {
      "id": "pay_DESdhfg54",
      "amount": 129900,
      "currency": "INR",
      "status": "failed",
      "method": "upi",
      "error_code": "BAD_REQUEST",
      "error_description": "Insufficient funds in the account",
      "customer": {
        "name": "Rahul Sharma",
        "contact": "+919876543210",
        "email": "rahul@example.com"
      }
    }
  }
}
```

### Responses

| Status | Meaning |
|---|---|
| `200` | Event accepted. Duplicate/irrelevant events also return `200` (stops Razorpay retries). |
| `400` | Signature valid but body is not parseable JSON. |
| `401` | Missing or invalid `X-Razorpay-Signature`. |

Notes:
- The signature is computed over the **exact raw bytes** of the request body.
- Processing is idempotent per `payment.entity.id`.
- Heavy work (LLM diagnosis, outreach) runs asynchronously after the `200`.

## GET /health

Liveness/readiness probe.

### Response

```json
{
  "status": "healthy",
  "config_ok": true
}
```

| Field | Description |
|---|---|
| `status` | Always `"healthy"` when the process is serving. |
| `config_ok` | `true` when mandatory credentials were present at startup; `false` means running degraded (see startup logs). |

## Internal Library APIs (not HTTP)

These are Python classes consumed by the pipeline — listed for completeness.

### `FailureClassifier.classify(payment_data) -> str`
Returns one of: `insufficient_funds`, `bank_timeout`, `invalid_vpa`,
`card_declined`, `otp_timeout`, `other`.

### `PaymentLinkCreator.create_payment_link(failed_payment) -> str`
Returns the Razorpay `short_url`. Raises `PaymentLinkError` after retries.

### `TelegramClient.send_recovery_message(contact, message) -> dict`
Returns `{"message_id": <int>, "status": "sent"}`. Raises `TelegramError`.
`contact` is used as the Telegram `chat_id` (see architecture notes on
phone→chat_id mapping).

### `AuditLogger.log_attempt(...) -> RecoveryAttempt`
Persists one attempt row: `(failed_payment_id, channel, action, message,
outcome, payment_link_id, recovery_amount)`.

### Error taxonomy

| Exception | Source | Retry behaviour |
|---|---|---|
| `RetriesExhaustedError` | `app/retry.py` | n/a (raised after backoff exhausted) |
| `PaymentLinkError` | Razorpay links | 3 attempts, exponential backoff |
| `TelegramError` | Bot API | 429/5xx/network errors retried (3x backoff); other 4xx fail fast |
