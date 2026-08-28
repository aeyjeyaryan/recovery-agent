#!/usr/bin/env python3
"""Fire a batch of synthetic failed payments (and optional recoveries).

Built for demos: one command populates the dashboard with aggregate metrics —
N failures with varied reasons, amounts, methods and customers, plus matching
``payment_link.paid`` events so some of them close out as recovered ₹.

Usage (from the project root, with the API running):

    source .venv/bin/activate
    python scripts/batch_demo.py                                  # 8 failures
    python scripts/batch_demo.py --count 12 --recover-every 2     # 50% recovered
    python scripts/batch_demo.py --dry-run                        # plan only

Every payload is signed exactly like Razorpay does
(``RAZORPAY_WEBHOOK_SECRET``; override with ``--secret``).
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.simulate_failure import build_paid_payload, build_payload, sign  # noqa: E402

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_CONTACT = "6789477144"  # Aryan's Telegram chat_id (live deliveries)

#: Rotated across the batch to show per-reason interventions + variety.
SCENARIOS = [
    ("INSUFFICIENT_FUNDS", "Customer doesn't have enough balance", "upi", [499, 1499]),
    ("CARD_DECLINED", "Payment declined by issuing bank", "card", [999, 2999]),
    ("GATEWAY_ERROR", "Gateway rejected the transaction", "netbanking", [1999]),
    ("AUTHENTICATION_REQUIRED", "3D-Secure authentication failed", "card", [749, 4999]),
]
CUSTOMER_NAMES = [
    "Asha Verma", "Rohan Mehta", "Priya Nair", "Kabir Singh",
    "Meera Iyer", "Dev Patel", "Sara Khan", "Arjun Rao",
    "Nisha Gupta", "Vikram Bose",
]


def build_batch_plan(count: int, *, recover_every: int) -> list[dict]:
    """Deterministic demo plan: one entry per failure to fire."""
    plan: list[dict] = []
    for i in range(count):
        code, desc, method, amounts = SCENARIOS[i % len(SCENARIOS)]
        payment_id = f"pay_SIM_{secrets.token_hex(4)}"
        amount_rupees = amounts[(i // len(SCENARIOS)) % len(amounts)]
        # Every Nth payment will later "pay" its recovery link.
        will_recover = bool(recover_every) and (i % recover_every == recover_every - 1)
        plan.append({
            "payment_id": payment_id,
            "link_id": f"plink_SIM_{secrets.token_hex(4)}" if will_recover else None,
            "amount_paise": amount_rupees * 100,
            "name": CUSTOMER_NAMES[i % len(CUSTOMER_NAMES)],
            "code": code,
            "description": desc,
            "method": method,
            "will_recover": will_recover,
        })
    return plan


def post(client: httpx.Client, base_url: str, payload: dict, secret: str) -> int:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response = client.post(
        f"{base_url.rstrip('/')}/webhook",
        content=body,
        headers={"Content-Type": "application/json",
                 "X-Razorpay-Signature": sign(body, secret)},
        timeout=15.0,
    )
    return response.status_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Populate the dashboard with a batch of failures + recoveries."
    )
    parser.add_argument("--count", type=int, default=8,
                        help="How many failures to fire (default: %(default)s)")
    parser.add_argument("--recover-every", type=int, default=3, metavar="N",
                        help="Close the loop on every Nth failure "
                             "(0 disables; default: %(default)s)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="API base URL (default: %(default)s)")
    parser.add_argument("--contact", default=DEFAULT_CONTACT,
                        help="Phone/chat_id used for every customer")
    parser.add_argument("--secret", default=None,
                        help="Webhook secret override (else read from .env)")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Seconds between webhooks (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan without sending anything.")
    args = parser.parse_args()

    secret = args.secret
    if not secret:
        from config import get_settings

        secret = get_settings().razorpay_webhook_secret
    if not secret and not args.dry_run:
        parser.error("No webhook secret found (set RAZORPAY_WEBHOOK_SECRET "
                     "in .env or pass --secret).")

    plan = build_batch_plan(args.count, recover_every=args.recover_every)
    recoveries = [p for p in plan if p["will_recover"]]
    total_inr = sum(p["amount_paise"] for p in plan) / 100
    recovered_inr = sum(p["amount_paise"] for p in recoveries) / 100

    print(f"Plan: {len(plan)} failures ({total_inr:,.0f} INR), "
          f"{len(recoveries)} recovering ({recovered_inr:,.0f} INR)\n")

    if args.dry_run:
        for p in plan:
            flag = " -> WILL RECOVER" if p["will_recover"] else ""
            print(f"  {p['payment_id']}  \u20b9{p['amount_paise'] / 100:>6,.0f}  "
                  f"{p['method']:<10} {p['code']}{flag}")
        return 0

    effective_secret = secret or "unused"
    base_url = args.base_url.rstrip("/")
    ok = fail = paid_ok = paid_fail = 0

    with httpx.Client() as client:
        for i, item in enumerate(plan):
            failure = build_payload(
                payment_id=item["payment_id"],
                amount_paise=item["amount_paise"],
                name=item["name"],
                contact=args.contact,
                email=f"{item['name'].split()[0].lower()}@example.com",
                method=item["method"],
                error_code=item["code"],
                error_description=item["description"],
            )
            status = post(client, base_url, failure, effective_secret)
            if status == 200:
                ok += 1
            else:
                fail += 1
            print(f"[{i + 1}/{len(plan)}] failure {item['payment_id']} "
                  f"\u2192 HTTP {status}")

            if item["will_recover"]:
                time.sleep(args.delay)
                paid = build_paid_payload(
                    link_id=item["link_id"],
                    original_payment_id=item["payment_id"],
                    amount_paise=item["amount_paise"],
                )
                status = post(client, base_url, paid, effective_secret)
                if status == 200:
                    paid_ok += 1
                else:
                    paid_fail += 1
                print(f"          recovery {item['link_id']} \u2192 HTTP {status}")

            time.sleep(args.delay)

    print(f"\nDone. failures accepted={ok} rejected={fail}; "
          f"paid events accepted={paid_ok} rejected={paid_fail}")
    print("Open the dashboard to see the aggregate view:")
    print("  streamlit run dashboard/app.py")
    return 0 if (fail == 0 and paid_fail == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
