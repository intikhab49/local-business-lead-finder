import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from app.services.website_scraper import WebsiteScraperService

logger = logging.getLogger(__name__)


def _log_stage(stage: str, duration: float, extra: str = "") -> None:
    logger.info(
        "[Timing] stage=%s duration_ms=%.1f%s",
        stage,
        duration * 1000,
        f" {extra}" if extra else "",
    )


@dataclass
class DiscoveredField:
    value: str | None
    confidence: float = 0.0
    source: str = ""
    method: str = ""


@dataclass
class DiscoveryResult:
    website: DiscoveredField | None = None
    linkedin: DiscoveredField | None = None
    facebook: DiscoveredField | None = None
    instagram: DiscoveredField | None = None
    twitter: DiscoveredField | None = None
    youtube: DiscoveredField | None = None
    verification_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_name in ("website", "linkedin", "facebook", "instagram", "twitter", "youtube"):
            df: DiscoveredField | None = getattr(self, field_name)
            if df and df.value:
                result[field_name] = {
                    "value": df.value,
                    "confidence": df.confidence,
                    "source": df.source,
                    "method": df.method,
                }
        result["verification_notes"] = self.verification_notes
        return result

    def get_flat(self) -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        for field_name in ("website", "linkedin", "facebook", "instagram", "twitter", "youtube"):
            df: DiscoveredField | None = getattr(self, field_name)
            if df:
                result[field_name] = df.value
            else:
                result[field_name] = None
        return result


class LeadDiscoveryService:
    def __init__(self, scraper: WebsiteScraperService | None = None) -> None:
        self._scraper = scraper or WebsiteScraperService()

    async def discover(
        self,
        business_name: str,
        category: str = "",
        location: str = "",
        existing_website: str | None = None,
    ) -> DiscoveryResult:
        logger.info(
            "Discovering leads for: %s (category=%s, location=%s)",
            business_name, category, location,
        )
        overall_start = time.monotonic()

        result = DiscoveryResult()

        website_start = time.monotonic()
        website_candidate = existing_website

        if not website_candidate:
            logger.info("[Discovery] step=find_website name=%s", business_name)
            try:
                website_candidate = await self.find_website(business_name, category, location)
                if website_candidate:
                    result.website = website_candidate
            except Exception:
                logger.exception("Website discovery failed for %s — continuing", business_name)
                website_candidate = None
            _log_stage("website_discovery", time.monotonic() - website_start)

        if website_candidate:
            await self._populate_from_website(
                result,
                website_candidate,
                business_name,
            )

        _log_stage("discovery_total", time.monotonic() - overall_start)
        return result

    async def _populate_from_website(
        self,
        result: DiscoveryResult,
        website_candidate: str,
        business_name: str,
    ) -> None:
        """Verify the website and extract social profiles concurrently.

        Both steps read the homepage; the scraper's page cache deduplicates
        the underlying download so they run in parallel without duplicate
        network round-trips. Failures in one step never block the other.
        """

        async def _verify() -> DiscoveredField | None:
            start = time.monotonic()
            try:
                verified = await self._verify_website(website_candidate, business_name)
                return verified
            except Exception:
                logger.exception(
                    "Website verification failed for %s",
                    website_candidate,
                )
                return None
            finally:
                _log_stage(
                    "website_verification",
                    time.monotonic() - start,
                    website_candidate,
                )

        async def _social() -> dict[str, str | None]:
            start = time.monotonic()
            try:
                return await self._discover_social_from_website(website_candidate)
            except Exception:
                logger.exception(
                    "Social discovery failed for %s",
                    website_candidate,
                )
                return {
                    "linkedin": None,
                    "facebook": None,
                    "instagram": None,
                    "twitter": None,
                    "youtube": None,
                }
            finally:
                _log_stage(
                    "social_discovery",
                    time.monotonic() - start,
                    website_candidate,
                )

        try:
            verified, social = await asyncio.gather(_verify(), _social())
        except Exception:
            logger.exception(
                "Website population failed for %s — returning unverified website",
                website_candidate,
            )
            verified, social = None, {}

        result.website = verified or DiscoveredField(
            value=website_candidate,
            confidence=0.3,
            source="provided",
            method="unverified",
        )

        for field_name in ("linkedin", "facebook", "instagram", "twitter", "youtube"):
            value = social.get(field_name)
            if value:
                setattr(
                    result,
                    field_name,
                    DiscoveredField(
                        value=value,
                        confidence=0.7,
                        source="website_scrape",
                        method="social_link_extraction",
                    ),
                )

    async def find_website(
        self,
        business_name: str,
        category: str = "",
        location: str = "",
    ) -> DiscoveredField | None:
        return None

    async def _verify_website(self, url: str, business_name: str) -> DiscoveredField | None:
        try:
            html = await self._scraper._download_page(url)
            if not html:
                return None

            soup = BeautifulSoup(html, "html.parser")

            name_lower = business_name.lower().strip()
            title = (soup.title.string or "") if soup.title else ""
            meta_desc = ""
            meta_tag = soup.find("meta", attrs={"name": "description"})
            if meta_tag and meta_tag.get("content"):
                meta_desc = meta_tag["content"]
            body_text = soup.get_text()[:5000]

            confidence = 0.0
            notes: list[str] = []

            if name_lower in title.lower():
                confidence += 0.5
                notes.append("business name found in page title")
            if name_lower in meta_desc.lower():
                confidence += 0.3
                notes.append("business name found in meta description")
            if name_lower in body_text.lower():
                confidence += 0.2

            if confidence == 0.0:
                notes.append("business name not found on page — may not be the official site")
                confidence = 0.1

            logger.info(
                "Website verification for %s: confidence=%.2f — %s",
                url, confidence, "; ".join(notes),
            )

            return DiscoveredField(
                value=url,
                confidence=round(confidence, 2),
                source="website_verification",
                method="page_content_check",
            )

        except Exception as e:
            logger.warning("Website verification failed for %s: %s", url, e)
            return None

    async def _discover_social_from_website(self, url: str) -> dict[str, str | None]:
        try:
            scraped = await self._scraper.scrape(url)
            return {
                "linkedin": scraped.get("linkedin"),
                "facebook": scraped.get("facebook"),
                "instagram": scraped.get("instagram"),
                "twitter": scraped.get("twitter"),
                "youtube": scraped.get("youtube"),
            }
        except Exception as e:
            logger.warning("Social discovery from website failed: %s", e)
            return {
                "linkedin": None,
                "facebook": None,
                "instagram": None,
                "twitter": None,
                "youtube": None,
            }
