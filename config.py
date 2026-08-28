"""Typed application configuration.

All settings are loaded from environment variables (and optionally a ``.env``
file in the working directory).  Every field has a safe default so that merely
importing this module never fails -- which keeps unit tests hermetic.  Call
:meth:`Settings.ensure_startup_ready` during application startup to enforce
that the credentials needed for live traffic are actually present.

Environment variable mapping is case-insensitive, e.g. ``RAZORPAY_KEY_ID``
populates ``razorpay_key_id``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

#: Settings that must be non-empty before the service can accept live
#: webhook traffic.  Checked once at startup, never at import time.
REQUIRED_FOR_STARTUP: tuple[str, ...] = (
    "razorpay_key_id",
    "razorpay_key_secret",
    "razorpay_webhook_secret",
)


class Settings(BaseSettings):
    """Central, typed configuration object shared by every module."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ #
    # Razorpay                                                           #
    # ------------------------------------------------------------------ #
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # ------------------------------------------------------------------ #
    # Groq (LLM failure diagnosis)                                       #
    # ------------------------------------------------------------------ #
    groq_api_key: str = ""
    groq_model: str = "llama-3.1-8b-instant"
    groq_timeout_seconds: float = 8.0

    # ------------------------------------------------------------------ #
    # Telegram Bot API (outreach channel)                                #
    # ------------------------------------------------------------------ #
    telegram_bot_token: str = ""
    telegram_api_base_url: str = "https://api.telegram.org"

    # ------------------------------------------------------------------ #
    # Application                                                        #
    # ------------------------------------------------------------------ #
    database_url: str = "sqlite:///./recovery_agent.db"
    log_level: str = "INFO"
    webhook_url: str = ""
    payment_success_callback_url: str = "https://your-domain.com/payment-success"

    # ------------------------------------------------------------------ #
    # Recovery tuning                                                    #
    # ------------------------------------------------------------------ #
    payment_link_expiry_hours: int = 24
    max_recovery_attempts_per_payment: int = 3
    outbound_rate_limit_per_minute: int = 30
    http_timeout_seconds: float = 15.0
    max_retries: int = 3
    retry_base_delay_seconds: float = 0.5

    def ensure_startup_ready(self) -> None:
        """Raise if mandatory production credentials are missing."""
        missing = [name for name in REQUIRED_FOR_STARTUP if not getattr(self, name)]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: "
                + ", ".join(name.upper() for name in missing)
                + ". Copy .env.example to .env and fill in real values."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton :class:`Settings` instance."""
    return Settings()
