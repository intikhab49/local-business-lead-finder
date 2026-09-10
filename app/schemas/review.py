from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReviewActionRequest(BaseModel):
    reviewer: str = Field(
        default="",
        max_length=100,
        description="Name of the human reviewer",
    )
    reason: str = Field(
        default="",
        max_length=500,
        description="Optional reason for the review decision",
    )


class ReviewEditRequest(BaseModel):
    reviewer: str = Field(
        default="",
        max_length=100,
        description="Name of the human reviewer",
    )
    updated_fields: dict[str, Any] = Field(
        default_factory=dict,
        description="Lead fields to update as part of the review edit",
    )


class ReviewEntryResponse(BaseModel):
    status: str = Field(description="Review status at the time of the entry")
    reviewer: str = Field(default="", description="Reviewer who acted")
    timestamp: float = Field(default=0.0, description="Epoch timestamp of the decision")
    reason: str = Field(default="", description="Reason for the decision")
    updated_fields: dict[str, Any] = Field(
        default_factory=dict,
        description="Fields changed by an edit decision",
    )


class ReviewResponseSchema(BaseModel):
    fsq_place_id: str = Field(description="Foursquare place ID under review")
    status: str = Field(description="Current review status")
    reviewer: str = Field(default="", description="Reviewer of the latest decision")
    timestamp: float = Field(default=0.0, description="Epoch timestamp of the latest decision")
    reason: str = Field(default="", description="Reason of the latest decision")
    updated_fields: dict[str, Any] = Field(
        default_factory=dict,
        description="Fields changed by the latest edit decision",
    )
    history: list[ReviewEntryResponse] = Field(
        default_factory=list,
        description="Ordered history of review decisions",
    )
    lead: dict[str, Any] = Field(
        default_factory=dict,
        description="Snapshot of the enriched lead under review",
    )


class ReviewListResponse(BaseModel):
    reviews: list[ReviewResponseSchema] = Field(
        default_factory=list,
        description="Reviews currently pending human attention",
    )
    total: int = Field(default=0, description="Number of pending reviews")
