"""Fresh Razorpay payment link generation for failed payments.

The official ``razorpay`` SDK is synchronous, so the blocking call is wrapped
in ``asyncio.to_thread`` to keep the event loop responsive, and retried with
exponential backoff for transient API failures.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.retry import RetriesExhaustedError, with_retries

logger = logging.getLogger(__name__)


class PaymentLinkError(RuntimeError):
    """Raised when a payment link cannot be created after all retries."""


class PaymentLinkCreator:
    """Creates recovery payment links through the Razorpay Payment Links API."""

    def __init__(self, settings: Any | None = None, client: Any | None = None) -> None:
        """Args:
        settings: application settings (defaults to global singleton).
        client: pre-initialised Razorpay SDK client (tests inject mocks).
        """
        self._settings = settings
        self._client = client
        #: Razorpay link ID of the most recently created link (audit trail).
        self.last_link_id: str | None = None

    async def create_payment_link(self, failed_payment: Any) -> str:
        """Create a new Razorpay payment link for a failed payment.

        Returns:
            The customer-facing short URL.

        Raises:
            PaymentLinkError: if link creation fails after all retries.
        """
        payload = self._build_payload(failed_payment)
        try:
            response = await with_retries(
                lambda: asyncio.to_thread(self._create_sync, payload),
                name="razorpay_create_payment_link",
                max_retries=self._settings_value("max_retries", 3),
                base_delay_seconds=self._settings_value("retry_base_delay_seconds", 0.5),
                sleep=self._sleep,
            )
        except RetriesExhaustedError as exc:
            logger.error(
                "payment_link_creation_failed",
                extra={"payment_id": failed_payment.payment_id},
            )
            raise PaymentLinkError(str(exc)) from exc.last_error

        logger.info(
            "payment_link_created",
            extra={
                "payment_id": failed_payment.payment_id,
                "link_id": response.get("id"),
            },
        )
        self.last_link_id = response.get("id")
        return response["short_url"]

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    def _create_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Blocking SDK call (runs in a worker thread)."""
        return dict(self._get_client().payment_link.create(payload))

    def _build_payload(self, failed_payment: Any) -> dict[str, Any]:
        # Timezone-aware UTC: naive datetime.timestamp() would interpret the
        # value as LOCAL time and skew the expiry by the UTC offset.
        expiry = datetime.now(timezone.utc) + timedelta(
            hours=self._settings_value("payment_link_expiry_hours", 24)
        )
        callback_url = self._settings_value("payment_success_callback_url", "")
        return {
            "amount": failed_payment.amount,
            "currency": failed_payment.currency or "INR",
            "customer": {
                "name": failed_payment.customer_name,
                "contact": failed_payment.customer_contact,
                "email": failed_payment.customer_email,
            },
            "notify": {"sms": True, "email": True},
            # NOTE: `reminder_by` is NOT a valid Payment Links API field —
            # Razorpay rejects it with "extra fields sent". Automatic
            # reminders are enabled by reminder_enable alone.
            "reminder_enable": True,
            "callback_url": callback_url,
            "callback_method": "get",
            "expire_by": int(expiry.timestamp()),
            "notes": {
                "recovery_campaign": "true",
                "original_payment_id": failed_payment.payment_id,
                "failure_reason": failed_payment.failure_reason or "unknown",
            },
        }

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import razorpay  # imported lazily; optional dependency

        settings = self._get_settings()
        self._client = razorpay.Client(
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
        )
        return self._client

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def _settings_value(self, key: str, default: Any) -> Any:
        return getattr(self._settings, key, default) if self._settings else default

    def _get_settings(self) -> Any:
        if self._settings is not None:
            return self._settings
        from config import get_settings

        return get_settings()
