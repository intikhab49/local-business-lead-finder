import logging

from app.agents.base_agent import BaseAgent, track_state
from app.agents.context import AgentContext
from app.agents.message_bus import BaseMessageBus
from app.agents.messaging import AgentMessage, MessageType
from app.agents.models import SearchAgentRequest, SearchAgentResponse
from app.services.business_search_service import BusinessSearchService
from app.tools.search_tool import SearchTool

logger = logging.getLogger(__name__)


class SearchAgent(BaseAgent):
    """Runs the business search step.

    Owns the search responsibility: given a location/category/radius, queries
    the search service and returns structured business records.
    """

    name = "SearchAgent"

    def __init__(
        self,
        search_service: BusinessSearchService,
        context: AgentContext | None = None,
        bus: BaseMessageBus | None = None,
    ) -> None:
        super().__init__(context=context, bus=bus)
        self._search = search_service
        self._tool = SearchTool(search_service)

    @track_state
    async def execute(self, request: SearchAgentRequest) -> SearchAgentResponse:
        logger.info(
            "[SearchAgent] Starting search: category=%s location=%s radius=%d max_results=%d",
            request.business_category, request.location, request.radius, request.max_results,
        )

        result = await self._tool.execute(
            "search",
            None,
            location=request.location,
            business_category=request.business_category,
            radius=request.radius,
            max_results=request.max_results,
        )

        if not result.success:
            logger.warning("[SearchAgent] Search failed: %s", result.error)
            return SearchAgentResponse(
                businesses=[],
                total_results=0,
                success=False,
                error=result.error,
            )

        businesses = result.data.get("businesses", [])
        logger.info("[SearchAgent] Search complete: found=%d businesses", len(businesses))
        return SearchAgentResponse(
            businesses=businesses,
            total_results=len(businesses),
            success=True,
        )

    @track_state
    async def handle_message(self, message: AgentMessage) -> AgentMessage | None:
        if message.message_type != MessageType.REQUEST:
            return await super().handle_message(message)

        payload = message.payload or {}
        request = SearchAgentRequest(
            location=payload.get("location", ""),
            business_category=payload.get("business_category", ""),
            radius=payload.get("radius", 5000),
            max_results=payload.get("max_results", 20),
        )
        response = await self.execute(request)
        if not response.success:
            return self._reply(message, {}, success=False, error=response.error)

        return self._reply(
            message,
            {"businesses": response.businesses, "total_results": response.total_results},
        )
