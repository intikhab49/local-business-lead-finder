from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RunHistoryRead(BaseModel):
    id: int
    run_type: str = Field(default="", max_length=50)
    location: str = Field(default="", max_length=300)
    business_category: str = Field(default="", max_length=200)
    status: str = Field(default="", max_length=50)
    total_tasks: int = 0
    total_results: int = 0
    processing_time: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None

    model_config = ConfigDict(from_attributes=True)


class RunHistoryListResponse(BaseModel):
    items: list[RunHistoryRead]
    total: int
    limit: int
    offset: int


class RunHistoryStatsResponse(BaseModel):
    total_runs: int
    successful: int
    failed: int
    avg_duration: float
    by_status: dict[str, int]
