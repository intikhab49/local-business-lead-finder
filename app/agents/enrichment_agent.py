import logging

from app.agents.base_agent import BaseAgent, track_state
from app.agents.context import AgentContext
from app.agents.message_bus import BaseMessageBus
from app.agents.messaging import AgentMessage, MessageType
from app.agents.models import AgentRequest, AgentResponse
from app.services.website_scraper import WebsiteScraperService
from app.tools.enrichment_tool import EnrichmentTool

logger = logging.getLogger(__name__)


class EnrichmentAgent(BaseAgent):
    """Handles website scraping and email extraction.

    Owns the enrichment responsibility: scrapes a business website for contact
    information and returns structured enrichment data.
    """

    name = "EnrichmentAgent"

    def __init__(
        self,
        scraper: WebsiteScraperService | None = None,
        context: AgentContext | None = None,
        bus: BaseMessageBus | None = None,
    ) -> None:
        super().__init__(context=context, bus=bus)
        self._scraper = scraper or WebsiteScraperService()
        self._tool = EnrichmentTool(scraper=self._scraper)

    @track_state
    async def execute(self, request: AgentRequest) -> AgentResponse:
        logger.info(
            "[EnrichmentAgent] Executing action=%s for business=%s",
            request.action, request.business_name or "unknown",
        )

        result = await self._tool.execute(
            request.action,
            None,
            website=request.website,
            business_name=request.business_name,
            category=request.category,
            location=request.location,
        )

        if not result.success:
            logger.warning("[EnrichmentAgent] Action %s failed: %s", request.action, result.error)
            return AgentResponse(success=False, data={}, error=result.error)

        logger.info("[EnrichmentAgent] Action %s succeeded: %s", request.action, result.data)
        return AgentResponse(success=True, data=result.data, error=None)

    @track_state
    async def handle_message(self, message: AgentMessage) -> AgentMessage | None:
        if message.message_type != MessageType.REQUEST:
            return await super().handle_message(message)

        payload = message.payload or {}
        request = AgentRequest(
            action=payload.get("action", ""),
            business=payload.get("business", {}),
            website=payload.get("website"),
            business_name=payload.get("business_name", ""),
            category=payload.get("category", ""),
            location=payload.get("location", ""),
        )
        response = await self.execute(request)
        return self._reply(
            message,
            response.data,
            success=response.success,
            error=response.error,
        )
