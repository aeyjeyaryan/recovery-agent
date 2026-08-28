"""FailureClassifier tests: LLM classification, validation, and rule fallback."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.failure_classifier import VALID_REASONS, FailureClassifier
from config import Settings


# --------------------------------------------------------------------- #
# Fakes                                                                 #
# --------------------------------------------------------------------- #
class FakeCompletions:
    def __init__(self, answer: str | None = None, error: Exception | None = None) -> None:
        self._answer = answer
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        message = SimpleNamespace(content=self._answer)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice])


class FakeGroqClient:
    def __init__(self, answer: str | None = None, error: Exception | None = None) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(answer, error))


def payment_data(**overrides: Any) -> dict[str, Any]:
    data = {
        "method": "upi",
        "error_code": "BAD_REQUEST",
        "error_description": "Insufficient funds in the account",
    }
    data.update(overrides)
    return data


@pytest.fixture()
def settings() -> Settings:
    return Settings(groq_api_key="gsk_test", groq_model="llama-3.1-8b-instant")


# --------------------------------------------------------------------- #
# LLM path                                                              #
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_llm_classification_success(settings):
    client = FakeGroqClient(answer="card_declined")
    classifier = FailureClassifier(settings=settings, llm_client=client)
    assert await classifier.classify(payment_data(method="card")) == "card_declined"


@pytest.mark.asyncio
async def test_llm_prompt_contains_payment_details(settings):
    client = FakeGroqClient(answer="other")
    classifier = FailureClassifier(settings=settings, llm_client=client)
    await classifier.classify(
        payment_data(method="upi", error_code="GATEWAY_ERROR", error_description="Bank timeout")
    )
    prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "upi" in prompt
    assert "GATEWAY_ERROR" in prompt
    assert "Bank timeout" in prompt


@pytest.mark.asyncio
async def test_llm_uses_configured_model_and_zero_temperature(settings):
    client = FakeGroqClient(answer="otp_timeout")
    classifier = FailureClassifier(settings=settings, llm_client=client)
    await classifier.classify(payment_data())
    call = client.chat.completions.calls[0]
    assert call["model"] == "llama-3.1-8b-instant"
    assert call["temperature"] == 0


@pytest.mark.asyncio
async def test_groq_network_error_falls_back_to_rules(settings):
    client = FakeGroqClient(error=ConnectionError("boom"))
    classifier = FailureClassifier(settings=settings, llm_client=client)
    reason = await classifier.classify(payment_data())
    assert reason == "insufficient_funds"


@pytest.mark.asyncio
async def test_llm_invalid_answer_falls_back_to_rules(settings):
    client = FakeGroqClient(answer="MAYBE_BANK_ISSUE")
    classifier = FailureClassifier(settings=settings, llm_client=client)
    reason = await classifier.classify(
        payment_data(error_description="Payment timed out at bank")
    )
    assert reason == "bank_timeout"


@pytest.mark.asyncio
async def test_missing_api_key_skips_llm(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")
    classifier = FailureClassifier(settings=Settings(groq_api_key=""))
    reason = await classifier.classify(payment_data())
    assert reason == "insufficient_funds"


def test_all_valid_reasons_are_known():
    assert set(VALID_REASONS) == {
        "insufficient_funds",
        "bank_timeout",
        "invalid_vpa",
        "card_declined",
        "otp_timeout",
        "other",
    }


# --------------------------------------------------------------------- #
# Rule-based fallback (deterministic)                                   #
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("error_code", "error_description", "expected"),
    [
        ("BAD_REQUEST", "Insufficient funds in the account", "insufficient_funds"),
        ("INSUFFICIENT_FUNDS", "Customer doesn't have enough balance", "insufficient_funds"),
        ("", "Customer does not have enough balance to pay", "insufficient_funds"),
        ("", "payment timed out while contacting bank", "bank_timeout"),
        ("GATEWAY_ERROR", "something else happened", "bank_timeout"),
        ("gateway_error", "", "bank_timeout"),
        ("BAD_REQUEST", "Invalid VPA entered by customer", "invalid_vpa"),
        ("BAD_REQUEST", "UPI handle does not exist", "invalid_vpa"),
        ("BAD_REQUEST", "Customer did not enter OTP in time", "otp_timeout"),
        ("AUTHENTICATION_REQUIRED", "3D-Secure authentication failed", "otp_timeout"),
        ("CARD_DECLINED", "Card declined by issuer", "card_declined"),
        ("CARD_DECLINED", "", "card_declined"),
        ("", "Card expired last month", "card_declined"),
        ("WEIRD_ERROR", "Something totally novel happened", "other"),
        ("", "", "other"),
    ],
)
def test_rule_based_classify(error_code, error_description, expected):
    assert FailureClassifier._rule_based_classify(error_code, error_description) == expected


@pytest.mark.asyncio
async def test_rules_used_directly_when_no_settings_provided():
    """No injected client + default settings without a key => pure rules."""
    classifier = FailureClassifier()
    reason = await classifier.classify(
        payment_data(error_code="", error_description="Invalid VPA entered")
    )
    assert reason == "invalid_vpa"
