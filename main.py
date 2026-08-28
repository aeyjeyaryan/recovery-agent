"""FastAPI application entrypoint for the Razorpay Recovery Agent."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

import app.models  # noqa: F401  -- imported to register ORM models on Base
from app.database import Base, engine
from app.logging_config import setup_logging
from app.webhook_listener import router as webhook_router
from config import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup/shutdown hook: configure logging, verify config, create tables."""
    settings = get_settings()
    setup_logging(settings.log_level)

    try:
        settings.ensure_startup_ready()
        application.state.config_ok = True
        logger.info("startup_complete", extra={"database_url": settings.database_url})
    except RuntimeError as exc:
        # Degrade gracefully: keep serving (health reports degraded state) so
        # local/dev environments work without live credentials.
        application.state.config_ok = False
        logger.error("startup_config_incomplete", extra={"error": str(exc)})

    Base.metadata.create_all(bind=engine)
    yield
    logger.info("shutdown_complete")


app = FastAPI(
    title="Razorpay Recovery Agent",
    description=(
        "Autonomous agent that diagnoses failed Razorpay payments and "
        "recovers them over Telegram."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(webhook_router)


@app.get("/health")
def health_check() -> dict[str, object]:
    """Liveness/readiness probe."""
    return {
        "status": "healthy",
        "config_ok": getattr(app.state, "config_ok", None),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
