"""SQLAlchemy ORM models: failed payments and their recovery attempts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class FailedPayment(Base):
    """A Razorpay payment that failed and may be recoverable."""

    __tablename__ = "failed_payments"

    # Recovery lifecycle values for `recovery_status`.
    STATUS_PENDING = "pending"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RECOVERED = "recovered"
    STATUS_UNRECOVERABLE = "unrecoverable"

    id = Column(Integer, primary_key=True)
    payment_id = Column(String, unique=True, index=True)  # Razorpay payment ID
    amount = Column(Integer)  # in paise
    currency = Column(String)
    customer_name = Column(String)
    customer_contact = Column(String)
    customer_email = Column(String)
    payment_method = Column(String)  # "upi", "card", "netbanking", "wallet"
    error_code = Column(String)
    error_description = Column(String)
    failure_reason = Column(String)  # e.g. "insufficient_funds"
    recovery_status = Column(
        String, default=STATUS_PENDING, index=True
    )  # pending | in_progress | recovered | unrecoverable
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    recovery_attempts = relationship(
        "RecoveryAttempt", back_populates="failed_payment"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FailedPayment {self.payment_id} status={self.recovery_status}>"


class RecoveryAttempt(Base):
    """One outbound recovery action taken against a failed payment."""

    __tablename__ = "recovery_attempts"

    id = Column(Integer, primary_key=True)
    failed_payment_id = Column(Integer, ForeignKey("failed_payments.id"))
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    channel = Column(String)  # "telegram"
    action = Column(
        String
    )  # "message_sent" | "call_initiated" | "payment_link_created"
    message = Column(Text)  # Full message body / call script
    outcome = Column(String)  # "delivered" | "clicked" | "paid" | "failed" | "escalated"
    payment_link_id = Column(String)  # Razorpay payment link ID (if created)
    recovery_amount = Column(Integer)  # Amount recovered in paise (if paid)

    failed_payment = relationship("FailedPayment", back_populates="recovery_attempts")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<RecoveryAttempt payment={self.failed_payment_id} "
            f"channel={self.channel} action={self.action} outcome={self.outcome}>"
        )
