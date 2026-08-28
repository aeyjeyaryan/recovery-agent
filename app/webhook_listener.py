"""FastAPI router receiving Razorpay webhooks.

Two event types are handled:

- ``payment.failed``    → start the recovery workflow (diagnose, link, DM).
- ``payment_link.paid`` → close the loop: the customer paid a recovery link,
  so flip the original payment to ``recovered`` and record the money.

Security & reliability contract:

1. The **raw** request body is read before any parsing so the HMAC-SHA256
   signature check (``X-Razorpay-Signature``) covers exactly the bytes
   Razorpay signed.
2. Invalid or missing signatures are rejected with ``401``.
3. Handling is **idempotent**: a duplicate ``payment_id`` short-circuits with
   ``200`` and does not re-trigger outreach; repeat ``paid`` events on an
   already-recovered payment are acknowledged without side effects.
4. The endpoint acknowledges within Razorpay's latency budget: verification
   plus one DB insert happen inline; LLM diagnosis + recovery execution run as
   a FastAPI background task with their own database session.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.logging_config import set_correlation_id

router = APIRouter()
logger = logging.getLogger(__name__)

WEBHOOK_SECRET_ATTR = "razorpay_webhook_secret"


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Return True iff ``signature`` is the HMAC-SHA256 of ``raw_body``."""
    if not signature or not secret:
        return False
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_signature, signature)


def extract_payment_entity(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the webhook envelope and return the inner payment entity.

    Real Razorpay webhooks wrap entities as
    ``{"event": ..., "payload": {"payment": {"entity": {...}}}}``.
    A flat ``{"payment": {"entity": ...}}`` envelope (used by the local
    simulator and older tests) is tolerated for convenience.

    Returns ``None`` when the payload does not carry a usable payment entity.
    """
    if payload.get("event") != "payment.failed":
        return None
    return _extract_nested_entity(payload, "payment")


def extract_paid_link_entity(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate a ``payment_link.paid`` envelope; return the link entity.

    Real Razorpay nests it as
    ``{"event": "payment_link.paid", "payload": {"payment_link":
    {"entity": {...}}}}`` (with a sibling ``payload.payment.entity`` for the
    captured payment).  The legacy flat shape is tolerated like above.
    """
    if payload.get("event") != "payment_link.paid":
        return None
    return _extract_nested_entity(payload, "payment_link")


def _extract_nested_entity(payload: dict[str, Any], kind: str) -> dict[str, Any] | None:
    container = payload.get("payload")
    nested = (
        container.get(kind)
        if isinstance(container, dict)
        else payload.get(kind)
    )
    entity = nested.get("entity") if isinstance(nested, dict) else None
    if not isinstance(entity, dict):
        return None
    if not entity.get("id"):
        return None
    return entity


def _webhook_secret() -> str:
    # Imported lazily so tests can monkeypatch config cleanly.
    from config import get_settings

    return getattr(get_settings(), WEBHOOK_SECRET_ATTR)


@router.post("/webhook")
async def razorpay_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Response:
    """Receive, verify and enqueue handling of a Razorpay webhook."""
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not verify_webhook_signature(raw_body, signature, _webhook_secret()):
        logger.warning("webhook_rejected_invalid_signature")
        return Response(status_code=401)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("webhook_rejected_malformed_json")
        return Response(status_code=400)

    if payload.get("event") == "payment_link.paid":
        entity = extract_paid_link_entity(payload)
        if entity is None:
            logger.warning(
                "paid_webhook_unusable_payload", extra={"event": payload.get("event")}
            )
            return Response(status_code=200)
        return handle_payment_link_paid(db, entity)

    entity = extract_payment_entity(payload)
    if entity is None:
        # Verified but irrelevant/unusable event: acknowledge to stop retries.
        logger.info("webhook_ignored", extra={"event": payload.get("event")})
        return Response(status_code=200)

    payment_id = entity["id"]
    set_correlation_id(payment_id)

    existing = db.query(models.FailedPayment).filter_by(payment_id=payment_id).first()
    if existing is not None:
        logger.info("webhook_duplicate_ignored", extra={"payment_id": payment_id})
        return Response(status_code=200)

    # Razorpay carries contact/email at the TOP level of the payment entity;
    # a nested `customer` object is tolerated when present.
    customer = entity.get("customer") or {}
    failed_payment = models.FailedPayment(
        payment_id=payment_id,
        amount=entity.get("amount"),
        currency=entity.get("currency"),
        customer_name=customer.get("name") or entity.get("name"),
        customer_contact=customer.get("contact") or entity.get("contact"),
        customer_email=customer.get("email") or entity.get("email"),
        payment_method=entity.get("method"),
        error_code=entity.get("error_code"),
        error_description=entity.get("error_description"),
        failure_reason=None,
        recovery_status=models.FailedPayment.STATUS_PENDING,
    )
    db.add(failed_payment)
    db.commit()

    background_tasks.add_task(process_recovery, payment_id)
    logger.info("webhook_accepted", extra={"payment_id": payment_id})
    return Response(status_code=200)


def handle_payment_link_paid(db: Session, entity: dict[str, Any]) -> Response:
    """Close the recovery loop when a customer pays a recovery link.

    The link's ``notes.original_payment_id`` (written by
    :class:`PaymentLinkCreator`) ties the payment back to the stored failed
    payment.  Idempotent: repeat events on an already-recovered payment are
    acknowledged without side effects.
    """
    link_id = entity.get("id")
    notes = entity.get("notes") or {}
    original_payment_id = (
        notes.get("original_payment_id") or entity.get("original_payment_id")
    )
    if not original_payment_id:
        logger.warning(
            "paid_webhook_missing_original_payment", extra={"link_id": link_id}
        )
        return Response(status_code=200)

    set_correlation_id(str(original_payment_id))
    failed = (
        db.query(models.FailedPayment)
        .filter_by(payment_id=original_payment_id)
        .first()
    )
    if failed is None:
        logger.warning(
            "paid_webhook_unknown_payment",
            extra={"link_id": link_id, "payment_id": original_payment_id},
        )
        return Response(status_code=200)

    if failed.recovery_status == models.FailedPayment.STATUS_RECOVERED:
        logger.info(
            "paid_webhook_duplicate_ignored",
            extra={"payment_id": original_payment_id, "link_id": link_id},
        )
        return Response(status_code=200)

    amount = entity.get("amount") or failed.amount or 0
    short_url = entity.get("short_url") or ""
    failed.recovery_status = models.FailedPayment.STATUS_RECOVERED
    db.add(
        models.RecoveryAttempt(
            failed_payment_id=failed.id,
            channel="razorpay",
            action="payment_completed",
            message=(
                f"Customer paid recovery link {link_id} "
                f"({short_url}) — ₹{amount / 100:.2f} recovered."
            ),
            outcome="paid",
            payment_link_id=link_id,
            recovery_amount=amount,
        )
    )
    db.commit()
    logger.info(
        "payment_recovered",
        extra={
            "payment_id": original_payment_id,
            "link_id": link_id,
            "recovery_amount_paise": amount,
        },
    )
    return Response(status_code=200)


async def _classify_failure(settings: Any, payment_data: dict[str, Any]) -> str:
    """DI seam: classify a failure (tests patch this)."""
    from app.failure_classifier import FailureClassifier

    return await FailureClassifier(settings=settings).classify(payment_data)


def _build_orchestrator(db: Session, settings: Any) -> Any:
    """DI seam: build the recovery orchestrator (tests patch this)."""
    from app.audit_logger import AuditLogger
    from app.payment_link_creator import PaymentLinkCreator
    from app.recovery_orchestrator import RecoveryOrchestrator

    return RecoveryOrchestrator(
        db=db,
        settings=settings,
        payment_link_creator=PaymentLinkCreator(settings=settings),
        audit_logger=AuditLogger(db=db),
    )


async def process_recovery(payment_id: str) -> None:
    """Classify a stored failed payment and run the recovery workflow.

    Runs as a background task with its own DB session so the webhook request
    session is never held open past the 2-second acknowledgement budget.
    """
    from app.database import SessionLocal
    from config import get_settings

    set_correlation_id(payment_id)
    settings = get_settings()
    db = SessionLocal()
    try:
        failed_payment = (
            db.query(models.FailedPayment).filter_by(payment_id=payment_id).first()
        )
        if failed_payment is None:
            logger.error("process_recovery_missing_payment", extra={"payment_id": payment_id})
            return

        failed_payment.failure_reason = await _classify_failure(
            settings,
            {
                "payment_id": failed_payment.payment_id,
                "method": failed_payment.payment_method,
                "error_code": failed_payment.error_code,
                "error_description": failed_payment.error_description,
            },
        )
        db.commit()

        await _build_orchestrator(db, settings).execute_recovery(failed_payment)
    except Exception:  # noqa: BLE001 - background task must never crash silently
        logger.exception("process_recovery_failed", extra={"payment_id": payment_id})
        db.rollback()
    finally:
        db.close()
