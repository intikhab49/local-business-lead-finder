from __future__ import annotations

import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class Lead(Base):
    """Persistent lead record — upserted at search time with review_status='discovered'."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fsq_place_id: Mapped[str] = mapped_column(
        String(200), unique=True, index=True, nullable=False, default=""
    )
    business_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    address: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    categories: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[str | None] = mapped_column(String(200), nullable=True)
    country: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    email: Mapped[str | None] = mapped_column(String(500), nullable=True)
    linkedin: Mapped[str | None] = mapped_column(String(500), nullable=True)
    facebook: Mapped[str | None] = mapped_column(String(500), nullable=True)
    instagram: Mapped[str | None] = mapped_column(String(500), nullable=True)
    twitter: Mapped[str | None] = mapped_column(String(500), nullable=True)
    youtube: Mapped[str | None] = mapped_column(String(500), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    review_status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="discovered", index=True
    )
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reviewer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class DiscoveryExclusion(Base):
    """Place IDs explicitly excluded from future searches (rejected / not interested)."""

    __tablename__ = "discovery_exclusions"

    fsq_place_id: Mapped[str] = mapped_column(
        String(200), primary_key=True, index=True
    )
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )


class RunHistory(Base):
    """Persistent record of a single search / discover / save execution."""

    __tablename__ = "run_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="", index=True
    )
    location: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    business_category: Mapped[str] = mapped_column(
        String(200), nullable=False, default=""
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="", index=True
    )
    total_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_results: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processing_time: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Resume state for grid discovery: which cells finished, and the plan they
    # belong to. Written after every cell so an interrupted run can continue.
    checkpoint: Mapped[dict | None] = mapped_column(JSON, nullable=True)