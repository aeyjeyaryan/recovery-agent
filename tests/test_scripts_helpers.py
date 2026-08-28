"""Tests for the demo scripts' pure helpers (payload builders + batch plan)."""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.batch_demo import build_batch_plan  # noqa: E402
from scripts.simulate_failure import (  # noqa: E402
    build_paid_payload,
    build_payload,
    sign,
)

SECRET = "test_secret"


class TestSign:
    def test_sign_is_hmac_sha256_hex_of_body(self):
        body = b'{"a": 1}'
        expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert sign(body, SECRET) == expected

    def test_signature_verifies_against_listener(self):
        from app.webhook_listener import verify_webhook_signature

        body = b"payload-bytes"
        assert verify_webhook_signature(body, sign(body, SECRET), SECRET)


class TestBuildPayload:
    def test_failure_payload_uses_real_nested_envelope(self):
        payload = build_payload(
            payment_id="pay_X",
            amount_paise=149900,
            name="Demo",
            contact="6789477144",
            email="d@example.com",
            method="upi",
            error_code="GATEWAY_ERROR",
            error_description="Gateway rejected",
        )
        assert payload["event"] == "payment.failed"
        entity = payload["payload"]["payment"]["entity"]
        assert entity["id"] == "pay_X"
        assert entity["amount"] == 149900
        assert entity["customer"]["contact"] == "6789477144"

    def test_paid_payload_closes_loop_via_notes(self):
        payload = build_paid_payload(
            link_id="plink_L",
            original_payment_id="pay_ORIG",
            amount_paise=149900,
        )
        assert payload["event"] == "payment_link.paid"
        link = payload["payload"]["payment_link"]["entity"]
        assert link["id"] == "plink_L"
        assert link["status"] == "paid"
        assert link["amount"] == 149900
        assert link["notes"]["original_payment_id"] == "pay_ORIG"
        # A captured sibling payment entity ships alongside, as in real events.
        captured = payload["payload"]["payment"]["entity"]
        assert captured["status"] == "captured"
        assert captured["amount"] == 149900


class TestBuildBatchPlan:
    def test_plan_size_and_unique_ids(self):
        plan = build_batch_plan(8, recover_every=3)
        assert len(plan) == 8
        assert len({p["payment_id"] for p in plan}) == 8

    def test_recover_every_n_marks_expected_entries(self):
        plan = build_batch_plan(7, recover_every=3)
        flags = [p["will_recover"] for p in plan]
        assert flags == [False, False, True, False, False, True, False]

    def test_recover_every_zero_disables_recoveries(self):
        plan = build_batch_plan(5, recover_every=0)
        assert not any(p["will_recover"] for p in plan)

    def test_variety_across_scenarios(self):
        plan = build_batch_plan(8, recover_every=3)
        methods = {p["method"] for p in plan}
        codes = {p["code"] for p in plan}
        assert len(methods) > 1
        assert len(codes) > 1
        assert all(isinstance(p["amount_paise"], int) and p["amount_paise"] > 0
                   for p in plan)
