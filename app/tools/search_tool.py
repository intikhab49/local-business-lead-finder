import logging
import time

from app.providers.base import Business
from app.services.business_search_service import BusinessSearchService
from app.tools.base_tool import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class SearchTool(BaseTool):
    name = "search"

    def __init__(self, search_service: BusinessSearchService) -> None:
        self._search = search_service

    async def execute(
        self,
        action: str = "search",
        business: Business | None = None,
        **kwargs: str | int | float | None,
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
        **kwargs: str | int | float | None,
    ) -> ToolResult:
        logger.info(
            "[Tool] Search: location=%s category=%s radius=%s max_results=%s",
            kwargs.get("location"), kwargs.get("business_category"),
            kwargs.get("radius", 5000), kwargs.get("max_results", 20),
        )

        try:
            results = await self._search.search(
                location=str(kwargs.get("location", "")),
                business_category=str(kwargs.get("business_category", "")),
                radius=int(kwargs.get("radius", 5000)),
                max_results=int(kwargs.get("max_results", 20)),
            )
            logger.info("[Tool] Search: found %d businesses", len(results))
            return ToolResult(success=True, data={"businesses": results})
        except Exception as e:
            logger.warning("[Tool] Search: failed — %s", e)
            return ToolResult(success=False, error=str(e))
