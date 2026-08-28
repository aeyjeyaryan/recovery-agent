#!/usr/bin/env python3
"""Trigger synthetic Razorpay webhooks against the recovery agent.

Signs payloads exactly like Razorpay does (HMAC-SHA256 over the raw body,
hex-encoded in ``X-Razorpay-Signature``) so they pass signature verification.

Two event modes:

- ``payment.failed``     → flows through diagnosis -> payment link -> Telegram.
- ``payment_link.paid``  → closes the loop: flips a stored payment to
  ``recovered`` and records the money on the dashboard.

Usage (from the project root, with the API running):

    source .venv/bin/activate
    python scripts/simulate_failure.py                        # random payment
    python scripts/simulate_failure.py --amount 2499 --name "Demo Customer"
    python scripts/simulate_failure.py --base-url https://<your-ngrok>.ngrok-free.dev
    python scripts/simulate_failure.py --event payment_link.paid \
        --payment-id pay_SIM_ab12cd34 --link-id plink_TTJR6ip0N6JlQV

The webhook secret is read from ``.env`` / environment
(``RAZORPAY_WEBHOOK_SECRET``); override ad-hoc with ``--secret``.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import sys
from pathlib import Path

import httpx

# Allow running from anywhere: make project root importable for config.py.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_CONTACT = "6789477144"  # Aryan's Telegram chat_id (live deliveries)


def build_payload(
    *,
    payment_id: str,
    amount_paise: int,
    name: str,
    contact: str,
    email: str,
    method: str,
    error_code: str,
    error_description: str,
) -> dict:
    """A minimal-but-realistic Razorpay ``payment.failed`` webhook body.

    Mirrors Razorpay's real envelope: entities live under
    ``payload.<type>.entity``.
    """
    return {
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount_paise,
                    "currency": "INR",
                    "status": "failed",
                    "method": method,
                    "error_code": error_code,
                    "error_description": error_description,
                    "customer": {
                        "name": name,
                        "contact": contact,
                        "email": email,
                    },
                }
            }
        },
    }


def build_paid_payload(
    *,
    link_id: str,
    original_payment_id: str,
    amount_paise: int,
) -> dict:
    """A realistic Razorpay ``payment_link.paid`` webhook body.

    Mirrors the real envelope: the paid link nests under
    ``payload.payment_link.entity`` (with its ``notes`` carrying our
    ``original_payment_id``), plus a sibling captured ``payment`` entity.
    """
    return {
        "event": "payment_link.paid",
        "contains": ["payment_link", "payment"],
        "payload": {
            "payment_link": {
                "entity": {
                    "id": link_id,
                    "status": "paid",
                    "amount": amount_paise,
                    "currency": "INR",
                    "short_url": f"https://rzp.io/i/{link_id[-8:]}",
                    "notes": {
                        "recovery_campaign": "true",
                        "original_payment_id": original_payment_id,
                    },
                }
            },
            "payment": {
                "entity": {
                    "id": f"pay_{secrets.token_hex(6)}",
                    "status": "captured",
                    "amount": amount_paise,
                    "currency": "INR",
                    "method": "upi",
                }
            },
        },
    }


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fire signed synthetic Razorpay webhooks "
                    "(payment.failed or payment_link.paid)."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="API base URL (default: %(default)s)")
    parser.add_argument("--event", default="payment.failed",
                        choices=["payment.failed", "payment_link.paid"],
                        help="Which webhook to fire (default: %(default)s)")
    parser.add_argument("--payment-id", default=None,
                        help="Unique payment id (default: pay_SIM_<random>). "
                             "For payment_link.paid: the ORIGINAL failed "
                             "payment id from notes.")
    parser.add_argument("--link-id", default=None,
                        help="Recovery payment link id for payment_link.paid "
                             "(default: plink_SIM_<random>)")
    parser.add_argument("--amount", type=float, default=1499.0,
                        help="Amount in RUPEES (default: %(default)s)")
    parser.add_argument("--name", default="Simulated Customer")
    parser.add_argument("--contact", default=DEFAULT_CONTACT,
                        help="Phone/E.164 or Telegram chat_id "
                             "(default: Aryan's chat id)")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--method", default="upi",
                        choices=["upi", "card", "netbanking", "wallet"])
    parser.add_argument("--reason-code", default="GATEWAY_ERROR",
                        help="Razorpay error_code, e.g. INSUFFICIENT_FUNDS, "
                             "CARD_DECLINED, AUTHENTICATION_REQUIRED "
                             "(default: %(default)s)")
    parser.add_argument("--reason-description", default=None)
    parser.add_argument("--secret", default=None,
                        help="Webhook secret override (else read from .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print payload + signature without sending.")
    args = parser.parse_args()

    secret = args.secret
    if not secret:
        from config import get_settings

        secret = get_settings().razorpay_webhook_secret
    if not secret and not args.dry_run:
        parser.error("No webhook secret found (set RAZORPAY_WEBHOOK_SECRET "
                     "in .env or pass --secret).")

    payment_id = args.payment_id or f"pay_SIM_{secrets.token_hex(4)}"

    if args.event == "payment_link.paid":
        link_id = args.link_id or f"plink_SIM_{secrets.token_hex(4)}"
        amount_paise = int(round(args.amount * 100))
        payload = build_paid_payload(
            link_id=link_id,
            original_payment_id=payment_id,
            amount_paise=amount_paise,
        )
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = sign(body, secret or "unused-for-dry-run")

        print("event        : payment_link.paid")
        print(f"original     : {payment_id}")
        print(f"link_id      : {link_id}")
        print(f"target       : {args.base_url.rstrip('/')}/webhook")
        print(f"amount       : {args.amount:.2f} INR ({amount_paise} paise)")

        if args.dry_run:
            print("\n[dry-run] payload:")
            print(json.dumps(payload, indent=2))
            print(f"\n[dry-run] X-Razorpay-Signature: {signature}")
            return 0

        return post_webhook(args.base_url, body, signature, expect="paid")

    error_descriptions = {
        "INSUFFICIENT_FUNDS": "Customer doesn't have enough balance",
        "CARD_DECLINED": "Payment declined by issuing bank",
        "AUTHENTICATION_REQUIRED": "3D-Secure authentication failed",
        "GATEWAY_ERROR": "Gateway rejected the transaction",
    }
    description = args.reason_description or error_descriptions.get(
        args.reason_code, "Transaction failed"
    )

    payload = build_payload(
        payment_id=payment_id,
        amount_paise=int(round(args.amount * 100)),
        name=args.name,
        contact=args.contact,
        email=args.email,
        method=args.method,
        error_code=args.reason_code,
        error_description=description,
    )
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = sign(body, secret or "unused-for-dry-run")

    print(f"payment_id   : {payment_id}")
    print(f"target       : {args.base_url.rstrip('/')}/webhook")
    print(f"amount       : {args.amount:.2f} INR ({payload['payload']['payment']['entity']['amount']} paise)")
    print(f"error_code   : {args.reason_code}")
    print(f"contact      : {args.contact}")

    if args.dry_run:
        print("\n[dry-run] payload:")
        print(json.dumps(payload, indent=2))
        print(f"\n[dry-run] X-Razorpay-Signature: {signature}")
        return 0

    return post_webhook(args.base_url, body, signature, expect="failed")


def post_webhook(base_url: str, body: bytes, signature: str, *, expect: str) -> int:
    """POST a signed webhook body and print human-friendly next steps."""
    url = f"{base_url.rstrip('/')}/webhook"
    try:
        response = httpx.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        print(f"\nFAILED to reach {url}: {exc}", file=sys.stderr)
        print("Is the API running?  ->  uvicorn main:app --port 8000",
              file=sys.stderr)
        return 1

    print(f"\nHTTP {response.status_code} from {url}")
    if response.status_code == 200 and expect == "paid":
        print("Accepted. The payment should now be marked recovered -")
        print("watch the server logs for `payment_recovered` or refresh the")
        print("dashboard to see recovered ₹ / recovery rate move up.")
    elif response.status_code == 200:
        print("Accepted. Watch the server logs / dashboard for the pipeline:")
        print("  classify -> create link -> telegram_message_sent -> audit row")
        print(f"Dashboard: streamlit run dashboard/app.py  ")
        print("Close the loop later:")
        print(f"  python scripts/simulate_failure.py --event payment_link.paid "
              f"--payment-id <this payment id>")
    elif response.status_code == 401:
        print("Signature rejected - check RAZORPAY_WEBHOOK_SECRET matches "
              "the server's .env.", file=sys.stderr)
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
