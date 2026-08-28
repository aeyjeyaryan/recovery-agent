"""Shared async retry helper with exponential backoff.

All external API calls (Razorpay, Groq, Telegram) funnel through
:func:`with_retries` so that transient failures are retried uniformly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetriesExhaustedError(RuntimeError):
    """Raised when every retry attempt for an operation has failed."""

    def __init__(self, name: str, last_error: BaseException) -> None:
        self.last_error = last_error
        super().__init__(f"{name}: retries exhausted (last error: {last_error})")


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    name: str,
    max_retries: int = 3,
    base_delay_seconds: float = 0.5,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``operation`` retrying transient failures with exponential backoff.

    Args:
        operation: Zero-arg async callable to execute.
        name: Human-readable operation name used in logs/errors.
        max_retries: Total attempts allowed (including the first).
        base_delay_seconds: Delay before the first retry; doubled each round.
        retry_on: Exception types considered retryable.
        sleep: Injectable sleep (tests patch this to avoid real waiting).

    Returns:
        The successful result of ``operation``.

    Raises:
        RetriesExhaustedError: If all attempts fail. The original exception is
            available on ``err.last_error``.
    """
    last_error: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return await operation()
        except retry_on as exc:  # type: ignore[misc]
            last_error = exc
            if attempt == max_retries:
                logger.error(
                    "operation_failed_permanently",
                    extra={"operation": name, "attempts": attempt, "error": str(exc)},
                )
                break
            delay = base_delay_seconds * (2 ** (attempt - 1))
            logger.warning(
                "operation_retry_scheduled",
                extra={
                    "operation": name,
                    "attempt": attempt,
                    "delay_seconds": delay,
                    "error": str(exc),
                },
            )
            await sleep(delay)
    raise RetriesExhaustedError(name, last_error or Exception("unknown"))
