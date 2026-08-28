"""Durable audit trail: every recovery action is persisted and logged."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import RecoveryAttempt

logger = logging.getLogger(__name__)


class AuditLogger:
    """Writes :class:`RecoveryAttempt` rows plus structured JSON log events."""

    def __init__(self, db: Session) -> None:
        self._db = db

    async def log_attempt(
        self,
        failed_payment_id: int,
        channel: str,
        action: str,
        message: str,
        outcome: str,
        payment_link_id: Optional[str] = None,
        recovery_amount: Optional[int] = None,
    ) -> RecoveryAttempt:
        """Persist one recovery attempt and emit a structured log line."""
        attempt = RecoveryAttempt(
            failed_payment_id=failed_payment_id,
            channel=channel,
            action=action,
            message=message,
            outcome=outcome,
            payment_link_id=payment_link_id,
            recovery_amount=recovery_amount,
        )
        self._db.add(attempt)
        self._db.commit()
        self._db.refresh(attempt)

        logger.info(
            "audit_attempt",
            extra={
                "attempt_id": attempt.id,
                "failed_payment_id": failed_payment_id,
                "channel": channel,
                "action": action,
                "outcome": outcome,
                "payment_link_id": payment_link_id,
            },
        )
        return attempt

    def count_attempts(self, failed_payment_id: int) -> int:
        """Number of recovery attempts already made against a payment."""
        return (
            self._db.query(RecoveryAttempt)
            .filter(RecoveryAttempt.failed_payment_id == failed_payment_id)
            .count()
        )

    def recent_attempts(self, limit: int = 50) -> list[RecoveryAttempt]:
        """Most recent attempts (dashboard helper)."""
        return (
            self._db.query(RecoveryAttempt)
            .order_by(RecoveryAttempt.timestamp.desc())
            .limit(limit)
            .all()
        )

    def attempts_for(self, failed_payment_id: int) -> list[dict[str, Any]]:
        """Serialised attempt history for one failed payment."""
        rows = (
            self._db.query(RecoveryAttempt)
            .filter(RecoveryAttempt.failed_payment_id == failed_payment_id)
            .order_by(RecoveryAttempt.timestamp.asc())
            .all()
        )
        return [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "channel": r.channel,
                "action": r.action,
                "outcome": r.outcome,
                "payment_link_id": r.payment_link_id,
            }
            for r in rows
        ]
