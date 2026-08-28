"""Outbound messaging channel clients.

Currently a single channel is live:

* :class:`~app.channel_clients.telegram_client.TelegramClient`

A shared :class:`RateLimiter` (below) prevents outbound spam and protects the
Telegram bot's API quotas.  Additional channels can be added as sibling
modules implementing the same ``send_recovery_message(contact, message)``
interface.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class RateLimiter:
    """In-process sliding-window rate limiter for outbound messages.

    Guarantees at most ``max_events`` sends per ``window_seconds`` across the
    whole process, protecting customers from spam and provider quotas from
    being blown.  ``acquire()`` waits until capacity is available.
    """

    def __init__(
        self,
        max_events: int = 30,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_events = max(1, max_events)
        self._window_seconds = window_seconds
        self._clock = clock  # injectable for tests (fake time)
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(
        self, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    ) -> None:
        """Block until one send-slot is available."""
        while True:
            async with self._lock:
                now = self._clock()
                cutoff = now - self._window_seconds
                while self._events and self._events[0] <= cutoff:
                    self._events.popleft()
                if len(self._events) < self._max_events:
                    self._events.append(now)
                    return
                wait_for = self._events[0] + self._window_seconds - now
            logger.debug("rate_limiter_wait", extra={"wait_seconds": round(wait_for, 3)})
            await sleep(max(wait_for, 0.01))
