import logging
import time
from typing import Any

from app.providers.base import Business
from app.services.lead_discovery_service import LeadDiscoveryService
from app.services.website_scraper import WebsiteScraperService
from app.tools.base_tool import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class DiscoveryTool(BaseTool):
    name = "discovery"

    @property
    def supported_actions(self) -> list[str]:
        return ["discover_website", "discover_social", "verify_website"]

    def __init__(
        self,
        discovery_service: LeadDiscoveryService | None = None,
        scraper: WebsiteScraperService | None = None,
    ) -> None:
        self._scraper = scraper or WebsiteScraperService()
        self._discovery = discovery_service or LeadDiscoveryService(scraper=self._scraper)

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
        if action == "discover_website":
            return await self._execute_discover_website(business, **kwargs)
        if action == "discover_social":
            return await self._execute_discover_social(business, **kwargs)
        if action == "verify_website":
            return await self._execute_verify_website(business, **kwargs)
        return ToolResult(success=False, error=f"Unsupported action: {action}")

    async def _execute_discover_website(
        self,
        business: Business | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        name = business.name if business else kwargs.get("business_name", "")
        category = (
            business.categories[0]
            if business and business.categories
            else kwargs.get("category", "")
        )
        location = (
            business.formatted_address
            if business
            else kwargs.get("location", "")
        )

        logger.info("[Tool] Discovery: discovering website for business=%s", name)
        logger.info("[Tool] Discovery: category=%s location=%s", category, location)

        try:
            result = await self._discovery.discover(
                business_name=name,
                category=category,
                location=location,
                existing_website=None,
            )

            data: dict[str, Any] = {}
            if result.website and result.website.value:
                data["website"] = result.website.value
                logger.info(
                    "[Tool] Discovery: found website=%s (confidence=%.2f)",
                    result.website.value, result.website.confidence,
                )

            for field in ("linkedin", "facebook", "instagram", "twitter", "youtube"):
                df = getattr(result, field)
                if df and df.value:
                    data[field] = df.value

            found = [f"{k}={v}" for k, v in data.items()]
            if found:
                logger.info("[Tool] Discovery: discovered %s", ", ".join(found))

            return ToolResult(success=True, data=data)
        except Exception as e:
            logger.warning("[Tool] Discovery: website discovery failed — %s", e)
            return ToolResult(success=False, error=str(e))

    async def _execute_discover_social(
        self,
        business: Business | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        url = kwargs.get("website") or (business.website if business else None)
        if not url:
            return ToolResult(success=True, data={})

        name = business.name if business else kwargs.get("business_name", "")
        category = (
            business.categories[0]
            if business and business.categories
            else kwargs.get("category", "")
        )
        location = (
            business.formatted_address
            if business
            else kwargs.get("location", "")
        )

        logger.info("[Tool] Discovery: discovering social for business=%s url=%s", name, url)

        try:
            result = await self._discovery.discover(
                business_name=name,
                category=category,
                location=location,
                existing_website=url,
            )

            data: dict[str, Any] = {}
            for field in ("linkedin", "facebook", "instagram", "twitter", "youtube"):
                df = getattr(result, field)
                if df and df.value:
                    data[field] = df.value

            if data:
                logger.info(
                    "[Tool] Discovery: social found %s",
                    ", ".join(f"{k}={v}" for k, v in data.items()),
                )

            return ToolResult(success=True, data=data)
        except Exception as e:
            logger.warning("[Tool] Discovery: social discovery failed — %s", e)
            return ToolResult(success=False, error=str(e))

    async def _execute_verify_website(
        self,
        business: Business | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        url = kwargs.get("website") or (business.website if business else None)
        if not url:
            return ToolResult(success=True, data={})

        name = business.name if business else kwargs.get("business_name", "")

        logger.info("[Tool] Discovery: verifying website for business=%s url=%s", name, url)

        try:
            discovered = await self._discovery._verify_website(url, name)
            if discovered and discovered.value:
                logger.info(
                    "[Tool] Discovery: website verified confidence=%.2f",
                    discovered.confidence,
                )
                return ToolResult(success=True, data={"website": discovered.value})
            return ToolResult(success=True, data={})
        except Exception as e:
            logger.warning("[Tool] Discovery: website verification failed — %s", e)
            return ToolResult(success=False, error=str(e))
