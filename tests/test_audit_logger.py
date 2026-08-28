"""AuditLogger tests plus cross-cutting checks (rate limiter, JSON logs)."""

from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit_logger import AuditLogger
from app.channel_clients import RateLimiter
from app.database import Base
from app.logging_config import JsonFormatter, set_correlation_id, setup_logging
from app.models import FailedPayment, RecoveryAttempt


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


@pytest.fixture()
def failed_payment(db) -> FailedPayment:
    payment = FailedPayment(
        payment_id="pay_audit_1",
        amount=129900,
        currency="INR",
        customer_name="Rahul",
        recovery_status=FailedPayment.STATUS_PENDING,
    )
    db.add(payment)
    db.commit()
    return payment


# --------------------------------------------------------------------- #
# log_attempt                                                           #
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_log_attempt_persists_all_fields(db, failed_payment):
    audit = AuditLogger(db)

    attempt = await audit.log_attempt(
        failed_payment_id=failed_payment.id,
        channel="telegram",
        action="message_sent",
        message="Hi Rahul, complete your purchase: https://rzp.io/i/x",
        outcome="delivered",
        payment_link_id="plink_9",
    )

    stored = db.query(RecoveryAttempt).one()
    assert stored.id == attempt.id
    assert stored.failed_payment_id == failed_payment.id
    assert stored.channel == "telegram"
    assert stored.action == "message_sent"
    assert stored.outcome == "delivered"
    assert stored.payment_link_id == "plink_9"
    assert stored.timestamp is not None


@pytest.mark.asyncio
async def test_log_attempt_defaults_optional_fields(db, failed_payment):
    attempt = await AuditLogger(db).log_attempt(
        failed_payment_id=failed_payment.id,
        channel="telegram",
        action="call_initiated",
        message="<Response><Say>Hi</Say></Response>",
        outcome="delivered",
    )
    assert attempt.payment_link_id is None
    assert attempt.recovery_amount is None


# --------------------------------------------------------------------- #
# Query helpers                                                         #
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_count_attempts_and_recent_attempts(db, failed_payment):
    audit = AuditLogger(db)
    for i in range(3):
        await audit.log_attempt(
            failed_payment_id=failed_payment.id,
            channel="telegram",
            action="message_sent",
            message=f"reminder {i}",
            outcome="delivered",
        )
    assert audit.count_attempts(failed_payment.id) == 3
    recent = audit.recent_attempts(limit=2)
    assert len(recent) == 2


@pytest.mark.asyncio
async def test_attempts_for_serialisation(db, failed_payment):
    audit = AuditLogger(db)
    await audit.log_attempt(
        failed_payment_id=failed_payment.id,
        channel="telegram",
        action="message_sent",
        message="hello",
        outcome="delivered",
        payment_link_id="plink_1",
    )
    rows = audit.attempts_for(failed_payment.id)
    assert rows == [
        {
            "id": rows[0]["id"],
            "timestamp": rows[0]["timestamp"],
            "channel": "telegram",
            "action": "message_sent",
            "outcome": "delivered",
            "payment_link_id": "plink_1",
        }
    ]


def test_relationship_back_populates(db, failed_payment):
    db.add(
        RecoveryAttempt(
            failed_payment_id=failed_payment.id,
            channel="telegram",
            action="message_sent",
            message="m",
            outcome="delivered",
        )
    )
    db.commit()
    assert len(failed_payment.recovery_attempts) == 1
    assert failed_payment.recovery_attempts[0].failed_payment.payment_id == "pay_audit_1"


# --------------------------------------------------------------------- #
# Cross-cutting: structured logging                                     #
# --------------------------------------------------------------------- #
class TestStructuredLogging:
    def test_json_formatter_emits_parseable_line(self):
        setup_logging("INFO")
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello %s", args=("world",), exc_info=None,
        )
        payload = json.loads(formatter.format(record))
        assert payload["message"] == "hello world"
        assert payload["level"] == "INFO"
        assert "correlation_id" in payload
        assert "timestamp" in payload

    def test_extra_fields_are_included(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="event", args=None, exc_info=None,
        )
        record.payment_id = "pay_123"
        payload = json.loads(formatter.format(record))
        assert payload["payment_id"] == "pay_123"

    def test_correlation_id_contextvar(self):
        set_correlation_id("pay_corr_42")
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="x", args=None, exc_info=None,
        )
        assert json.loads(formatter.format(record))["correlation_id"] == "pay_corr_42"


# --------------------------------------------------------------------- #
# Cross-cutting: outbound rate limiter                                  #
# --------------------------------------------------------------------- #
class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_up_to_limit_immediately(self):
        limiter = RateLimiter(max_events=3, window_seconds=60)
        for _ in range(3):
            await limiter.acquire()

    @pytest.mark.asyncio
    async def test_blocks_beyond_limit_then_recovers(self):
        # Fake clock advanced by the injected sleep so the sliding window can
        # be simulated without real waiting.
        now = {"t": 0.0}
        sleeps: list[float] = []

        def fake_clock() -> float:
            return now["t"]

        async def advancing_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now["t"] += seconds

        limiter = RateLimiter(max_events=2, window_seconds=10, clock=fake_clock)
        await limiter.acquire(sleep=advancing_sleep)  # t=0
        await limiter.acquire(sleep=advancing_sleep)  # t=0
        await limiter.acquire(sleep=advancing_sleep)  # must wait until slot frees

        assert len(sleeps) == 1
        assert sleeps[0] > 0
        assert abs(now["t"] - 10.0) < 1e-9  # waited exactly one window

    @pytest.mark.asyncio
    async def test_window_slides_with_time(self):
        now = {"t": 100.0}

        def fake_clock() -> float:
            return now["t"]

        async def no_sleep(seconds: float) -> None:  # pragma: no cover
            raise AssertionError("should not need to wait")

        limiter = RateLimiter(max_events=2, window_seconds=5, clock=fake_clock)
        await limiter.acquire(sleep=no_sleep)   # t=100
        now["t"] = 200.0                        # window has fully slid past
        await limiter.acquire(sleep=no_sleep)   # old entries expired, no wait
