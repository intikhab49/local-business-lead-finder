import logging
import os
from collections.abc import Generator

import sqlalchemy
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


_engine = None
_session_local = None
_initialized_engine_id: int | None = None


def _database_url() -> str:
    return os.getenv("DATABASE_URL") or settings.database_url


def get_engine():
    """Return the shared SQLAlchemy engine, creating it lazily."""
    global _engine
    if _engine is None:
        url = _database_url()
        connect_args = {}
        poolclass = None
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
            if url in ("sqlite://", "sqlite:///:memory:"):
                poolclass = StaticPool
        _engine = create_engine(url, connect_args=connect_args, poolclass=poolclass)
        logger.info("[DB] Engine created for %s", url)
    return _engine


def get_session_local() -> sessionmaker:
    """Return a cached session factory bound to the shared engine."""
    global _session_local
    if _session_local is None:
        _session_local = sessionmaker(
            bind=get_engine(), autocommit=False, autoflush=False
        )
    return _session_local


def init_db(engine=None) -> None:
    """Create missing tables once per engine."""
    global _initialized_engine_id
    target = engine or get_engine()
    if _initialized_engine_id is id(target):
        return
    from app.db import models  # noqa: F401  register ORM models

    Base.metadata.create_all(bind=target)
    _ensure_lead_columns(target)
    _ensure_columns(target, "run_history", {"checkpoint": "JSON"})
    _initialized_engine_id = id(target)
    logger.info("[DB] Tables ensured on %s", _database_url())


def _ensure_lead_columns(engine) -> None:
    """Idempotently add missing Lead columns to an existing database."""
    missing = {
        "categories": "TEXT",
        "city": "VARCHAR(200)",
        "state": "VARCHAR(200)",
        "country": "VARCHAR(200)",
        "run_id": "INTEGER",
    }
    url = _database_url()
    if not url.startswith("sqlite"):
        return
    try:
        inspector = sqlalchemy.inspect(engine)
        existing = {col["name"] for col in inspector.get_columns("leads")}
    except Exception:  # noqa: BLE001
        return
    with engine.begin() as conn:
        for name, ddl_type in missing.items():
            if name not in existing:
                conn.execute(
                    sqlalchemy.text(
                        f"ALTER TABLE leads ADD COLUMN {name} {ddl_type}"
                    )
                )
                logger.info("[DB] Migrated leads table: added column %s", name)


def _ensure_columns(engine, table: str, columns: dict[str, str]) -> None:
    """Idempotently add missing columns to an existing SQLite table."""
    if not _database_url().startswith("sqlite"):
        return
    try:
        inspector = sqlalchemy.inspect(engine)
        existing = {col["name"] for col in inspector.get_columns(table)}
    except Exception:  # noqa: BLE001 — table not created yet
        return
    with engine.begin() as conn:
        for name, ddl_type in columns.items():
            if name not in existing:
                conn.execute(
                    sqlalchemy.text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")
                )
                logger.info("[DB] Migrated %s table: added column %s", table, name)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a database session."""
    init_db()
    session = get_session_local()()
    try:
        yield session
    finally:
        session.close()