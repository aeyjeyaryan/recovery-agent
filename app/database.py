"""SQLAlchemy engine, session factory and FastAPI dependency."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import get_settings

_settings = get_settings()

_engine_kwargs: dict = {}
if _settings.database_url.startswith("sqlite"):
    # SQLite is used with FastAPI's threadpool; relax its single-thread check.
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(_settings.database_url, **_engine_kwargs)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables() -> None:
    """Create any missing tables (idempotent). Called on app startup."""
    # Import for the side effect of registering models on Base.metadata.
    from app import models  # noqa: F401  pylint: disable=unused-import

    Base.metadata.create_all(bind=engine)
