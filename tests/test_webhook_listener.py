"""Webhook listener tests: signature verification, idempotency, end-to-end flow."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import webhook_listener as wl
from app.database import Base, get_db
from app.models import FailedPayment, RecoveryAttempt

SECRET = "test_webhook_secret"


# --------------------------------------------------------------------- #
# Fixtures & helpers                                                    #
# --------------------------------------------------------------------- #
@pytest.fixture()
def db_session():
    # StaticPool => every session shares one connection, so data written by
    # the background task's own session is visible here.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    yield session
    session.close()


def make_payload(payment_id: str = "pay_DESdhfg54", **overrides: Any) -> dict:
    entity: dict[str, Any] = {
        "id": payment_id,
        "amount": 129900,
        "currency": "INR",
        "status": "failed",
        "method": "upi",
        "error_code": "BAD_REQUEST",
        "error_description": "Insufficient funds in the account",
        "customer": {
            "name": "Rahul Sharma",
            "contact": "+919876543210",
            "email": "rahul@example.com",
        },
    }
    entity.update(overrides)
    # Real Razorpay envelope: entities live under payload.<type>.entity.
    return {
        "entity": "event",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {"payment": {"entity": entity}},
    }


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@pytest.fixture()
def client(monkeypatch, db_session):
    """TestClient wired to an in-memory DB with a known webhook secret."""
    monkeypatch.setattr(wl, "_webhook_secret", lambda: SECRET)

    async def noop_recovery(payment_id: str) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(wl, "process_recovery", noop_recovery)

    app = FastAPI()
    app.include_router(wl.router)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------- #
# Signature verification (pure function)                                #
# --------------------------------------------------------------------- #
class TestVerifySignature:
    def test_valid_signature(self):
        body = b'{"event": "payment.failed"}'
        assert wl.verify_webhook_signature(body, sign(body), SECRET) is True

    def test_invalid_signature(self):
        assert (
            wl.verify_webhook_signature(b"body", "deadbeef" * 8, SECRET) is False
        )


# --------------------------------------------------------------------- #
# Envelope parsing (real Razorpay vs legacy flat shape)                 #
# --------------------------------------------------------------------- #
class TestExtractPaymentEntity:
    def test_real_razorpay_nested_envelope(self):
        entity = {"id": "pay_NESTED_1", "amount": 1000}
        payload = {
            "event": "payment.failed",
            "contains": ["payment"],
            "payload": {"payment": {"entity": entity}},
        }
        assert wl.extract_payment_entity(payload) == entity

    def test_legacy_flat_envelope_still_accepted(self):
        entity = {"id": "pay_FLAT_1"}
        payload = {"event": "payment.failed", "payment": {"entity": entity}}
        assert wl.extract_payment_entity(payload) == entity

    def test_wrong_event_rejected(self):
        assert (
            wl.extract_payment_entity({"event": "payment.captured"}) is None
        )

    def test_missing_entity_id_rejected(self):
        payload = {
            "event": "payment.failed",
            "payload": {"payment": {"entity": {"amount": 100}}},
        }
        assert wl.extract_payment_entity(payload) is None

    def test_missing_payment_section_rejected(self):
        assert wl.extract_payment_entity({"event": "payment.failed"}) is None

    def test_tampered_body_fails(self):
        body = b'{"amount": 100}'
        tampered = b'{"amount": 999999}'
        assert wl.verify_webhook_signature(tampered, sign(body), SECRET) is False

    def test_missing_signature_fails(self):
        assert wl.verify_webhook_signature(b"body", "", SECRET) is False

    def test_missing_secret_fails(self):
        body = b"body"
        assert wl.verify_webhook_signature(body, sign(body), "") is False

    def test_uses_hmac_sha256_hexdigest(self):
        body = b"payload"
        expected = hashlib.sha256(SECRET.encode("utf-8") + body).hexdigest()
        # Sanity check the fixture itself produces HMAC, not plain hash.
        assert sign(body) != expected
        assert wl.verify_webhook_signature(body, sign(body), SECRET)


# --------------------------------------------------------------------- #
# Endpoint behaviour                                                    #
# --------------------------------------------------------------------- #
class TestWebhookEndpoint:
    def test_valid_event_creates_record_and_returns_200(self, client, db_session):
        payload = make_payload()
        response = client.post(
            "/webhook",
            content=json.dumps(payload).encode(),
            headers={"X-Razorpay-Signature": sign(json.dumps(payload).encode())},
        )
        assert response.status_code == 200
        row = db_session.query(FailedPayment).one()
        assert row.payment_id == "pay_DESdhfg54"
        assert row.amount == 129900
        assert row.customer_name == "Rahul Sharma"
        assert row.recovery_status == FailedPayment.STATUS_PENDING

    def test_real_razorpay_entity_top_level_contact_captured(
        self, client, db_session
    ):
        """Real Razorpay payments carry contact/email at entity top level."""
        payload = make_payload(
            customer=None,
            contact="+919319840512",
            email="void@razorpay.com",
        )
        response = client.post(
            "/webhook",
            content=json.dumps(payload).encode(),
            headers={"X-Razorpay-Signature": sign(json.dumps(payload).encode())},
        )
        assert response.status_code == 200
        row = db_session.query(FailedPayment).one()
        assert row.customer_contact == "+919319840512"
        assert row.customer_email == "void@razorpay.com"

    def test_invalid_signature_returns_401(self, client, db_session):
        response = client.post(
            "/webhook",
            content=json.dumps(make_payload()).encode(),
            headers={"X-Razorpay-Signature": "0" * 64},
        )
        assert response.status_code == 401
        assert db_session.query(FailedPayment).count() == 0

    def test_missing_signature_returns_401(self, client):
        response = client.post("/webhook", content=b"{}")
        assert response.status_code == 401

    def test_malformed_json_with_valid_signature_returns_400(self, client):
        body = b"{not json"
        response = client.post(
            "/webhook", content=body, headers={"X-Razorpay-Signature": sign(body)}
        )
        assert response.status_code == 400

    def test_non_failed_events_acknowledged_but_not_stored(self, client, db_session):
        payload = make_payload()
        payload["event"] = "payment.captured"
        body = json.dumps(payload).encode()
        response = client.post(
            "/webhook", content=body, headers={"X-Razorpay-Signature": sign(body)}
        )
        assert response.status_code == 200
        assert db_session.query(FailedPayment).count() == 0

    def test_duplicate_payment_is_idempotent(self, client, db_session):
        body = json.dumps(make_payload()).encode()
        headers = {"X-Razorpay-Signature": sign(body)}
        first = client.post("/webhook", content=body, headers=headers)
        second = client.post("/webhook", content=body, headers=headers)
        assert first.status_code == 200
        assert second.status_code == 200
        assert db_session.query(FailedPayment).count() == 1

    def test_payment_entity_without_id_rejected(self, client, db_session):
        payload = make_payload(payment_id=None)
        body = json.dumps(payload).encode()
        response = client.post(
            "/webhook", content=body, headers={"X-Razorpay-Signature": sign(body)}
        )
        assert response.status_code == 200  # acked, but nothing stored
        assert db_session.query(FailedPayment).count() == 0


# --------------------------------------------------------------------- #
# Envelope parsing: payment_link.paid                                   #
# --------------------------------------------------------------------- #
def make_paid_payload(
    link_id: str = "plink_PAID_1",
    original_payment_id: str = "pay_DESdhfg54",
    amount: int = 129900,
    **overrides: Any,
) -> dict:
    entity: dict[str, Any] = {
        "id": link_id,
        "status": "paid",
        "amount": amount,
        "currency": "INR",
        "short_url": "https://rzp.io/i/paidlink",
        "notes": {
            "recovery_campaign": "true",
            "original_payment_id": original_payment_id,
        },
    }
    entity.update(overrides)
    return {
        "entity": "event",
        "event": "payment_link.paid",
        "contains": ["payment_link", "payment"],
        "payload": {
            "payment_link": {"entity": entity},
            "payment": {"entity": {"id": "pay_captured", "amount": amount}},
        },
    }


class TestExtractPaidLinkEntity:
    def test_real_nested_envelope(self):
        payload = make_paid_payload()
        entity = wl.extract_paid_link_entity(payload)
        assert entity is not None
        assert entity["id"] == "plink_PAID_1"
        assert entity["notes"]["original_payment_id"] == "pay_DESdhfg54"

    def test_legacy_flat_envelope_still_accepted(self):
        payload = {
            "event": "payment_link.paid",
            "payment_link": {"entity": {"id": "plink_FLAT", "notes": {}}},
        }
        assert wl.extract_paid_link_entity(payload)["id"] == "plink_FLAT"

    def test_wrong_event_rejected(self):
        assert (
            wl.extract_paid_link_entity({"event": "payment.failed"}) is None
        )

    def test_missing_entity_id_rejected(self):
        payload = make_paid_payload(link_id=None)
        assert wl.extract_paid_link_entity(payload) is None

    def test_missing_section_rejected(self):
        assert wl.extract_paid_link_entity({"event": "payment_link.paid"}) is None


# --------------------------------------------------------------------- #
# Endpoint behaviour: closing the recovered-money loop                  #
# --------------------------------------------------------------------- #
def post_signed(client, payload: dict) -> httpx.Response:
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook", content=body, headers={"X-Razorpay-Signature": sign(body)}
    )


class TestPaymentLinkPaidEndpoint:
    @pytest.fixture()
    def recovered_seed(self, db_session):
        """A previously-attempted failed payment awaiting customer payment."""
        payment = FailedPayment(
            payment_id="pay_DESdhfg54",
            amount=129900,
            currency="INR",
            customer_name="Rahul Sharma",
            recovery_status=FailedPayment.STATUS_IN_PROGRESS,
        )
        db_session.add(payment)
        db_session.commit()
        return payment

    def test_valid_paid_event_marks_recovered_and_records_money(
        self, client, db_session, recovered_seed
    ):
        response = post_signed(client, make_paid_payload())
        assert response.status_code == 200

        db_session.refresh(recovered_seed)
        assert recovered_seed.recovery_status == FailedPayment.STATUS_RECOVERED

        attempt = db_session.query(RecoveryAttempt).one()
        assert attempt.failed_payment_id == recovered_seed.id
        assert attempt.outcome == "paid"
        assert attempt.action == "payment_completed"
        assert attempt.payment_link_id == "plink_PAID_1"
        assert attempt.recovery_amount == 129900

    def test_unknown_original_payment_acknowledged_without_side_effects(
        self, client, db_session
    ):
        payload = make_paid_payload(original_payment_id="pay_MISSING")
        response = post_signed(client, payload)
        assert response.status_code == 200
        assert db_session.query(FailedPayment).count() == 0
        assert db_session.query(RecoveryAttempt).count() == 0

    def test_missing_notes_original_id_ignored(self, client, db_session, recovered_seed):
        payload = make_paid_payload(notes={"recovery_campaign": "true"})
        response = post_signed(client, payload)
        assert response.status_code == 200
        db_session.refresh(recovered_seed)
        assert recovered_seed.recovery_status != FailedPayment.STATUS_RECOVERED
        assert db_session.query(RecoveryAttempt).count() == 0

    def test_duplicate_paid_event_is_idempotent(
        self, client, db_session, recovered_seed
    ):
        payload = make_paid_payload()
        assert post_signed(client, payload).status_code == 200
        assert post_signed(client, payload).status_code == 200
        assert db_session.query(RecoveryAttempt).filter_by(outcome="paid").count() == 1

    def test_unrecoverable_payment_that_pays_still_flips_to_recovered(
        self, client, db_session, recovered_seed
    ):
        recovered_seed.recovery_status = FailedPayment.STATUS_UNRECOVERABLE
        db_session.commit()
        response = post_signed(client, make_paid_payload())
        assert response.status_code == 200
        db_session.refresh(recovered_seed)
        assert recovered_seed.recovery_status == FailedPayment.STATUS_RECOVERED

    def test_invalid_signature_on_paid_event_returns_401(
        self, client, db_session, recovered_seed
    ):
        body = json.dumps(make_paid_payload()).encode()
        response = client.post(
            "/webhook", content=body, headers={"X-Razorpay-Signature": "f" * 64}
        )
        assert response.status_code == 401
        db_session.refresh(recovered_seed)
        assert recovered_seed.recovery_status != FailedPayment.STATUS_RECOVERED

    def test_unusable_paid_payload_acknowledged(self, client, db_session, recovered_seed):
        payload = make_paid_payload(link_id=None)
        response = post_signed(client, payload)
        assert response.status_code == 200
        assert db_session.query(RecoveryAttempt).count() == 0


# --------------------------------------------------------------------- #
# Database helpers                                                      #
# --------------------------------------------------------------------- #
class TestDatabaseHelpers:
    def test_create_tables_and_get_db(self, tmp_path, monkeypatch):
        from sqlalchemy import inspect

        from app import database as app_db

        db_path = tmp_path / "helper_test.db"
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        monkeypatch.setattr(app_db, "engine", engine)

        assert not inspect(engine).has_table("failed_payments")
        app_db.create_tables()
        assert inspect(engine).has_table("failed_payments")
        # Idempotent.
        app_db.create_tables()

        sessions = list(app_db.get_db())
        assert len(sessions) == 1  # generator yields one session then closes


# --------------------------------------------------------------------- #
# End-to-end: webhook -> diagnosis -> recovery -> audit                 #
# --------------------------------------------------------------------- #
class FakeLinkCreator:
    def __init__(self) -> None:
        self.last_link_id = "link_test_1"

    async def create_payment_link(self, failed_payment) -> str:  # noqa: ANN001
        return "https://rzp.io/i/testlink"


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_recovery_message(self, contact: str, message: str) -> dict:
        self.sent.append((contact, message))
        return {"ok": True, "result": {"message_id": 1, "status": "sent"}}


class TestEndToEndRecoveryFlow:
    @pytest.fixture()
    def e2e_client(self, monkeypatch, db_session):
        """Full pipeline: only external providers (Groq/Razorpay/Telegram) faked."""
        monkeypatch.setattr(wl, "_webhook_secret", lambda: SECRET)
        # Redirect process_recovery's session to our in-memory DB.
        monkeypatch.setattr(
            "app.database.SessionLocal",
            sessionmaker(bind=db_session.bind, expire_on_commit=False),
        )

        async def fake_classify(settings, payment_data):  # noqa: ANN001
            return "insufficient_funds"

        monkeypatch.setattr(wl, "_classify_failure", fake_classify)

        telegram = FakeTelegram()

        def build_orchestrator(db, settings):  # noqa: ANN001
            from app.audit_logger import AuditLogger
            from app.recovery_orchestrator import RecoveryOrchestrator

            # Real orchestrator + audit logger; only network edges faked.
            return RecoveryOrchestrator(
                db=db,
                settings=settings,
                payment_link_creator=FakeLinkCreator(),
                telegram_client=telegram,
                audit_logger=AuditLogger(db=db),
            )

        monkeypatch.setattr(wl, "_build_orchestrator", build_orchestrator)

        app = FastAPI()
        app.include_router(wl.router)
        app.dependency_overrides[get_db] = lambda: db_session
        with TestClient(app) as test_client:
            yield test_client

    def test_full_flow(self, e2e_client, db_session):
        payload = make_payload()
        body = json.dumps(payload).encode()
        response = e2e_client.post(
            "/webhook", content=body, headers={"X-Razorpay-Signature": sign(body)}
        )
        assert response.status_code == 200

        payment = db_session.query(FailedPayment).one()
        assert payment.failure_reason == "insufficient_funds"
        assert payment.recovery_status == FailedPayment.STATUS_IN_PROGRESS

        attempt = db_session.query(RecoveryAttempt).one()
        assert attempt.failed_payment_id == payment.id
        assert attempt.channel == "telegram"
        assert attempt.action == "message_sent"
        assert attempt.outcome == "delivered"
        assert attempt.payment_link_id == "link_test_1"
        assert "rzp.io" in attempt.message
