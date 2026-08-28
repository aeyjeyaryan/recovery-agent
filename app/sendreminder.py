"""Scheduled reminder dispatcher for failed payments still in recovery.

This module implements the **re-attempt scheduler** (see README roadmap):
it periodically scans ``failed_payments`` for rows stuck in ``pending`` or
``in_progress`` that haven't exhausted their attempt budget, then triggers
the recovery orchestrator for each one.

Per-reason cooldown windows prevent spamming a customer whose bank is still
down, while the per-payment attempt cap (default 3) is inherited from
the orchestrator and respected here.

Designed to run as either:
- A FastAPI background task launched at startup, **or**
- A standalone CLI script (``python -m app.sendreminder``).

All external collaborators (DB session, orchestrator) are injectable so
unit tests stay hermetic.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.audit_logger import AuditLogger
from app.channel_clients.telegram_client import TelegramClient
from app.models import FailedPayment, RecoveryAttempt
from app.payment_link_creator import PaymentLinkCreator
from app.recovery_orchestrator import RecoveryOrchestrator

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Per-reason cooldown windows                                            #
# --------------------------------------------------------------------- #

#: Minimum wait (seconds) between recovery attempts, keyed by diagnosed
#: failure reason.  The scheduler skips a payment if its most recent
#: attempt happened less than ``reason_cooldown`` seconds ago.
#:
#: Rationale:
#:   insufficient_funds → 2 h  (customer may need time to top up)
#:   bank_timeout       → 30 m (transient; retry sooner)
#:   invalid_vpa        → 1 h  (customer may correct the VPA)
#:   card_declined      → 2 h  (customer may switch payment method)
#:   otp_timeout        → 30 m (just timed out; quick retry is fine)
#:   other              → 1 h  (conservative default)
REASON_COOLDOWNS: dict[str, int] = {
    "insufficient_funds": 7200,   # 2 hours
    "bank_timeout": 1800,         # 30 minutes
    "invalid_vpa": 3600,          # 1 hour
    "card_declined": 7200,        # 2 hours
    "otp_timeout": 1800,          # 30 minutes
}
DEFAULT_COOLDOWN_SECONDS = 3600  # 1 hour for unknown/other reasons

#: Default polling interval when run as a standalone loop.
DEFAULT_POLL_INTERVAL_SECONDS = 300  # 5 minutes


# --------------------------------------------------------------------- #
# Core functions                                                        #
# --------------------------------------------------------------------- #

def get_cooldown_seconds(failure_reason: str | None) -> int:
    """Return the cooldown window for a diagnosed failure reason."""
    if not failure_reason:
        return DEFAULT_COOLDOWN_SECONDS
    return REASON_COOLDOWNS.get(failure_reason, DEFAULT_COOLDOWN_SECONDS)


def seconds_until_next_attempt(
    failed_payment: FailedPayment,
    max_attempts: int = 3,
    clock: Callable[[], datetime] | None = None,
) -> float:
    """Return seconds remaining before the next reminder is due.

    Returns ``0`` when the payment is eligible for an immediate retry.
    Returns ``-1`` when the payment is fully capped / unrecoverable.

    Args:
        failed_payment: ORM row to evaluate.
        max_attempts: per-payment attempt ceiling.
        clock: injectable clock (tests pass a fixed datetime).
    """
    now = clock() if clock else datetime.now(timezone.utc)

    # Already recovered or permanently given up → nothing to do.
    if failed_payment.recovery_status in (
        FailedPayment.STATUS_RECOVERED,
        FailedPayment.STATUS_UNRECOVERABLE,
    ):
        return -1

    # Count existing attempts.
    attempts: list[RecoveryAttempt] = sorted(
        failed_payment.recovery_attempts or [],
        key=lambda a: a.timestamp or datetime.min.replace(tzinfo=timezone.utc),
    )
    if len(attempts) >= max_attempts:
        return -1

    # If no attempts have been made yet, eligible immediately.
    if not attempts:
        return 0.0

    # Evaluate cooldown from the *last* attempt.
    last_attempt = attempts[-1]
    last_ts = last_attempt.timestamp
    if last_ts is None:
        return 0.0
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)

    cooldown = get_cooldown_seconds(failed_payment.failure_reason)
    eligible_at = last_ts + timedelta(seconds=cooldown)
    remaining = (eligible_at - now).total_seconds()
    return max(remaining, 0.0)


def eligible_payments(
    db: Session,
    max_attempts: int = 3,
    clock: Callable[[], datetime] | None = None,
) -> list[FailedPayment]:
    """Query for failed payments that are due for a reminder.

    Returns payments with ``recovery_status`` in {pending, in_progress}
    that have fewer than ``max_attempts`` attempts AND whose cooldown
    window has elapsed.
    """
    candidates = (
        db.query(FailedPayment)
        .filter(
            FailedPayment.recovery_status.in_(
                [FailedPayment.STATUS_PENDING, FailedPayment.STATUS_IN_PROGRESS]
            )
        )
        .all()
    )
    due: list[FailedPayment] = []
    for payment in candidates:
        remaining = seconds_until_next_attempt(
            payment, max_attempts=max_attempts, clock=clock
        )
        if remaining == 0.0:
            due.append(payment)
    return due


# --------------------------------------------------------------------- #
# Orchestrator wrapper                                                  #
# --------------------------------------------------------------------- #

def _build_orchestrator(
    db: Session,
    settings: Any | None = None,
    telegram_client: TelegramClient | None = None,
) -> RecoveryOrchestrator:
    """Build a fully-wired orchestrator.  DI seam for tests."""
    return RecoveryOrchestrator(
        db=db,
        settings=settings,
        payment_link_creator=PaymentLinkCreator(settings=settings),
        telegram_client=telegram_client,
        audit_logger=AuditLogger(db=db),
    )


async def send_reminder(
    failed_payment: FailedPayment,
    db: Session,
    settings: Any | None = None,
    telegram_client: TelegramClient | None = None,
) -> str | None:
    """Trigger a single recovery attempt for one failed payment.

    Returns the channel used (e.g. ``"telegram"``) if delivered, else
    ``None``.  Respects the per-payment attempt cap via the orchestrator.
    """
    orchestrator = _build_orchestrator(db, settings, telegram_client)
    return await orchestrator.execute_recovery(failed_payment)


async def dispatch_due_reminders(
    db: Session,
    settings: Any | None = None,
    telegram_client: TelegramClient | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Scan for due payments and dispatch reminders.

    Returns a summary dict with ``eligible``, ``attempted``, and
    ``delivered`` counts for logging / monitoring.

    This is the main entry point called by the background loop or a
    scheduled job.
    """
    max_attempts = int(
        getattr(settings, "max_recovery_attempts_per_payment", 3)
        if settings
        else 3
    )
    due = eligible_payments(db, max_attempts=max_attempts, clock=clock)
    attempted = 0
    delivered = 0

    logger.info(
        "reminder_scan_complete",
        extra={"eligible_count": len(due)},
    )

    for payment in due:
        attempted += 1
        try:
            channel = await send_reminder(
                payment, db, settings, telegram_client
            )
            if channel:
                delivered += 1
                logger.info(
                    "reminder_sent",
                    extra={
                        "payment_id": payment.payment_id,
                        "channel": channel,
                    },
                )
            else:
                logger.warning(
                    "reminder_delivery_failed",
                    extra={"payment_id": payment.payment_id},
                )
        except Exception:  # noqa: BLE001 - never crash the loop
            logger.exception(
                "reminder_exception",
                extra={"payment_id": payment.payment_id},
            )

    summary = {
        "eligible": len(due),
        "attempted": attempted,
        "delivered": delivered,
    }
    logger.info("reminder_dispatch_summary", extra=summary)
    return summary


# --------------------------------------------------------------------- #
# Standalone background loop                                            #
# --------------------------------------------------------------------- #

async def run_reminder_loop(
    settings: Any | None = None,
    poll_interval_seconds: float | None = None,
) -> None:
    """Run the reminder scanner on a repeating poll loop.

    Intended to be launched as a FastAPI background task at startup::

        from app.sendreminder import run_reminder_loop
        background_tasks.add_task(run_reminder_loop, settings=settings)

    Or invoked directly::

        python -m app.sendreminder
    """
    import asyncio

    if poll_interval_seconds is None:
        poll_interval_seconds = float(
            getattr(settings, "reminder_poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
            if settings
            else DEFAULT_POLL_INTERVAL_SECONDS
        )

    # Import here to avoid circular imports at module level.
    from app.database import SessionLocal

    logger.info(
        "reminder_loop_started",
        extra={"poll_interval_seconds": poll_interval_seconds},
    )

    while True:
        await asyncio.sleep(poll_interval_seconds)
        db = SessionLocal()
        try:
            await dispatch_due_reminders(db, settings=settings)
        except Exception:  # noqa: BLE001
            logger.exception("reminder_loop_iteration_failed")
        finally:
            db.close()


# --------------------------------------------------------------------- #
# Standalone CLI entrypoint                                             #
# --------------------------------------------------------------------- #

def _cli_main() -> None:
    """CLI entrypoint: run one scan pass then exit (for cron / one-shot)."""
    import argparse

    from app.logging_config import setup_logging

    parser = argparse.ArgumentParser(
        description="Send reminders for due failed payments (one-shot scan)."
    )
    parser.add_argument(
        "--poll", action="store_true",
        help="Run continuously instead of a single scan.",
    )
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Seconds between polls in continuous mode (default: %(default)s).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show eligible payments without sending reminders.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        help="Log level (default: %(default)s).",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)

    from config import get_settings

    settings = get_settings()

    if args.dry_run:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            max_attempts = int(
                getattr(settings, "max_recovery_attempts_per_payment", 3)
            )
            due = eligible_payments(db, max_attempts=max_attempts)
            if not due:
                print("No payments due for a reminder right now.")
                return
            print(f"{len(due)} payment(s) eligible for reminder:\n")
            for p in due:
                remaining = max_attempts - len(p.recovery_attempts or [])
                print(
                    f"  {p.payment_id}  ₹{(p.amount or 0) / 100:g}  "
                    f"reason={p.failure_reason or 'unknown'}  "
                    f"attempts_left={remaining}  "
                    f"status={p.recovery_status}"
                )
        finally:
            db.close()
        return

    import asyncio

    if args.poll:
        asyncio.run(run_reminder_loop(settings, args.interval))
    else:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            summary = asyncio.run(
                dispatch_due_reminders(db, settings=settings)
            )
            print(
                f"Done. eligible={summary['eligible']} "
                f"attempted={summary['attempted']} "
                f"delivered={summary['delivered']}"
            )
        finally:
            db.close()


if __name__ == "__main__":
    _cli_main()
