"""Tests for the sendreminder scheduler module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import FailedPayment, RecoveryAttempt
from app.sendreminder import (
    DEFAULT_COOLDOWN_SECONDS,
    REASON_COOLDOWNS,
    _build_orchestrator,
    dispatch_due_reminders,
    eligible_payments,
    get_cooldown_seconds,
    seconds_until_next_attempt,
    send_reminder,
)


# --------------------------------------------------------------------- #
# Fixtures                                                              #
# --------------------------------------------------------------------- #
@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def make_failed_payment(db, **overrides) -> FailedPayment:
    defaults = dict(
        payment_id="pay_rem_1",
        amount=129900,
        currency="INR",
        customer_name="Test User",
        customer_contact="12345",
        customer_email="test@example.com",
        payment_method="upi",
        failure_reason="insufficient_funds",
        recovery_status=FailedPayment.STATUS_PENDING,
    )
    defaults.update(overrides)
    payment = FailedPayment(**defaults)
    db.add(payment)
    db.commit()
    return payment


def add_attempt(db, failed_payment_id: int, **overrides) -> RecoveryAttempt:
    defaults = dict(
        failed_payment_id=failed_payment_id,
        channel="telegram",
        action="message_sent",
        message="test message",
        outcome="delivered",
    )
    defaults.update(overrides)
    attempt = RecoveryAttempt(**defaults)
    db.add(attempt)
    db.commit()
    return attempt


# --------------------------------------------------------------------- #
# get_cooldown_seconds                                                  #
# --------------------------------------------------------------------- #
class TestGetCooldownSeconds:
    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            ("insufficient_funds", 7200),
            ("bank_timeout", 1800),
            ("invalid_vpa", 3600),
            ("card_declined", 7200),
            ("otp_timeout", 1800),
            ("other", DEFAULT_COOLDOWN_SECONDS),
            (None, DEFAULT_COOLDOWN_SECONDS),
            ("unknown_reason", DEFAULT_COOLDOWN_SECONDS),
        ],
    )
    def test_cooldown_mapping(self, reason, expected):
        assert get_cooldown_seconds(reason) == expected

    def test_all_valid_reasons_have_cooldowns(self):
        from app.failure_classifier import VALID_REASONS

        for reason in VALID_REASONS:
            cd = get_cooldown_seconds(reason)
            assert cd > 0, f"cooldown for {reason} must be positive"


# --------------------------------------------------------------------- #
# seconds_until_next_attempt                                            #
# --------------------------------------------------------------------- #
class TestSecondsUntilNextAttempt:
    def test_recovered_payment_returns_negative(self, db):
        payment = make_failed_payment(
            db, recovery_status=FailedPayment.STATUS_RECOVERED
        )
        assert seconds_until_next_attempt(payment) == -1

    def test_unrecoverable_payment_returns_negative(self, db):
        payment = make_failed_payment(
            db, recovery_status=FailedPayment.STATUS_UNRECOVERABLE
        )
        assert seconds_until_next_attempt(payment) == -1

    def test_no_attempts_returns_zero(self, db):
        payment = make_failed_payment(db)
        assert seconds_until_next_attempt(payment) == 0.0

    def test_within_cooldown_returns_positive(self, db):
        payment = make_failed_payment(db)
        add_attempt(db, payment.id)
        remaining = seconds_until_next_attempt(payment, max_attempts=3)
        assert remaining > 0

    def test_after_cooldown_returns_zero(self, db):
        payment = make_failed_payment(db)
        attempt = add_attempt(db, payment.id)
        # Set attempt timestamp far in the past (beyond cooldown).
        attempt.timestamp = datetime.now(timezone.utc) - timedelta(hours=3)
        db.commit()
        remaining = seconds_until_next_attempt(payment, max_attempts=3)
        assert remaining == 0.0

    def test_capped_at_max_attempts_returns_negative(self, db):
        payment = make_failed_payment(db)
        for _ in range(3):
            add_attempt(db, payment.id)
        assert seconds_until_next_attempt(payment, max_attempts=3) == -1

    def test_naive_timestamp_handled(self, db):
        """Timestamps without tzinfo should not crash the scheduler."""
        payment = make_failed_payment(db)
        attempt = add_attempt(db, payment.id)
        attempt.timestamp = datetime.utcnow() - timedelta(hours=5)
        db.commit()
        remaining = seconds_until_next_attempt(payment, max_attempts=3)
        assert remaining == 0.0

    def test_in_progress_payment_eligible(self, db):
        payment = make_failed_payment(
            db, recovery_status=FailedPayment.STATUS_IN_PROGRESS
        )
        assert seconds_until_next_attempt(payment) == 0.0

    def test_custom_clock_within_cooldown(self, db):
        fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        payment = make_failed_payment(db)
        attempt = add_attempt(db, payment.id)
        attempt.timestamp = fixed - timedelta(minutes=10)
        db.commit()
        # bank_timeout cooldown = 1800s = 30min → still within window at +10min
        payment.failure_reason = "bank_timeout"
        db.commit()
        remaining = seconds_until_next_attempt(
            payment, max_attempts=3, clock=lambda: fixed
        )
        assert remaining == 1200.0  # 1800s cooldown - 600s elapsed

    def test_custom_clock_after_cooldown(self, db):
        fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        payment = make_failed_payment(db)
        attempt = add_attempt(db, payment.id)
        attempt.timestamp = fixed - timedelta(hours=3)
        db.commit()
        remaining = seconds_until_next_attempt(
            payment, max_attempts=3, clock=lambda: fixed
        )
        assert remaining == 0.0


# --------------------------------------------------------------------- #
# eligible_payments                                                     #
# --------------------------------------------------------------------- #
class TestEligiblePayments:
    def test_returns_pending_payments(self, db):
        make_failed_payment(db, payment_id="pay_a")
        make_failed_payment(
            db,
            payment_id="pay_b",
            recovery_status=FailedPayment.STATUS_RECOVERED,
        )
        due = eligible_payments(db)
        assert len(due) == 1
        assert due[0].payment_id == "pay_a"

    def test_returns_in_progress_payments(self, db):
        make_failed_payment(
            db,
            payment_id="pay_ip",
            recovery_status=FailedPayment.STATUS_IN_PROGRESS,
        )
        due = eligible_payments(db)
        assert len(due) == 1

    def test_excludes_unrecoverable(self, db):
        make_failed_payment(
            db,
            payment_id="pay_un",
            recovery_status=FailedPayment.STATUS_UNRECOVERABLE,
        )
        due = eligible_payments(db)
        assert len(due) == 0

    def test_excludes_recently_attempted(self, db):
        payment = make_failed_payment(db)
        add_attempt(db, payment.id)
        # Within cooldown → not eligible.
        due = eligible_payments(db)
        assert len(due) == 0

    def test_includes_after_cooldown(self, db):
        payment = make_failed_payment(db)
        attempt = add_attempt(db, payment.id)
        attempt.timestamp = datetime.now(timezone.utc) - timedelta(hours=3)
        db.commit()
        due = eligible_payments(db)
        assert len(due) == 1

    def test_excludes_capped_payments(self, db):
        payment = make_failed_payment(db)
        for _ in range(3):
            add_attempt(db, payment.id)
        due = eligible_payments(db, max_attempts=3)
        assert len(due) == 0

    def test_multiple_payments_mixed_eligibility(self, db):
        p1 = make_failed_payment(db, payment_id="pay_due")
        add_attempt(db, p1.id)
        # Still within cooldown.

        p2 = make_failed_payment(db, payment_id="pay_ready")
        a2 = add_attempt(db, p2.id)
        a2.timestamp = datetime.now(timezone.utc) - timedelta(hours=5)
        db.commit()

        make_failed_payment(
            db,
            payment_id="pay_recovered",
            recovery_status=FailedPayment.STATUS_RECOVERED,
        )

        due = eligible_payments(db)
        assert len(due) == 1
        assert due[0].payment_id == "pay_ready"


# --------------------------------------------------------------------- #
# send_reminder                                                         #
# --------------------------------------------------------------------- #
class TestSendReminder:
    @pytest.mark.asyncio
    async def test_returns_channel_on_success(self, db):
        payment = make_failed_payment(db)
        mock_orchestrator = AsyncMock()
        mock_orchestrator.execute_recovery.return_value = "telegram"

        with patch(
            "app.sendreminder._build_orchestrator", return_value=mock_orchestrator
        ):
            result = await send_reminder(payment, db)
        assert result == "telegram"
        mock_orchestrator.execute_recovery.assert_awaited_once_with(payment)

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self, db):
        payment = make_failed_payment(db)
        mock_orchestrator = AsyncMock()
        mock_orchestrator.execute_recovery.return_value = None

        with patch(
            "app.sendreminder._build_orchestrator", return_value=mock_orchestrator
        ):
            result = await send_reminder(payment, db)
        assert result is None


# --------------------------------------------------------------------- #
# dispatch_due_reminders                                                #
# --------------------------------------------------------------------- #
class TestDispatchDueReminders:
    @pytest.mark.asyncio
    async def test_no_eligible_payments(self, db):
        summary = await dispatch_due_reminders(db)
        assert summary == {"eligible": 0, "attempted": 0, "delivered": 0}

    @pytest.mark.asyncio
    async def test_dispatches_eligible_payment(self, db):
        payment = make_failed_payment(db)
        mock_orchestrator = AsyncMock()
        mock_orchestrator.execute_recovery.return_value = "telegram"

        with patch(
            "app.sendreminder._build_orchestrator", return_value=mock_orchestrator
        ):
            summary = await dispatch_due_reminders(db)

        assert summary["eligible"] == 1
        assert summary["attempted"] == 1
        assert summary["delivered"] == 1

    @pytest.mark.asyncio
    async def test_handles_send_failure_gracefully(self, db):
        payment = make_failed_payment(db)
        mock_orchestrator = AsyncMock()
        mock_orchestrator.execute_recovery.return_value = None

        with patch(
            "app.sendreminder._build_orchestrator", return_value=mock_orchestrator
        ):
            summary = await dispatch_due_reminders(db)

        assert summary["eligible"] == 1
        assert summary["attempted"] == 1
        assert summary["delivered"] == 0

    @pytest.mark.asyncio
    async def test_handles_exception_in_send_gracefully(self, db):
        payment = make_failed_payment(db)

        with patch(
            "app.sendreminder._build_orchestrator",
            side_effect=RuntimeError("boom"),
        ):
            summary = await dispatch_due_reminders(db)

        assert summary["eligible"] == 1
        assert summary["attempted"] == 1
        assert summary["delivered"] == 0

    @pytest.mark.asyncio
    async def test_mix_of_eligible_and_ineligible(self, db):
        p1 = make_failed_payment(db, payment_id="pay_a")
        a1 = add_attempt(db, p1.id)
        a1.timestamp = datetime.now(timezone.utc) - timedelta(hours=5)
        db.commit()

        make_failed_payment(
            db,
            payment_id="pay_b",
            recovery_status=FailedPayment.STATUS_RECOVERED,
        )

        make_failed_payment(db, payment_id="pay_c")

        mock_orchestrator = AsyncMock()
        mock_orchestrator.execute_recovery.return_value = "telegram"

        with patch(
            "app.sendreminder._build_orchestrator", return_value=mock_orchestrator
        ):
            summary = await dispatch_due_reminders(db)

        assert summary["eligible"] == 2
        assert summary["delivered"] == 2


# --------------------------------------------------------------------- #
# _build_orchestrator                                                   #
# --------------------------------------------------------------------- #
class TestBuildOrchestrator:
    def test_returns_orchestrator_instance(self, db):
        from app.recovery_orchestrator import RecoveryOrchestrator

        orch = _build_orchestrator(db)
        assert isinstance(orch, RecoveryOrchestrator)


# --------------------------------------------------------------------- #
# CLI dry-run output                                                    #
# --------------------------------------------------------------------- #
class TestCliDryRun:
    def test_dry_run_prints_eligible(self, db, capsys, monkeypatch):
        make_failed_payment(db, payment_id="pay_dry")

        # Patch SessionLocal (imported inside _cli_main at call time) so
        # _cli_main's DB is our test fixture's in-memory DB.
        monkeypatch.setattr("app.database.SessionLocal", lambda: db)
        monkeypatch.setattr("sys.argv", ["sendreminder", "--dry-run"])

        from app.sendreminder import _cli_main
        import contextlib

        with contextlib.suppress(SystemExit):
            _cli_main()

        captured = capsys.readouterr()
        assert "pay_dry" in captured.out
        assert "1 payment(s) eligible" in captured.out
