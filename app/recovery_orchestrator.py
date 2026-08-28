"""End-to-end recovery workflow orchestration.

Pipeline for each failed payment:

1. Guard: respect the per-payment attempt cap (anti-spam).
2. Create a fresh Razorpay payment link.
3. Select the outbound channel (Telegram-only today; ``select_channel`` is the
   extension point for future multi-channel routing).
4. Generate a message that adapts to the diagnosed failure reason
   (:data:`REASON_INTERVENTIONS`) and the escalation stage
   (:data:`ESCALATION_TONES`), embedding the fresh link.
5. Deliver via Telegram (``FALLBACK_CHAINS`` keeps the graceful-degradation
   structure ready for additional channels later).
6. Persist every step to the audit trail and update recovery status.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.audit_logger import AuditLogger
from app.channel_clients.telegram_client import TelegramClient
from app.models import FailedPayment
from app.payment_link_creator import PaymentLinkCreator, PaymentLinkError

logger = logging.getLogger(__name__)

#: Preferred channel -> fallback chain.  Telegram is the only live channel
#: today; when new channels land, register their degradation chains here.
FALLBACK_CHAINS: dict[str, tuple[str, ...]] = {
    "telegram": (),
}

DEFAULT_CHANNEL = "telegram"

#: Per-reason intervention copy. The agent's diagnosis decides WHAT remedy
#: the customer is offered — this is the "determines the right intervention"
#: behaviour, keyed by :data:`~app.failure_classifier.VALID_REASONS`.
REASON_INTERVENTIONS: dict[str, str] = {
    "insufficient_funds": (
        "looks like there wasn't enough balance at the time - if that's "
        "sorted now, you can complete your purchase right away"
    ),
    "bank_timeout": (
        "your bank's server timed out, so no money left your account - "
        "it's safe to complete the payment here"
    ),
    "invalid_vpa": (
        "the UPI ID entered couldn't be verified - please double-check your "
        "UPI ID or pay through this secure link instead"
    ),
    "card_declined": (
        "your card was declined by the issuing bank - you can retry with "
        "another card or a different method on this link"
    ),
    "otp_timeout": (
        "the verification OTP expired before confirmation - this fresh link "
        "starts a clean session"
    ),
    "other": ("we hit a snag while processing - here's a secure link to "
              "complete your purchase"),
}
DEFAULT_INTERVENTION = REASON_INTERVENTIONS["other"]

#: Compliant escalation ladder: tone shifts with each unanswered attempt.
#: Stage 1 is a gentle nudge; stage 2 adds urgency; stage 3 is a final notice
#: promising to stop — after which the attempt cap marks the payment
#: unrecoverable and outreach ceases.
ESCALATION_TONES: tuple[str, ...] = (
    "",
    "Quick heads-up: this link expires in {expiry_hours}h.",
    "Final reminder - we'll stop reaching out about this payment after this.",
)


def escalation_stage(attempt_number: int) -> int:
    """Map an attempt number (1-based) onto its escalation stage."""
    return max(1, min(attempt_number, len(ESCALATION_TONES)))


def normalize_phone(contact: str | None) -> str | None:
    """Normalise a phone number to E.164-style ``+<digits>`` form."""
    if not contact:
        return None
    digits = re.sub(r"\D", "", contact)
    if not digits:
        return None
    return f"+{digits}"


def resolve_recipient(contact: str | None) -> str | None:
    """Pick the primary address to hand to the outbound channel.

    Digit-only contacts are treated as Telegram ``chat_id``s and passed
    through untouched (E.164-mangling would break delivery).  Anything else
    (phone numbers from webhooks) is normalised to E.164.
    """
    candidates = resolve_recipient_candidates(contact)
    return candidates[0] if candidates else None


def resolve_recipient_candidates(contact: str | None) -> list[str]:
    """Return every plausible recipient address, best first.

    Razorpay normalises link phone numbers to E.164 (``+91XXXXXXXXXX``), so a
    chat_id typed into the contact field arrives prefixed.  Until a real
    phone->chat_id mapping exists, we try the E.164 form first and then the
    bare national digits (the original chat_id) — Telegram rejects unknown
    addresses immediately, so probing costs one fast failed call at most.
    """
    if not contact:
        return []
    digits = re.sub(r"\D", "", contact)
    if not digits:
        return []
    stripped = contact.strip()
    candidates: list[str] = []
    if stripped.isdigit():
        candidates.append(stripped)
    else:
        e164 = normalize_phone(stripped)
        if e164:
            candidates.append(e164)
        # Country-code prefixes worth stripping for the bare-digits fallback.
        for cc in ("91", "1", "44", "971"):  # India, US, UK, UAE
            if digits.startswith(cc) and len(digits) > len(cc):
                candidates.append(digits[len(cc):])
                break
        else:
            candidates.append(digits)
    seen: set[str] = set()
    ordered: list[str] = []
    for cand in candidates:
        if cand not in seen:
            seen.add(cand)
            ordered.append(cand)
    return ordered


class RecoveryOrchestrator:
    """Executes the full recovery workflow for a failed payment."""

    def __init__(
        self,
        db: Session,
        settings: Any | None = None,
        payment_link_creator: PaymentLinkCreator | None = None,
        telegram_client: TelegramClient | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        """All collaborators are injectable; production defaults are built lazily."""
        self.db = db
        self.settings = settings
        self.payment_link_creator = payment_link_creator or PaymentLinkCreator(settings=settings)
        self._telegram = telegram_client
        self.audit_logger = audit_logger or AuditLogger(db=db)

    # ------------------------------------------------------------------ #
    # Public workflow                                                    #
    # ------------------------------------------------------------------ #
    async def execute_recovery(self, failed_payment: FailedPayment) -> str | None:
        """Run the recovery workflow; returns the channel used, if delivered."""
        attempt_number = self.audit_logger.count_attempts(failed_payment.id) + 1
        max_attempts = int(self._value("max_recovery_attempts_per_payment", 3))
        if attempt_number > max_attempts:
            logger.warning(
                "recovery_attempt_cap_reached",
                extra={"payment_id": failed_payment.payment_id},
            )
            failed_payment.recovery_status = FailedPayment.STATUS_UNRECOVERABLE
            self.db.commit()
            return None

        # Step 1: fresh payment link.
        try:
            link_url = await self.payment_link_creator.create_payment_link(failed_payment)
        except PaymentLinkError as exc:
            logger.error(
                "recovery_aborted_no_link",
                extra={"payment_id": failed_payment.payment_id, "error": str(exc)},
            )
            return None  # stays `pending`; a later event/sweeper can retry.
        link_id = getattr(self.payment_link_creator, "last_link_id", None)

        # Step 2-3: channel selection + reason-aware, escalation-aware message.
        channel = self.select_channel(failed_payment)
        message = self.build_message(
            failed_payment, link_url, attempt_number, settings=self.settings
        )

        # Step 4: deliver (with fallback chain for future channels).
        used_channel, delivered = await self._send_with_fallback(
            channel,
            resolve_recipient_candidates(failed_payment.customer_contact),
            message,
        )

        # Step 5: audit trail + status update.
        outcome = "delivered" if delivered else "failed"
        await self.audit_logger.log_attempt(
            failed_payment_id=failed_payment.id,
            channel=used_channel,
            action="message_sent",
            message=message,
            outcome=outcome,
            payment_link_id=link_id,
        )

        if delivered:
            failed_payment.recovery_status = FailedPayment.STATUS_IN_PROGRESS
            logger.info(
                "recovery_dispatched",
                extra={
                    "payment_id": failed_payment.payment_id,
                    "channel": used_channel,
                    "link_id": link_id,
                },
            )
        else:
            logger.error(
                "recovery_all_channels_failed",
                extra={"payment_id": failed_payment.payment_id},
            )
        self.db.commit()
        return used_channel if delivered else None

    # ------------------------------------------------------------------ #
    # Channel selection & messaging                                      #
    # ------------------------------------------------------------------ #
    @staticmethod
    def select_channel(failed_payment: FailedPayment) -> str:
        """Choose the outbound channel for a failed payment.

        Single-channel deployment today; this method is the seam where
        amount/reason-based multi-channel routing returns later.
        """
        del failed_payment
        return DEFAULT_CHANNEL

    @staticmethod
    def generate_message(failed_payment: FailedPayment, payment_link: str) -> str:
        """Personalised recovery message embedding the fresh payment link.

        Kept for compatibility/tests; production sends go through
        :meth:`build_message` which layers on reason + escalation context.
        """
        return RecoveryOrchestrator.build_message(
            failed_payment, payment_link, attempt_number=1
        )

    @staticmethod
    def build_message(
        failed_payment: FailedPayment,
        payment_link: str,
        attempt_number: int = 1,
        settings: Any | None = None,
    ) -> str:
        """Compose the outreach message for this attempt.

        Layers three pieces of intelligence:
        1. Who + what (name, amount, order).
        2. WHY it failed → the matching intervention from
           :data:`REASON_INTERVENTIONS`.
        3. HOW many times we've asked → tone from :data:`ESCALATION_TONES`.
        """
        amount_inr = (failed_payment.amount or 0) / 100
        name = failed_payment.customer_name or "there"
        reason_line = RecoveryOrchestrator.reason_intervention(
            failed_payment.failure_reason
        )

        stage = escalation_stage(attempt_number)
        tone_template = ESCALATION_TONES[stage - 1]
        urgency = ""
        if tone_template:
            expiry_hours = getattr(settings, "payment_link_expiry_hours", 24) if settings else 24
            urgency = " " + tone_template.format(expiry_hours=expiry_hours)

        return (
            f"Hi {name}, your payment of \u20b9{amount_inr:g} "
            f"for order {failed_payment.payment_id} didn't go through. "
            f"{reason_line.capitalize()}.{urgency} "
            f"Here's your secure link to complete the purchase: {payment_link}"
        )

    @staticmethod
    def reason_intervention(failure_reason: str | None) -> str:
        """Intervention copy for a diagnosed failure reason."""
        if not failure_reason or failure_reason not in REASON_INTERVENTIONS:
            return DEFAULT_INTERVENTION
        return REASON_INTERVENTIONS[failure_reason]

    # ------------------------------------------------------------------ #
    # Delivery with fallback                                             #
    # ------------------------------------------------------------------ #
    async def _send_with_fallback(
        self,
        preferred: str,
        contacts: Sequence[str],
        message: str,
    ) -> tuple[str, bool]:
        """Try the preferred channel across every candidate recipient.

        ``contacts`` is ordered best-first by
        :func:`resolve_recipient_candidates`; Telegram rejects unknown
        addresses immediately, so probing costs one fast failed call each.
        Returns ``(channel_used, delivered)``.  When every channel is
        exhausted the last attempted channel is returned with ``False``.
        """
        if not contacts:
            logger.warning("delivery_skipped_missing_contact")
            return preferred, False

        chain: tuple[str, ...] = (preferred,) + FALLBACK_CHAINS.get(preferred, ())
        last_channel = chain[-1]
        for channel in chain:
            if channel != DEFAULT_CHANNEL:
                # pragma: no cover - defensive until more channels exist
                logger.error("unknown_channel", extra={"channel": channel})
                continue
            for recipient in contacts:
                try:
                    await self._get_telegram().send_recovery_message(
                        recipient, message
                    )
                    return channel, True
                except Exception as exc:  # noqa: BLE001 - degrade to next address
                    logger.warning(
                        "channel_send_failed",
                        extra={
                            "channel": channel,
                            "recipient": recipient,
                            "error": str(exc),
                        },
                    )
        return last_channel, False

    # ------------------------------------------------------------------ #
    # Lazy default collaborators                                         #
    # ------------------------------------------------------------------ #
    def _get_telegram(self) -> TelegramClient:
        if self._telegram is None:
            self._telegram = TelegramClient(settings=self.settings)
        return self._telegram

    def _value(self, key: str, default: Any) -> Any:
        return getattr(self.settings, key, default) if self.settings else default
