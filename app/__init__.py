"""Razorpay AI Revenue Recovery Agent application package.

Modules:
    webhook_listener   -- FastAPI router receiving ``payment.failed`` events.
    failure_classifier -- LLM-powered (Groq) failure diagnosis with rules fallback.
    recovery_orchestrator -- End-to-end recovery workflow (link -> channel -> audit).
    payment_link_creator  -- Fresh Razorpay payment link generation.
    channel_clients   -- Telegram delivery client (+ shared rate limiter).
    audit_logger      -- Durable audit trail for every recovery action.
    models / database -- SQLAlchemy persistence layer.
"""
