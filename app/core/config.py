from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "Business Lead Finder"
    app_version: str = "2.0.0"
    debug: bool = Field(default=True, alias="DEBUG")
    environment: str = Field(default="development", alias="ENVIRONMENT")

    # Server
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    # API
    api_prefix: str = Field(default="/api/v1", alias="API_PREFIX")
    default_radius: int = Field(default=5000, alias="DEFAULT_RADIUS")
    provider_timeout: int = Field(default=30, alias="PROVIDER_TIMEOUT")

    # Google Places API (New v1)
    google_places_api_key: str | None = Field(
        default=None, alias="GOOGLE_PLACES_API_KEY"
    )
    google_places_base_url: str = Field(
        default="https://places.googleapis.com/v1",
        alias="GOOGLE_PLACES_BASE_URL",
    )
    google_places_timeout: int = Field(
        default=30, alias="GOOGLE_PLACES_TIMEOUT"
    )
    # New: much higher ceiling — paginated discovery can pull hundreds
    google_places_max_results: int = Field(
        default=500, alias="GOOGLE_PLACES_MAX_RESULTS"
    )

    # Foursquare API
    foursquare_api_key: str | None = Field(default=None, alias="FOURSQUARE_API_KEY")
    foursquare_base_url: str = Field(
        default="https://places-api.foursquare.com/places",
        alias="FOURSQUARE_BASE_URL",
    )
    foursquare_timeout: int = Field(default=30, alias="FOURSQUARE_TIMEOUT")
    foursquare_max_results: int = Field(default=50, alias="FOURSQUARE_MAX_RESULTS")

    # Geoapify API
    geoapify_api_key: str | None = Field(default=None, alias="GEOAPIFY_API_KEY")
    geoapify_max_results: int = Field(
        default=500, alias="GEOAPIFY_MAX_RESULTS"
    )

    # Provider
    provider_name: str = Field(default="google_places", alias="PROVIDER_NAME")
    # Free/open-data fallback provider used when the primary provider fails,
    # is rate-limited, or returns nothing. Set to "none" to disable.
    fallback_provider_name: str = Field(default="osm", alias="FALLBACK_PROVIDER_NAME")

    # OpenStreetMap / Overpass (free, no API key required)
    osm_overpass_urls: list[str] = Field(
        default=[
            "https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
        ],
        alias="OSM_OVERPASS_URLS",
    )
    osm_nominatim_url: str = Field(
        default="https://nominatim.openstreetmap.org/search",
        alias="OSM_NOMINATIM_URL",
    )
    # Nominatim and Overpass both require a descriptive User-Agent.
    osm_user_agent: str = Field(
        default="BusinessLeadFinder/2.1 (self-hosted lead research tool)",
        alias="OSM_USER_AGENT",
    )
    osm_timeout: int = Field(default=90, alias="OSM_TIMEOUT")
    osm_max_results: int = Field(default=500, alias="OSM_MAX_RESULTS")
    # Overpass asks for <=1 heavy query at a time from a single client.
    osm_min_interval: float = Field(default=1.5, alias="OSM_MIN_INTERVAL")

    # Discovery engine (grid crawl)
    discovery_concurrency: int = Field(
        default=4, alias="DISCOVERY_CONCURRENCY", ge=1, le=16
    )
    discovery_min_interval: float = Field(
        default=0.35, alias="DISCOVERY_MIN_INTERVAL", ge=0.0
    )
    discovery_max_interval: float = Field(
        default=30.0, alias="DISCOVERY_MAX_INTERVAL", ge=0.1
    )
    # Multiplicative increase applied to the pacing interval on a 429/5xx.
    discovery_backoff_factor: float = Field(
        default=2.0, alias="DISCOVERY_BACKOFF_FACTOR", ge=1.0
    )
    # Multiplicative decrease applied after a streak of clean responses.
    discovery_recovery_factor: float = Field(
        default=0.75, alias="DISCOVERY_RECOVERY_FACTOR", gt=0.0, le=1.0
    )
    discovery_recovery_streak: int = Field(
        default=3, alias="DISCOVERY_RECOVERY_STREAK", ge=1
    )
    # Website scraping during a discovery job. Higher concurrency finishes a
    # big run faster; the scraper's own timeout bounds each site.
    enrichment_concurrency: int = Field(
        default=20, alias="ENRICHMENT_CONCURRENCY", ge=1, le=64
    )
    # Jobs finished longer ago than this are dropped from the in-memory registry.
    discovery_job_ttl: float = Field(
        default=3600.0, alias="DISCOVERY_JOB_TTL", ge=60.0
    )


    # Concurrency
    max_concurrent_tasks: int = Field(
        default=5, alias="MAX_CONCURRENT_TASKS", ge=1
    )

    # Cache
    cache_ttl_seconds: int = Field(
        default=86400, alias="CACHE_TTL_SECONDS", ge=1
    )

    # Database
    database_url: str = Field(
        default="sqlite:///./business_leads.db", alias="DATABASE_URL"
    )

    # Retry
    retry_max_retries: int = Field(default=3, alias="RETRY_MAX_RETRIES", ge=0)
    retry_initial_delay: float = Field(default=0.5, alias="RETRY_INITIAL_DELAY", ge=0)
    retry_backoff_multiplier: float = Field(default=2.0, alias="RETRY_BACKOFF_MULTIPLIER", ge=1)
    retry_max_delay: float = Field(default=10.0, alias="RETRY_MAX_DELAY", ge=0)

    # Website scraping
    scraper_timeout: int = Field(default=8, alias="SCRAPER_TIMEOUT")
    scraper_page_cache_ttl: float = Field(
        default=600.0, alias="SCRAPER_PAGE_CACHE_TTL"
    )

    # Gemini LLM (optional, for AI-powered enrichment)
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.0-flash", alias="GEMINI_MODEL")
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta",
        alias="GEMINI_BASE_URL",
    )
    gemini_timeout: int = Field(default=12, alias="GEMINI_TIMEOUT")
    gemini_max_retries: int = Field(default=2, alias="GEMINI_MAX_RETRIES")

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        alias="LOG_FORMAT",
    )

    # CORS
    cors_origins: list[str] = Field(default=["*"], alias="CORS_ORIGINS")
    cors_allow_credentials: bool = Field(default=True, alias="CORS_ALLOW_CREDENTIALS")
    cors_allow_methods: list[str] = Field(default=["*"], alias="CORS_ALLOW_METHODS")
    cors_allow_headers: list[str] = Field(default=["*"], alias="CORS_ALLOW_HEADERS")

    @property
    def is_development(self) -> bool:
        return self.environment.lower() == "development"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()