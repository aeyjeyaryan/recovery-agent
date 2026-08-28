"""RecoveryOrchestrator tests: Telegram delivery, guards, and audit outcomes."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit_logger import AuditLogger
from app.channel_clients.telegram_client import TelegramClient, TelegramError
from app.database import Base
from app.models import FailedPayment, RecoveryAttempt
from app.payment_link_creator import PaymentLinkError
from app.recovery_orchestrator import (
    DEFAULT_CHANNEL,
    DEFAULT_INTERVENTION,
    ESCALATION_TONES,
    REASON_INTERVENTIONS,
    RecoveryOrchestrator,
    escalation_stage,
    normalize_phone,
    resolve_recipient,
    resolve_recipient_candidates,
)
from config import Settings


# --------------------------------------------------------------------- #
# Fakes                                                                 #
# --------------------------------------------------------------------- #
class FakeLinkCreator:
    def __init__(self) -> None:
        self.last_link_id = "plink_fake_1"
        self.calls = 0

    async def create_payment_link(self, failed_payment) -> str:  # noqa: ANN001
        self.calls += 1
        return "https://rzp.io/i/fake"


class FailingLinkCreator(FakeLinkCreator):
    async def create_payment_link(self, failed_payment) -> str:  # noqa: ANN001
        self.calls += 1
        raise PaymentLinkError("razorpay down")


class FakeTelegram:
    """Records sends; stands in for the TelegramClient."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    async def send_recovery_message(self, contact: str, message: str) -> dict:
        if self.fail:
            raise RuntimeError("telegram unavailable")
        self.sent.append((contact, message))
        return {"message_id": 42, "status": "sent"}


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
        payment_id="pay_test_1",
        amount=129900,
        currency="INR",
        customer_name="Rahul Sharma",
        customer_contact="+919876543210",
        customer_email="rahul@example.com",
        payment_method="upi",
        failure_reason="insufficient_funds",
        recovery_status=FailedPayment.STATUS_PENDING,
    )
    defaults.update(overrides)
    payment = FailedPayment(**defaults)
    db.add(payment)
    db.commit()
    return payment


def build_orchestrator(db, *, link=None, telegram=None):
    settings = Settings(retry_base_delay_seconds=0.0)
    return RecoveryOrchestrator(
        db=db,
        settings=settings,
        payment_link_creator=link or FakeLinkCreator(),
        telegram_client=telegram or FakeTelegram(),
        audit_logger=AuditLogger(db),
    )


# --------------------------------------------------------------------- #
# Channel selection                                                     #
# --------------------------------------------------------------------- #
class TestSelectChannel:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"amount": 750000},                       # high value
            {"amount": 100000, "failure_reason": "invalid_vpa"},
            {"amount": 100000, "failure_reason": "insufficient_funds"},
            {"amount": 100000, "failure_reason": "card_declined"},
            {},
        ],
    )
    def test_single_channel_is_telegram(self, overrides):
        payment = FailedPayment(payment_id="pay_x", **overrides)
        assert RecoveryOrchestrator.select_channel(payment) == DEFAULT_CHANNEL == "telegram"


class TestNormalizePhone:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("+919876543210", "+919876543210"),
            ("+91 98765 43210", "+919876543210"),
            ("919876543210", "+919876543210"),
            (None, None),
            ("", None),
            ("not-a-number", None),
        ],
    )
    def test_normalisation(self, raw, expected):
        assert normalize_phone(raw) == expected


class TestResolveRecipient:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("123456789", "123456789"),          # chat_id passthrough
            (" 987654321 ", "987654321"),        # digit-only, trimmed
            ("+919876543210", "+919876543210"),  # phone -> E.164
            ("+91 98765 43210", "+919876543210"),
            (None, None),
            ("", None),
            ("not-a-number", None),
        ],
    )
    def test_recipient_resolution(self, raw, expected):
        assert resolve_recipient(raw) == expected

    @pytest.mark.asyncio
    async def test_digit_only_contact_reaches_telegram_untouched(self, db):
        payment = make_failed_payment(db, customer_contact="123456789")
        telegram = FakeTelegram()
        await build_orchestrator(db, telegram=telegram).execute_recovery(payment)
        assert telegram.sent[0][0] == "123456789"


class TestResolveRecipientCandidates:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # chat_id passthrough: single candidate
            ("123456789", ["123456789"]),
            # Razorpay E.164-normalised chat_id: bare digits fallback added
            ("+916789477144", ["+916789477144", "6789477144"]),
            ("+1 415 555 0100", ["+14155550100", "4155550100"]),
            # no recognised country code: full digits appended
            ("+49 30 1234", ["+49301234", "49301234"]),
            (None, []),
            ("", []),
            ("not-a-number", []),
        ],
    )
    def test_candidates(self, raw, expected):
        assert resolve_recipient_candidates(raw) == expected

    @pytest.mark.asyncio
    async def test_e164_prefixed_chat_id_falls_back_to_bare_digits(self, db):
        """Razorpay turns '6789477144' into '+916789477144'; we still deliver."""

        class PhoneRejectingTelegram:
            """Mimics real Telegram: phone-shaped addresses are undeliverable."""

            def __init__(self) -> None:
                self.attempted: list[str] = []
                self.sent: list[tuple[str, str]] = []

            async def send_recovery_message(
                self, contact: str, message: str
            ) -> dict:
                self.attempted.append(contact)
                if contact.startswith("+"):
                    raise RuntimeError("chat not found")
                self.sent.append((contact, message))
                return {"message_id": 42, "status": "sent"}

        payment = make_failed_payment(db, customer_contact="+916789477144")
        telegram = PhoneRejectingTelegram()
        await build_orchestrator(db, telegram=telegram).execute_recovery(payment)
        assert telegram.attempted == ["+916789477144", "6789477144"]
        assert telegram.sent == [("6789477144", telegram.sent[0][1])]
        assert payment.recovery_status == FailedPayment.STATUS_IN_PROGRESS


# --------------------------------------------------------------------- #
# Message generation                                                    #
# --------------------------------------------------------------------- #
def test_generate_message_contains_details():
    payment = FailedPayment(payment_id="pay_XYZ", amount=250000, customer_name="Asha")
    message = RecoveryOrchestrator.generate_message(payment, "https://rzp.io/i/x")
    assert "Asha" in message
    assert "\u20b92500" in message
    assert "pay_XYZ" in message
    assert "https://rzp.io/i/x" in message


def test_generate_message_handles_missing_name():
    payment = FailedPayment(payment_id="pay_XYZ", amount=250000)
    message = RecoveryOrchestrator.generate_message(payment, "https://rzp.io/i/x")
    assert "Hi there" in message


# --------------------------------------------------------------------- #
# Per-reason interventions                                              #
# --------------------------------------------------------------------- #
class TestReasonInterventions:
    def test_every_valid_reason_has_intervention_copy(self):
        from app.failure_classifier import VALID_REASONS

        missing = [r for r in VALID_REASONS if r not in REASON_INTERVENTIONS]
        assert not missing, f"no intervention copy for: {missing}"

    @pytest.mark.parametrize(
        ("reason", "phrase"),
        [
            ("insufficient_funds", "balance"),
            ("bank_timeout", "timed out"),
            ("invalid_vpa", "upi id"),
            ("card_declined", "declined"),
            ("otp_timeout", "otp"),
        ],
    )
    def test_reason_specific_copy_is_used(self, reason, phrase):
        payment = FailedPayment(
            payment_id="pay_R1", amount=100000, failure_reason=reason
        )
        message = RecoveryOrchestrator.build_message(
            payment, "https://rzp.io/i/x", attempt_number=1
        )
        assert phrase in message

    def test_unknown_reason_falls_back_to_default(self):
        payment = FailedPayment(
            payment_id="pay_R2", amount=100000, failure_reason="mystery"
        )
        message = RecoveryOrchestrator.build_message(payment, "https://rzp.io/i/x")
        assert DEFAULT_INTERVENTION.capitalize() in message

    def test_missing_reason_uses_default(self):
        assert (
            RecoveryOrchestrator.reason_intervention(None) == DEFAULT_INTERVENTION
        )


# --------------------------------------------------------------------- #
# Escalation ladder                                                     #
# --------------------------------------------------------------------- #
class TestEscalationLadder:
    @pytest.mark.parametrize(
        ("attempt", "stage"),
        [(0, 1), (1, 1), (2, 2), (3, 3), (10, 3), (-5, 1)],
    )
    def test_stage_clamping(self, attempt, stage):
        assert escalation_stage(attempt) == stage

    def test_stage_count_matches_tones(self):
        assert len(ESCALATION_TONES) == 3

    def test_first_attempt_is_gentle(self):
        payment = FailedPayment(payment_id="pay_E1", amount=100000)
        message = RecoveryOrchestrator.build_message(
            payment, "https://rzp.io/i/x", attempt_number=1
        )
        assert "expires" not in message
        assert "Final reminder" not in message

    def test_second_attempt_adds_expiry_urgency(self):
        payment = FailedPayment(payment_id="pay_E2", amount=100000)
        message = RecoveryOrchestrator.build_message(
            payment, "https://rzp.io/i/x", attempt_number=2
        )
        assert "expires in 24h" in message  # default expiry setting
        assert "Final reminder" not in message

    def test_third_attempt_promises_to_stop(self):
        payment = FailedPayment(payment_id="pay_E3", amount=100000)
        message = RecoveryOrchestrator.build_message(
            payment, "https://rzp.io/i/x", attempt_number=3
        )
        assert "Final reminder" in message
        assert "stop reaching out" in message

    @pytest.mark.asyncio
    async def test_retry_after_delivery_escalates_tone(self, db):
        """A second execute_recovery run must sound firmer than the first."""
        payment = make_failed_payment(db)
        telegram = FakeTelegram()
        orchestrator = build_orchestrator(db, telegram=telegram)

        await orchestrator.execute_recovery(payment)
        first_message = telegram.sent[0][1]

        await orchestrator.execute_recovery(payment)
        second_message = telegram.sent[1][1]

        assert "expires" not in first_message.lower()
        assert "expires in 24h" in second_message.lower()
        # The retry is audited as its own attempt.
        assert db.query(RecoveryAttempt).count() == 2


# --------------------------------------------------------------------- #
# Workflow execution                                                    #
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_happy_path_telegram_delivery(db):
    payment = make_failed_payment(db)
    link = FakeLinkCreator()
    telegram = FakeTelegram()

    used = await build_orchestrator(db, link=link, telegram=telegram).execute_recovery(
        payment
    )

    assert used == "telegram"
    assert link.calls == 1
    assert telegram.sent[0][0] == "+919876543210"
    assert "https://rzp.io/i/fake" in telegram.sent[0][1]
    assert payment.recovery_status == FailedPayment.STATUS_IN_PROGRESS

    attempt = db.query(RecoveryAttempt).one()
    assert attempt.channel == "telegram"
    assert attempt.action == "message_sent"
    assert attempt.outcome == "delivered"
    assert attempt.payment_link_id == "plink_fake_1"


@pytest.mark.asyncio
async def test_delivery_failure_marks_attempt_failed_and_keeps_pending(db):
    payment = make_failed_payment(db)

    used = await build_orchestrator(
        db, telegram=FakeTelegram(fail=True)
    ).execute_recovery(payment)

    assert used is None
    # Stays pending so a later event/sweeper can retry.
    assert payment.recovery_status == FailedPayment.STATUS_PENDING
    attempt = db.query(RecoveryAttempt).one()
    assert attempt.channel == "telegram"
    assert attempt.outcome == "failed"


@pytest.mark.asyncio
async def test_missing_contact_skips_delivery_but_creates_no_false_success(db):
    payment = make_failed_payment(db, customer_contact=None)

    used = await build_orchestrator(db).execute_recovery(payment)

    assert used is None
    assert payment.recovery_status == FailedPayment.STATUS_PENDING
    attempt = db.query(RecoveryAttempt).one()
    assert attempt.outcome == "failed"


@pytest.mark.asyncio
async def test_payment_link_failure_aborts_without_attempts(db):
    payment = make_failed_payment(db)
    orchestrator = build_orchestrator(db, link=FailingLinkCreator())

    used = await orchestrator.execute_recovery(payment)

    assert used is None
    assert payment.recovery_status == FailedPayment.STATUS_PENDING
    assert db.query(RecoveryAttempt).count() == 0


@pytest.mark.asyncio
async def test_attempt_cap_marks_payment_unrecoverable(db):
    payment = make_failed_payment(db)
    for _ in range(3):  # max_recovery_attempts_per_payment default = 3
        db.add(RecoveryAttempt(failed_payment_id=payment.id, channel="telegram"))
    db.commit()
    orchestrator = build_orchestrator(db, link=FakeLinkCreator())

    used = await orchestrator.execute_recovery(payment)

    assert used is None
    assert payment.recovery_status == FailedPayment.STATUS_UNRECOVERABLE


# --------------------------------------------------------------------- #
# TelegramClient (real client over a mocked HTTP transport)             #
# --------------------------------------------------------------------- #
def telegram_settings() -> Settings:
    return Settings(
        telegram_bot_token="TESTTOKEN",
        retry_base_delay_seconds=0.0,
        max_retries=3,
    )


def make_client(handler, settings: Settings | None = None) -> tuple[TelegramClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording_handler)
    client = TelegramClient(
        settings=settings or telegram_settings(),
        http_client=httpx.AsyncClient(transport=transport),
    )
    return client, calls


class TestTelegramClient:
    @pytest.mark.asyncio
    async def test_send_success_returns_message_id(self):
        client, calls = make_client(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
        )
        response = await client.send_recovery_message("12345", "hello")

        assert response == {"message_id": 7, "status": "sent"}
        assert str(calls[0].url) == "https://api.telegram.org/botTESTTOKEN/sendMessage"
        body = calls[0].read().decode()
        assert '"chat_id": "12345"' in body or '"chat_id":"12345"' in body
        assert "hello" in body

    @pytest.mark.asyncio
    async def test_rate_limited_response_is_retried(self):
        state = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            state["n"] += 1
            if state["n"] < 3:
                return httpx.Response(429, json={"ok": False})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})

        client, calls = make_client(handler)
        response = await client.send_recovery_message("12345", "retry me")
        assert response["message_id"] == 8
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_permanent_error_fails_fast_without_retry(self):
        client, calls = make_client(
            lambda request: httpx.Response(400, json={"ok": False, "description": "chat not found"})
        )
        with pytest.raises(TelegramError):
            await client.send_recovery_message("bad-chat", "hi")
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_ok_false_payload_raises_telegram_error(self):
        client, calls = make_client(
            lambda request: httpx.Response(200, json={"ok": False, "description": "blocked"})
        )
        with pytest.raises(TelegramError):
            await client.send_recovery_message("12345", "hi")
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_wraps_in_telegram_error(self):
        client, calls = make_client(
            lambda request: httpx.Response(503, json={"ok": False})
        )
        with pytest.raises(TelegramError):
            await client.send_recovery_message("12345", "hi")
        assert len(calls) == 3  # max_retries

    @pytest.mark.asyncio
    async def test_lazy_http_client_is_created_and_closed(self):
        client = TelegramClient(settings=telegram_settings())
        assert client._http is None
        assert client._get_http() is not None   # lazily constructed
        await client.aclose()                    # owned => closed

    @pytest.mark.asyncio
    async def test_aclose_leaves_injected_client_open(self):
        injected = httpx.AsyncClient()
        client, _ = make_client(
            lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        )
        client2 = TelegramClient(settings=telegram_settings(), http_client=injected)
        await client2.aclose()                   # no-op for injected clients
        assert not injected.is_closed
        await injected.aclose()
        del client
