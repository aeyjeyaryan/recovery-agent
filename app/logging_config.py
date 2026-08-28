"""Structured JSON logging with correlation IDs.

Every log record is emitted as a single JSON line containing the timestamp,
level, logger name, message and the current correlation ID (set to the
Razorpay ``payment_id`` while a failure is being processed).  This keeps logs
greppable/traceable in any JSON-aware log pipeline.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone

#: Correlation ID for the current async task/context (usually a payment_id).
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)


def set_correlation_id(value: str) -> None:
    """Set the correlation ID for the current context."""
    correlation_id_var.set(value)


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
        }
        # Merge structured fields passed via `extra={"key": ...}`.
        standard = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)))
        for key, value in record.__dict__.items():
            if key not in standard and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger to emit structured JSON to stdout."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    # Quiet down noisy third-party loggers unless debugging.
    if level.upper() not in ("DEBUG",):
        for noisy in ("httpx", "httpcore", "groq", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
