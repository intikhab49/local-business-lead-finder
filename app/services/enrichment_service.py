import asyncio
import logging
import time
from typing import Any

from app.cache.cache_manager import CacheManager
from app.core.concurrency import get_max_concurrent_tasks
from app.models.enriched_business import EnrichedBusiness
from app.monitoring.metrics import get_metrics, timer
from app.providers.base import BaseProvider, Business
from app.services.lead_discovery_service import LeadDiscoveryService
from app.services.website_scraper import WebsiteScraperService

logger = logging.getLogger(__name__)


def _log_stage(stage: str, duration: float, extra: str = "") -> None:
    logger.info(
        "[Timing] stage=%s duration_ms=%.1f%s",
        stage,
        duration * 1000,
        f" {extra}" if extra else "",
    )


class EnrichmentService:
    def __init__(
        self,
        scraper: WebsiteScraperService | None = None,
        discovery: LeadDiscoveryService | None = None,
        email_cache: CacheManager | None = None,
    ) -> None:
        self._scraper = scraper or WebsiteScraperService()
        self._discovery = discovery or LeadDiscoveryService(scraper=self._scraper)
        self._email_cache = email_cache if email_cache is not None else CacheManager()

    def enrich(self, business: Business) -> EnrichedBusiness:
        logger.info("Enriching business: %s (%s)", business.name, business.fsq_place_id)

        enriched = EnrichedBusiness.from_business(business)

        enriched.email = self._find_email(business)
        enriched.linkedin, enriched.facebook, enriched.instagram = self._find_social_profiles(business)
        enriched.twitter, enriched.youtube = self._find_additional_social(business)

        enriched.confidence_score = self._calculate_confidence(business, enriched)
        enriched.source = self._determine_source(business)

        return enriched

    async def enrich_by_id(self, provider: BaseProvider, fsq_place_id: str) -> EnrichedBusiness:
        business = await provider.get_details(fsq_place_id)
        return self.enrich(business)

    def _find_email(self, business: Business) -> str | None:
        # Prefer a cached email when enriching synchronously so search
        # responses can surface previously discovered addresses without
        # requiring an async scrape. Do not perform network IO here.
        if not business or not business.fsq_place_id:
            return None
        cached = self._email_cache.get(business.fsq_place_id)
        if isinstance(cached, str) and cached:
            return cached
        return None

    def _find_social_profiles(self, business: Business) -> tuple[str | None, str | None, str | None]:
        return None, None, None

    def _find_additional_social(self, business: Business) -> tuple[str | None, str | None]:
        return None, None

    async def enrich_with_scraper(self, business: Business) -> EnrichedBusiness:
        get_metrics().increment("businesses_processed")
        try:
            enriched = self.enrich(business)
        except Exception:
            logger.exception("Base enrichment failed for %s — returning empty lead", business.name)
            enriched = EnrichedBusiness.from_business(business)

        if not business.website:
            logger.debug("No website to scrape for %s", business.name)
            return enriched

        scrape_start = time.monotonic()
        try:
            logger.info("[Enrich] step=scrape_website url=%s name=%s", business.website, business.name)
            with timer("enrichment"):
                scraped = await self._scraper.scrape(business.website)
            _log_stage("scrape_http", time.monotonic() - scrape_start, business.website)

            scraped_emails = scraped.get("emails") or []
            if isinstance(scraped_emails, list):
                emails = [e for e in scraped_emails if isinstance(e, str)]
            else:
                emails = []
            if emails:
                enriched.email = emails[0]
            logger.info("[Enrich] step=email_extraction_complete name=%s emails=%d", business.name, len(emails))

            enriched.merge(
                linkedin=scraped.get("linkedin"),
                facebook=scraped.get("facebook"),
                instagram=scraped.get("instagram"),
                twitter=scraped.get("twitter"),
                youtube=scraped.get("youtube"),
            )
            logger.info("[Enrich] step=scrape_merge_complete name=%s", business.name)

        except Exception:
            logger.exception("Website scraping failed for %s — continuing without scrape", business.website)
        finally:
            _log_stage("web_scraping", time.monotonic() - scrape_start, business.website)

        return enriched

    async def enrich_with_discovery(self, business: Business) -> EnrichedBusiness:
        enriched = await self.enrich_with_scraper(business)

        try:
            category = business.categories[0] if business.categories else ""
            logger.info(
                "[Enrich] step=lead_discovery name=%s website=%s",
                business.name,
                enriched.website,
            )
            with timer("enrichment"):
                result = await self._discovery.discover(
                    business_name=business.name,
                    category=category,
                    location=business.formatted_address,
                    existing_website=enriched.website,
                )
            logger.info("[Enrich] step=lead_discovery_complete name=%s", business.name)

            merged_fields: dict[str, str | None] = {}
            for key in ("website", "linkedin", "facebook", "instagram", "twitter", "youtube"):
                field_val = getattr(result, key)
                if field_val and field_val.value:
                    merged_fields[key] = field_val.value

            enriched.merge(**merged_fields)

            if result.verification_notes:
                logger.info(
                    "Discovery verification for %s: %s",
                    business.name, "; ".join(result.verification_notes),
                )

        except Exception:
            logger.exception(
                "Lead discovery failed for %s — continuing with scraped data",
                business.name,
            )

        try:
            enriched.confidence_score = self._calculate_confidence(business, enriched)
        except Exception:
            logger.exception("Confidence calculation failed for %s", business.name)
        return enriched

    async def enrich_many(
        self,
        businesses: list[Business],
        max_concurrency: int | None = None,
    ) -> dict[str, EnrichedBusiness]:
        """Run discovery pipeline across many businesses concurrently."""
        if not businesses:
            return {}
        semaphore = asyncio.Semaphore(max_concurrency or get_max_concurrent_tasks())

        async def _discover(business: Business) -> tuple[str, EnrichedBusiness | None]:
            async with semaphore:
                try:
                    enriched = await self.enrich_with_discovery(business)
                    return business.fsq_place_id, enriched
                except Exception:
                    logger.exception("Auto-enrichment failed for %s", business.fsq_place_id)
                    return business.fsq_place_id, None

        outcomes = await asyncio.gather(*(_discover(b) for b in businesses))
        return {
            place_id: enriched
            for place_id, enriched in outcomes
            if place_id is not None and enriched is not None
        }

    async def discover_emails(
        self,
        businesses: list[Business],
        max_concurrency: int | None = None,
    ) -> dict[str, str]:
        if not businesses:
            return {}
        semaphore = asyncio.Semaphore(max_concurrency or get_max_concurrent_tasks())

        async def _discover(business: Business) -> tuple[str, str | None]:
            cached = self._email_cache.get(business.fsq_place_id)
            if isinstance(cached, str) and cached:
                logger.info("[Enrich] Email cache hit for %s", business.fsq_place_id)
                return business.fsq_place_id, cached
            async with semaphore:
                try:
                    enriched = await self.enrich_with_scraper(business)
                except Exception:
                    logger.exception("Email discovery failed for %s", business.fsq_place_id)
                    return business.fsq_place_id, None
                if enriched.email:
                    self._email_cache.set(business.fsq_place_id, enriched.email)
                return business.fsq_place_id, enriched.email

        outcomes = await asyncio.gather(*(_discover(b) for b in businesses))
        return {
            place_id: email
            for place_id, email in outcomes
            if place_id is not None and email is not None
        }

    def _calculate_confidence(self, business: Business, enriched: EnrichedBusiness) -> float:
        score = 0.0
        if business.name:
            score += 0.15
        if business.formatted_address:
            score += 0.15
        if business.phone_number:
            score += 0.15
        if business.website:
            score += 0.1
        if business.latitude and business.longitude:
            score += 0.1
        if enriched.email:
            score += 0.15
        if enriched.linkedin or enriched.facebook or enriched.instagram:
            score += 0.1
        if enriched.twitter or enriched.youtube:
            score += 0.1
        return round(score, 2)

    def _determine_source(self, business: Business) -> str:
        from app.core.config import settings

        return "Google Places" if settings.provider_name == "google_places" else "Foursquare"
