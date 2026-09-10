import logging
import time
from typing import Any

from app.providers.base import Business
from app.services.website_scraper import WebsiteScraperService
from app.tools.base_tool import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class EnrichmentTool(BaseTool):
    name = "enrichment"

    @property
    def supported_actions(self) -> list[str]:
        return ["scrape_website", "extract_email"]

    def __init__(self, scraper: WebsiteScraperService | None = None) -> None:
        self._scraper = scraper or WebsiteScraperService()

    async def execute(
        self,
        action: str,
        business: Business | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        start = time.monotonic()
        try:
            return await self._execute(action, business, **kwargs)
        finally:
            self._record_tool_metrics(start)

    async def _execute(
        self,
        action: str,
        business: Business | None,
        **kwargs: Any,
    ) -> ToolResult:
        if action == "scrape_website":
            return await self._execute_scrape(business, **kwargs)
        if action == "extract_email":
            return await self._execute_extract_email(business, **kwargs)
        return ToolResult(success=False, error=f"Unsupported action: {action}")

    async def _execute_scrape(
        self,
        business: Business | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        url = kwargs.get("website") or (business.website if business else None)
        if not url:
            return ToolResult(success=True, data={})

        logger.info("[Tool] Enrichment: scraping website=%s", url)

        try:
            scraped = await self._scraper.scrape(url)
            data: dict[str, Any] = {
                "email": scraped.get("emails", [None])[0] if scraped.get("emails") else None,
            }
            for field in ("linkedin", "facebook", "instagram", "twitter", "youtube"):
                data[field] = scraped.get(field)

            found = [k for k, v in data.items() if v]
            if found:
                logger.info("[Tool] Enrichment: found %s", ", ".join(found))

            return ToolResult(success=True, data=data)
        except Exception as e:
            logger.warning("[Tool] Enrichment: scrape failed — %s", e)
            return ToolResult(success=False, error=str(e))

    async def _execute_extract_email(
        self,
        business: Business | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        url = kwargs.get("website") or (business.website if business else None)
        if not url:
            return ToolResult(success=True, data={})

        logger.info("[Tool] Enrichment: extracting email from %s", url)

        try:
            scraped = await self._scraper.scrape(url)
            emails = scraped.get("emails", [])
            if emails:
                logger.info("[Tool] Enrichment: found email=%s", emails[0])
                return ToolResult(success=True, data={"email": emails[0]})
            return ToolResult(success=True, data={})
        except Exception as e:
            logger.warning("[Tool] Enrichment: email extraction failed — %s", e)
            return ToolResult(success=False, error=str(e))
