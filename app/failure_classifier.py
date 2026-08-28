"""LLM-powered payment failure diagnosis with a rule-based safety net.

Primary path queries Groq's ``llama-3.1-8b-instant``; any failure (network,
rate limit, timeout, or an unparseable LLM answer) degrades gracefully to the
deterministic rule-based classifier so recovery is never blocked.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

VALID_REASONS: tuple[str, ...] = (
    "insufficient_funds",
    "bank_timeout",
    "invalid_vpa",
    "card_declined",
    "otp_timeout",
    "other",
)

_PROMPT_TEMPLATE = """You are a payment failure diagnosis expert for Indian payment gateways.

Payment Method: {method}
Error Code: {error_code}
Error Description: {error_description}

Classify the failure reason into ONE of these categories:
- insufficient_funds: Customer's bank balance or card limit exceeded
- bank_timeout: Bank server down, network timeout, gateway error
- invalid_vpa: Wrong UPI ID / VPA entered by customer
- card_declined: Card online transactions disabled, card expired, or fraud block
- otp_timeout: Customer didn't enter 3D Secure OTP in time
- other: Any other reason

Return ONLY the category name (lowercase, underscore-separated)."""


class FailureClassifier:
    """Diagnoses why a payment failed via LLM, falling back to rules."""

    def __init__(self, settings: Any | None = None, llm_client: Any | None = None) -> None:
        """Args:
        settings: application settings (defaults to the global singleton).
        llm_client: optional pre-built Groq async client (tests inject fakes).
        """
        self._settings = settings
        self._llm_client = llm_client

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    async def classify(self, payment_data: dict[str, Any]) -> str:
        """Classify the failure reason for a payment.

        Args:
            payment_data: dict with ``method``, ``error_code`` and
                ``error_description`` keys.

        Returns:
            One of :data:`VALID_REASONS`.
        """
        method = payment_data.get("method") or "unknown"
        error_code = payment_data.get("error_code") or ""
        error_description = payment_data.get("error_description") or ""

        reason = await self._classify_with_llm(method, error_code, error_description)
        if reason in VALID_REASONS:
            logger.info(
                "failure_classified_llm",
                extra={"reason": reason, "error_code": error_code},
            )
            return reason

        reason = self._rule_based_classify(error_code, error_description)
        logger.info(
            "failure_classified_rules_fallback",
            extra={"reason": reason, "error_code": error_code},
        )
        return reason

    # ------------------------------------------------------------------ #
    # LLM path                                                           #
    # ------------------------------------------------------------------ #
    async def _classify_with_llm(
        self, method: str, error_code: str, error_description: str
    ) -> str | None:
        """Query Groq; return ``None`` on any failure or invalid answer."""
        client = await self._ensure_llm_client()
        if client is None:
            return None
        try:
            response = await client.chat.completions.create(
                model=self._model_name(),
                messages=[
                    {
                        "role": "user",
                        "content": _PROMPT_TEMPLATE.format(
                            method=method,
                            error_code=error_code,
                            error_description=error_description,
                        ),
                    }
                ],
                temperature=0,
                # Generous budget: reasoning models (gpt-oss, qwen) spend
                # tokens thinking before emitting the single-word answer.
                max_tokens=512,
                timeout=self._timeout_seconds(),
            )
            raw_answer = self._extract_category(
                response.choices[0].message.content or ""
            )
        except Exception as exc:  # noqa: BLE001 - any SDK/network error -> fallback
            logger.warning("groq_classification_failed", extra={"error": str(exc)})
            return None

        if raw_answer not in VALID_REASONS:
            logger.warning(
                "groq_invalid_category", extra={"raw_answer": raw_answer[:80]}
            )
            return None
        return raw_answer

    @staticmethod
    def _extract_category(content: str) -> str:
        """Normalise an LLM reply into a bare category name.

        Handles reasoning models that wrap answers in ``<think>`` blocks or
        add punctuation/quotes around the category.
        """
        text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        cleaned = text.strip().strip("`*\"' ").lower()
        if cleaned in VALID_REASONS:
            return cleaned
        # Tolerant fallback: accept when exactly one valid category appears
        # anywhere in the remaining text (multiple mentions = ambiguous).
        matches = [r for r in VALID_REASONS if r in cleaned]
        if len(matches) == 1:
            return matches[0]
        return cleaned

    async def _ensure_llm_client(self) -> Any | None:
        if self._llm_client is not None:
            return self._llm_client
        api_key = getattr(self._groq_settings(), "groq_api_key", "")
        if not api_key:
            logger.info("groq_api_key_missing_using_rules")
            return None
        from groq import AsyncGroq  # imported lazily; heavy import + optional dep

        self._llm_client = AsyncGroq(api_key=api_key)
        return self._llm_client

    def _model_name(self) -> str:
        return getattr(self._groq_settings(), "groq_model", "llama-3.1-8b-instant")

    def _timeout_seconds(self) -> float:
        return float(getattr(self._groq_settings(), "groq_timeout_seconds", 8.0))

    def _groq_settings(self) -> Any:
        if self._settings is not None:
            return self._settings
        from config import get_settings

        return get_settings()

    # ------------------------------------------------------------------ #
    # Deterministic fallback                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _rule_based_classify(error_code: str, error_description: str) -> str:
        """Keyword-based classification used when the LLM is unavailable."""
        description = (error_description or "").lower()
        code = (error_code or "").lower()

        if (
            "insufficient" in description
            or "enough balance" in description
            or "insufficient_funds" in code
        ):
            return "insufficient_funds"
        if (
            "timeout" in description
            or "timed out" in description
            or "gateway" in code
        ):
            return "bank_timeout"
        if "vpa" in description or "upi" in description:
            return "invalid_vpa"
        if "otp" in description or "authentication" in code:
            return "otp_timeout"
        if "card" in description and ("declin" in description or "expired" in description):
            return "card_declined"
        if "declin" in code:
            return "card_declined"
        return "other"
