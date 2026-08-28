"""Telegram Bot API client for recovery messages.

Sends plain-text recovery messages via the official Bot API
(``POST /bot<token>/sendMessage``) over async HTTP — no heavy SDK needed.

Note on addressing: Razorpay webhooks carry a *phone number*, while Telegram
addresses users by numeric ``chat_id``.  Until a phone→chat_id mapping exists
(e.g. via a deep-link onboarding flow), ``customer_contact`` is passed through
as ``chat_id`` so the pipeline works unchanged once that mapping lands.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.channel_clients import RateLimiter
from app.retry import RetriesExhaustedError, with_retries

logger = logging.getLogger(__name__)

#: Network/HTTP-level failures worth retrying (429/5xx via raise_for_status,
#: connection and timeout errors).  Permanent 4xx errors raise TelegramError
#: instead, which is deliberately NOT in this tuple => fail fast, no retry.
RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPError,)


class TelegramError(RuntimeError):
    """Raised when a Telegram message cannot be delivered."""


class TelegramClient:
    """Sends recovery messages via the Telegram Bot API."""

    def __init__(
        self,
        settings: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        """Args:
        settings: application settings (defaults to global singleton).
        http_client: injectable ``httpx.AsyncClient`` (tests mock this).
        rate_limiter: shared outbound rate limiter.
        """
        self._settings = settings
        self._http = http_client
        self._owns_http = http_client is None
        self._rate_limiter = rate_limiter or RateLimiter(
            max_events=self._value("outbound_rate_limit_per_minute", 30),
            window_seconds=60.0,
        )

    async def send_recovery_message(self, customer_contact: str, message: str) -> dict[str, Any]:
        """Send a Telegram message; returns ``{"message_id", "status"}``.

        Args:
            customer_contact: recipient chat ID (see module docstring).
            message: full message text.

        Raises:
            TelegramError: if delivery fails after all retries.
        """
        await self._rate_limiter.acquire()
        try:
            raw = await with_retries(
                lambda: self._send(customer_contact, message),
                name="telegram_send",
                max_retries=self._value("max_retries", 3),
                base_delay_seconds=self._value("retry_base_delay_seconds", 0.5),
                retry_on=RETRYABLE_ERRORS,
            )
        except RetriesExhaustedError as exc:
            raise TelegramError(str(exc)) from exc.last_error
        result = raw.get("result", {})
        return {"message_id": result.get("message_id"), "status": "sent"}

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #
    async def _send(self, chat_id: str, text: str) -> dict[str, Any]:
        url = (
            f"{self._value('telegram_api_base_url', 'https://api.telegram.org')}"
            f"/bot{self._value('telegram_bot_token', '')}/sendMessage"
        )
        response = await self._get_http().post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=self._timeout(),
        )
        if response.status_code in (429, 500, 502, 503, 504):
            # Transient: raise to trigger retry/backoff.
            response.raise_for_status()
        if response.status_code >= 400:
            logger.error(
                "telegram_permanent_error",
                extra={"status_code": response.status_code, "body": response.text[:300]},
            )
            raise TelegramError(f"Telegram API error {response.status_code}")

        data: dict[str, Any] = response.json()
        if not data.get("ok"):
            description = data.get("description", "unknown error")
            logger.error(
                "telegram_api_rejected",
                extra={"description": str(description)[:200]},
            )
            raise TelegramError(f"Telegram API rejected message: {description}")

        logger.info(
            "telegram_message_sent",
            extra={"message_id": data.get("result", {}).get("message_id")},
        )
        return data

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        """Close the owned HTTP client (no-op for injected clients)."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    def _timeout(self) -> float:
        return float(self._value("http_timeout_seconds", 15.0))

    def _value(self, key: str, default: Any) -> Any:
        if self._settings is not None:
            return getattr(self._settings, key, default)
        from config import get_settings

        return getattr(get_settings(), key, default)
