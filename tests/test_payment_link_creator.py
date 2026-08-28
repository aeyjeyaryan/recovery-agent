"""PaymentLinkCreator tests: payload shape, retries, and error handling."""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.models import FailedPayment
from app.payment_link_creator import PaymentLinkCreator, PaymentLinkError
from config import Settings


# --------------------------------------------------------------------- #
# Fakes                                                                 #
# --------------------------------------------------------------------- #
class FakePaymentLinkAPI:
    """Stands in for ``razorpay_client.payment_link``."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeRazorpayClient:
    def __init__(self, responses: list[Any]) -> None:
        self.payment_link = FakePaymentLinkAPI(responses)


@pytest.fixture()
def failed_payment() -> FailedPayment:
    return FailedPayment(
        payment_id="pay_DESdhfg54",
        amount=129900,
        currency="INR",
        customer_name="Rahul Sharma",
        customer_contact="+919876543210",
        customer_email="rahul@example.com",
        payment_method="upi",
        failure_reason="insufficient_funds",
    )


def fast_settings(**overrides: Any) -> Settings:
    # Zero base delay => retry backoff doesn't slow the test suite.
    defaults = {"retry_base_delay_seconds": 0.0}
    defaults.update(overrides)
    return Settings(**defaults)


SUCCESS_RESPONSE = {
    "id": "plink_test_123",
    "short_url": "https://rzp.io/i/plink_test_123",
}


# --------------------------------------------------------------------- #
# Happy path                                                            #
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_create_payment_link_returns_short_url(failed_payment):
    client = FakeRazorpayClient([dict(SUCCESS_RESPONSE)])
    creator = PaymentLinkCreator(settings=fast_settings(), client=client)

    url = await creator.create_payment_link(failed_payment)

    assert url == "https://rzp.io/i/plink_test_123"
    assert creator.last_link_id == "plink_test_123"
    assert len(client.payment_link.calls) == 1


@pytest.mark.asyncio
async def test_payload_contains_required_fields(failed_payment):
    client = FakeRazorpayClient([dict(SUCCESS_RESPONSE)])
    creator = PaymentLinkCreator(settings=fast_settings(), client=client)
    await creator.create_payment_link(failed_payment)

    payload = client.payment_link.calls[0]
    assert payload["amount"] == 129900
    assert payload["currency"] == "INR"
    assert payload["customer"]["name"] == "Rahul Sharma"
    assert payload["customer"]["contact"] == "+919876543210"
    assert payload["notify"] == {"sms": True, "email": True}
    assert payload["reminder_enable"] is True
    # Regression guard: Razorpay rejects this field ("extra fields sent").
    assert "reminder_by" not in payload
    notes = payload["notes"]
    assert notes["recovery_campaign"] == "true"
    assert notes["original_payment_id"] == "pay_DESdhfg54"
    assert notes["failure_reason"] == "insufficient_funds"


@pytest.mark.asyncio
async def test_link_expires_within_configured_hours(failed_payment):
    client = FakeRazorpayClient([dict(SUCCESS_RESPONSE)])
    creator = PaymentLinkCreator(
        settings=fast_settings(payment_link_expiry_hours=2), client=client
    )
    before = time.time()
    await creator.create_payment_link(failed_payment)
    after = time.time()

    expire_by = client.payment_link.calls[0]["expire_by"]
    assert before + 2 * 3600 - 5 <= expire_by <= after + 2 * 3600 + 5


# --------------------------------------------------------------------- #
# Retry behaviour                                                       #
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_transient_failures_are_retried_then_succeed(failed_payment):
    responses = [
        RuntimeError("connection reset"),
        RuntimeError("502 bad gateway"),
        dict(SUCCESS_RESPONSE),
    ]
    client = FakeRazorpayClient(responses)
    creator = PaymentLinkCreator(settings=fast_settings(), client=client)

    url = await creator.create_payment_link(failed_payment)

    assert url == SUCCESS_RESPONSE["short_url"]
    assert len(client.payment_link.calls) == 3


@pytest.mark.asyncio
async def test_permanent_failure_raises_payment_link_error(failed_payment):
    responses = [RuntimeError("down")] * 3
    client = FakeRazorpayClient(responses)
    creator = PaymentLinkCreator(
        settings=fast_settings(max_retries=3), client=client
    )

    with pytest.raises(PaymentLinkError):
        await creator.create_payment_link(failed_payment)

    assert len(client.payment_link.calls) == 3
