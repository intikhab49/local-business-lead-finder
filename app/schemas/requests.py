from __future__ import annotations

from pydantic import BaseModel, Field


class BusinessSearchRequest(BaseModel):
    location: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Location to search (e.g., 'New York, NY' or 'lat,lng')",
        examples=["New York, NY"],
    )
    business_category: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Business category/type (e.g., 'restaurant', 'gym')",
        examples=["restaurant"],
    )
    radius: int = Field(
        default=5000,
        ge=100,
        le=50000,
        description="Search radius in meters (100-50000)",
    )
    keyword: str | None = Field(
        default=None,
        max_length=100,
        description="Additional keyword filter",
    )
    min_rating: float | None = Field(
        default=None,
        ge=0.0,
        le=5.0,
        description="Minimum rating filter (0-5)",
    )
    max_results: int | None = Field(
        default=None,
        ge=1,
        le=5000,
        description="Maximum results to return (1-5000, default 500)",
    )
    exclude_saved: bool = Field(
        default=True,
        description="Exclude already-saved leads from results",
    )
    search_mode: str = Field(
        default="normal",
        pattern="^(normal|staggered)$",
        description="Search mode: 'normal' or 'staggered' (grid crawl). "
                    "Prefer POST /discovery/start for grid crawls — it reports "
                    "progress, saves as it goes, and can resume.",
    )
    grid_size: int = Field(
        default=3,
        ge=2,
        le=5,
        description="Grid dimensions for staggered mode (2-5). 3 means 3x3=9 cells.",
    )
    pause_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Deprecated and ignored — pacing is now adaptive "
                    "(it speeds up when the provider is happy and backs off "
                    "when it returns 429/5xx).",
    )


class BusinessSearchQueryRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Text search query",
        examples=["best pizza near Times Square"],
    )
    location: str | None = Field(
        default=None,
        max_length=200,
        description="Location bias for search",
    )
    radius: int = Field(default=5000, ge=100, le=50000)
    business_category: str | None = Field(default=None, max_length=100)
    min_rating: float | None = Field(default=None, ge=0.0, le=5.0)
    max_results: int | None = Field(default=None, ge=1, le=1000)
    exclude_saved: bool = Field(default=True)


class AgentSearchRequest(BaseModel):
    location: str = Field(..., min_length=1, max_length=200)
    business_category: str = Field(..., min_length=1, max_length=100)
    radius: int = Field(default=5000, ge=100, le=50000)
    max_results: int = Field(default=50, ge=1, le=500)


class BusinessResponse(BaseModel):
    fsq_place_id: str = Field(description="Place ID")
    name: str = Field(description="Business name")
    formatted_address: str = Field(description="Full formatted address")
    phone_number: str | None = Field(default=None, description="Phone number")
    website: str | None = Field(default=None, description="Website URL")
    email: str | None = Field(default=None, description="Discovered email address")
    linkedin: str | None = Field(default=None, description="LinkedIn URL")
    facebook: str | None = Field(default=None, description="Facebook URL")
    instagram: str | None = Field(default=None, description="Instagram URL")
    twitter: str | None = Field(default=None, description="X / Twitter URL")
    youtube: str | None = Field(default=None, description="YouTube URL")
    rating: float | None = Field(default=None, description="Rating")
    categories: list[str] = Field(default_factory=list, description="Business categories")
    latitude: float | None = Field(default=None, description="Latitude")
    longitude: float | None = Field(default=None, description="Longitude")
    opening_hours: list[str] = Field(default_factory=list)
    price_level: int | None = Field(default=None)
    confidence_score: float | None = Field(default=None, description="Data confidence score (0-1)")
    source: str | None = Field(default=None, description="Data source")
    auto_enriched: bool = Field(default=False)

    model_config = {"from_attributes": True}


class SearchResponse(BaseModel):
    businesses: list[BusinessResponse]
    total_results: int
    new_results: int = 0
    skipped: int = 0
    total_from_api: int = 0
    search_location: str
    search_category: str
    search_radius: int
    next_page_token: str | None = None
    message: str | None = None


class BusinessDetailResponse(BaseModel):
    business: BusinessResponse


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    provider: str


class ErrorResponse(BaseModel):
    error: str
    message: str
    status_code: int
    details: dict | None = None


class EnrichmentRequest(BaseModel):
    fsq_place_id: str = Field(..., min_length=1, max_length=200,
                              examples=["5a0e475b018cbb6a2196479e"])


class EnrichedBusinessResponse(BaseModel):
    business_name: str = Field(description="Business name")
    address: str = Field(description="Full address")
    phone: str | None = Field(default=None, description="Phone number")
    website: str | None = Field(default=None, description="Website URL")
    email: str | None = Field(default=None, description="Email address")
    linkedin: str | None = Field(default=None, description="LinkedIn URL")
    facebook: str | None = Field(default=None, description="Facebook URL")
    instagram: str | None = Field(default=None, description="Instagram URL")
    twitter: str | None = Field(default=None, description="X / Twitter URL")
    youtube: str | None = Field(default=None, description="YouTube URL")
    latitude: float | None = Field(default=None, description="Latitude")
    longitude: float | None = Field(default=None, description="Longitude")
    confidence_score: float = Field(default=0.0, description="Data confidence score (0-1)")
    source: str = Field(default="Google Places", description="Data source")


class AgentSearchResponse(BaseModel):
    leads: list[EnrichedBusinessResponse]
    total_results: int
    search_location: str
    search_category: str
    search_radius: int
    processing_time_ms: int

# ── Discovery jobs (non-blocking grid crawl) ───────────────────────

class DiscoveryStartRequest(BaseModel):
    location: str = Field(..., min_length=1, max_length=200,
                          examples=["Chicago, IL", "41.8781,-87.6298"])
    business_category: str = Field(..., min_length=1, max_length=100,
                                   examples=["dentist"])
    radius: int = Field(default=5000, ge=100, le=50000)
    keyword: str | None = Field(default=None, max_length=100)
    min_rating: float | None = Field(default=None, ge=0.0, le=5.0)
    max_results: int = Field(default=500, ge=1, le=5000)
    grid_size: int = Field(
        default=3, ge=1, le=6,
        description="Cells per side. 1 = single search, 3 = 3x3 = 9 cells.",
    )
    exclude_saved: bool = Field(
        default=True, description="Skip places already saved or excluded"
    )
    auto_save: bool = Field(
        default=True,
        description="Write results to the database as each cell finishes. "
                    "Turn off to review first and save explicitly.",
    )
    auto_enrich: bool = Field(
        default=True, description="Scrape websites for e-mail and socials"
    )
    provider: str | None = Field(
        default=None, description="Primary provider override (google_places, osm, ...)"
    )
    fallback_provider: str | None = Field(
        default=None, description="Fallback provider, or 'none' to disable"
    )
    concurrency: int | None = Field(default=None, ge=1, le=16)
    resume_run_id: int | None = Field(
        default=None, description="Resume an interrupted run instead of starting fresh"
    )


class DiscoveryJobResponse(BaseModel):
    job_id: str
    status: str
    message: str = ""
    run_id: int | None = None
    provider: str = ""
    fallback_provider: str | None = None
    cells_total: int = 0
    cells_done: int = 0
    cells_skipped: int = 0
    cells_stopped: int = 0
    phase: str = "starting"
    progress: float = 0.0
    found: int = 0
    saved: int = 0
    enriched: int = 0
    duplicates_skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    cell_log: list[dict] = Field(default_factory=list)
    rate_limit: dict = Field(default_factory=dict)
    elapsed: float = 0.0
    resumed_from: int | None = None
    auto_save: bool = True
    params: dict = Field(default_factory=dict)


class SaveLeadsRequest(BaseModel):
    place_ids: list[str] = Field(
        default_factory=list,
        description="Subset to save. Empty means every lead in the job.",
    )
