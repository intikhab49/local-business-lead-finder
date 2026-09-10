from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LeadBase(BaseModel):
    fsq_place_id: str = Field(default="", max_length=200)
    business_name: str = Field(default="", max_length=300)
    address: str = Field(default="", max_length=500)
    categories: str | None = None
    city: str | None = Field(default=None, max_length=200)
    state: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=200)
    phone: str | None = None
    website: str | None = None
    email: str | None = None
    linkedin: str | None = None
    facebook: str | None = None
    instagram: str | None = None
    twitter: str | None = None
    youtube: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    source: str = Field(default="", max_length=100)
    review_status: str = Field(default="", max_length=50)
    reviewer: str | None = Field(default=None, max_length=100)
    notes: str | None = None


class LeadCreate(LeadBase):
    extra: dict[str, Any] | None = None


class LeadBatchRequest(BaseModel):
    items: list[LeadCreate] = Field(default_factory=list)
    location: str = Field(default="", max_length=300)
    business_category: str = Field(default="", max_length=200)


class LeadBatchResult(BaseModel):
    created: int
    updated: int
    failed: int
    run_id: int | None = None


class LeadUpdate(BaseModel):
    business_name: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=500)
    categories: str | None = None
    city: str | None = Field(default=None, max_length=200)
    state: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=200)
    phone: str | None = None
    website: str | None = None
    email: str | None = None
    linkedin: str | None = None
    facebook: str | None = None
    instagram: str | None = None
    twitter: str | None = None
    youtube: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    confidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    source: str | None = Field(default=None, max_length=100)
    review_status: str | None = Field(default=None, max_length=50)
    reviewer: str | None = Field(default=None, max_length=100)
    notes: str | None = None
    extra: dict[str, Any] | None = None


class LeadRead(LeadBase):
    id: int
    extra: dict[str, Any] | None = None
    run_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class LeadBatchRead(BaseModel):
    """A saved batch: a Save All run plus its lead count and metadata."""

    run_id: int
    location: str = ""
    business_category: str = ""
    status: str = ""
    total_results: int = 0
    lead_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None


class LeadBatchesResponse(BaseModel):
    items: list[LeadBatchRead]
    total: int
    ungrouped_total: int = 0


class LeadListResponse(BaseModel):
    items: list[LeadRead]
    total: int
    limit: int
    offset: int


class LeadDeleteResult(BaseModel):
    deleted: int


class LeadStatsResponse(BaseModel):
    total: int
    by_status: dict[str, int]
    avg_confidence: float
    with_email: int
    with_website: int
