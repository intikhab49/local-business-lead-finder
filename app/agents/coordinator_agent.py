import logging
import time
from typing import Any

from app.agents.agent_memory import AgentMemory
from app.agents.context import AgentContext
from app.agents.decision_engine import (
    DISCOVER_SOCIAL,
    DISCOVER_WEBSITE,
    EXTRACT_EMAIL,
    SCRAPE_WEBSITE,
    VERIFY_WEBSITE,
    Action,
    BaseDecisionEngine,
    RuleBasedDecisionEngine,
)
from app.agents.discovery_agent import DiscoveryAgent
from app.agents.enrichment_agent import EnrichmentAgent
from app.agents.human_review import HumanReviewAgent
from app.agents.message_bus import create_message_bus
from app.agents.messaging import AgentMessage, AgentState, MessageType
from app.agents.models import (
    AgentRequest,
    SearchAgentRequest,
    ValidationAgentRequest,
)
from app.agents.search_agent import SearchAgent
from app.agents.task_manager import TaskManager
from app.agents.validation_agent import ValidationAgent
from app.cache.cache_manager import CacheManager
from app.models.enriched_business import EnrichedBusiness
from app.monitoring.metrics import get_metrics
from app.providers.base import Business
from app.services.business_search_service import BusinessSearchService
from app.services.enrichment_service import EnrichmentService
from app.services.lead_discovery_service import LeadDiscoveryService
from app.services.website_scraper import WebsiteScraperService
from app.tools.base_tool import BaseTool

logger = logging.getLogger(__name__)


class CoordinatorAgent:
    """Orchestrates the full lead discovery workflow.

    Delegates search, enrichment, discovery and validation to specialized
    agents. Built-in actions dispatch to agents; injected custom tools remain
    supported through the `tools` constructor argument.
    """

    def __init__(
        self,
        search_service: BusinessSearchService,
        enrichment_service: EnrichmentService | None = None,
        discovery_service: LeadDiscoveryService | None = None,
        decision_engine: BaseDecisionEngine | None = None,
        memory: AgentMemory | None = None,
        tools: list[BaseTool] | None = None,
        task_manager: TaskManager | None = None,
        cache: CacheManager | None = None,
        human_review_agent: HumanReviewAgent | None = None,
    ) -> None:
        self._search = search_service
        self._scraper = WebsiteScraperService()
        if discovery_service:
            self._discovery = discovery_service
        else:
            self._discovery = LeadDiscoveryService(scraper=self._scraper)
        if enrichment_service:
            self._enrichment = enrichment_service
        else:
            self._enrichment = EnrichmentService(
                scraper=self._scraper,
                discovery=self._discovery,
            )
        self._engine = decision_engine or RuleBasedDecisionEngine()
        self._memory = memory or AgentMemory()
        self._task_manager = task_manager or TaskManager()
        self._cache = cache if cache is not None else CacheManager()

        self._context = AgentContext()
        self._bus = create_message_bus()

        self.search_agent = SearchAgent(self._search, context=self._context, bus=self._bus)
        self.enrichment_agent = EnrichmentAgent(
            scraper=self._scraper,
            context=self._context,
            bus=self._bus,
        )
        self.discovery_agent = DiscoveryAgent(
            discovery_service=self._discovery,
            scraper=self._scraper,
            context=self._context,
            bus=self._bus,
        )
        self.validation_agent = ValidationAgent(context=self._context, bus=self._bus)

        for agent in (
            self.search_agent,
            self.enrichment_agent,
            self.discovery_agent,
            self.validation_agent,
        ):
            self._bus.subscribe(agent.name, agent.handle_message)
        self._bus.subscribe("*", self._handle_event)

        self.human_review_agent = human_review_agent
        if human_review_agent is not None:
            human_review_agent.set_memory(self._memory)
            human_review_agent.bind(self._context, self._bus)
            self._bus.subscribe(
                human_review_agent.name, human_review_agent.handle_message
            )

        self._context.set_state("CoordinatorAgent", AgentState.IDLE)

        self._action_agent_map: dict[str, object] = {
            DISCOVER_WEBSITE.name: self.discovery_agent,
            VERIFY_WEBSITE.name: self.discovery_agent,
            SCRAPE_WEBSITE.name: self.enrichment_agent,
            EXTRACT_EMAIL.name: self.enrichment_agent,
            DISCOVER_SOCIAL.name: self.discovery_agent,
        }

        self._action_tool_map: dict[str, BaseTool] = {}
        for tool in tools or []:
            for action_name in tool.supported_actions:
                self._action_tool_map[action_name] = tool

    @property
    def provider_name(self) -> str:
        return self._search.provider.provider_name

    async def _handle_event(self, message: AgentMessage) -> AgentMessage | None:
        """Wildcard bus handler that observes events for shared context."""
        if message.message_type != MessageType.EVENT:
            return None
        logger.info(
            "[Coordinator] Event received from=%s payload_keys=%s",
            message.sender,
            sorted((message.payload or {}).keys()),
        )
        if message.payload:
            self._context.update_shared(**message.payload)
        return None

    async def search(
        self,
        location: str,
        business_category: str,
        radius: int = 5000,
        max_results: int = 20,
    ) -> dict[str, Any]:
        start = time.monotonic()
        self._context.set_state("CoordinatorAgent", AgentState.BUSY)

        logger.info(
            "[Coordinator] Starting lead search: category=%s location=%s radius=%d max_results=%d",
            business_category, location, radius, max_results,
        )

        try:
            result = await self._search_inner(
                location, business_category, radius, max_results, start
            )
        except Exception:
            self._context.set_state("CoordinatorAgent", AgentState.FAILED)
            raise
        self._context.set_state("CoordinatorAgent", AgentState.COMPLETE)
        return result

    async def _search_inner(
        self,
        location: str,
        business_category: str,
        radius: int,
        max_results: int,
        start: float,
    ) -> dict[str, Any]:
        search_request = SearchAgentRequest(
            location=location,
            business_category=business_category,
            radius=radius,
            max_results=max_results,
        )
        search_response = await self.search_agent.execute(search_request)

        if not search_response.success:
            raise RuntimeError(f"Search failed: {search_response.error}")

        business_dicts = search_response.businesses
        logger.info(
            "[Coordinator] Search complete: found=%d businesses",
            len(business_dicts),
        )

        businesses = [self._dict_to_business(biz_dict) for biz_dict in business_dicts]
        for idx, business in enumerate(businesses, start=1):
            logger.info(
                "[Coordinator] Processing lead %d/%d: id=%s name=%s",
                idx, len(businesses), business.fsq_place_id, business.name,
            )

        processed = await self._task_manager.run(
            businesses,
            self._process_single_lead,
        )

        enriched_leads: list[EnrichedBusiness] = []
        failed_count = 0

        for business, enriched in zip(businesses, processed, strict=True):
            if enriched is None:
                failed_count += 1
                continue

            enriched_leads.append(enriched)
            logger.info(
                "[Coordinator] Lead enriched: id=%s name=%s confidence=%.2f "
                "email=%s linkedin=%s facebook=%s instagram=%s twitter=%s youtube=%s",
                business.fsq_place_id,
                business.name,
                enriched.confidence_score,
                enriched.email or "none",
                enriched.linkedin or "none",
                enriched.facebook or "none",
                enriched.instagram or "none",
                enriched.twitter or "none",
                enriched.youtube or "none",
            )

        enriched_leads.sort(key=lambda x: x.confidence_score, reverse=True)

        elapsed_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "[Coordinator] Lead search finished: total=%d enriched=%d failed=%d time=%dms",
            len(business_dicts),
            len(enriched_leads),
            failed_count,
            elapsed_ms,
        )

        return {
            "leads": [lead.to_dict() for lead in enriched_leads],
            "total_results": len(enriched_leads),
            "search_location": location,
            "search_category": business_category,
            "search_radius": radius,
            "processing_time_ms": elapsed_ms,
        }

    async def _process_single_lead(self, business: Business) -> EnrichedBusiness | None:
        get_metrics().increment("businesses_processed")
        cached = self._cache.get(business.fsq_place_id)
        if cached is not None:
            logger.info(
                "[Coordinator] Cache hit for %s: returning cached lead", business.fsq_place_id,
            )
            return cached

        enriched = EnrichedBusiness.from_business(business)
        seen_actions: set[str] = set()
        max_rounds = 3

        for round_num in range(max_rounds):
            state = self._build_state(business, enriched)

            try:
                actions = await self._engine.decide(state)
            except Exception as e:
                logger.warning(
                    "[Coordinator] Engine decision failed for %s (round %d): %s",
                    business.fsq_place_id, round_num, e,
                )
                break
            pending = [a for a in actions if a.name not in seen_actions]
            if not pending:
                if all(a.name == "skip_enrichment" for a in actions):
                    break
                break

            pending.sort(key=lambda a: a.priority)

            for action in pending:
                seen_actions.add(action.name)
                try:
                    await self._execute_tool_action(action, business, enriched)
                except Exception as e:
                    logger.warning(
                        "[Coordinator] Action %s failed for %s: %s",
                        action.name, business.fsq_place_id, e,
                    )

            enriched.confidence_score = self._calculate_confidence(business, enriched)

        if not enriched.email and not enriched.linkedin and not enriched.facebook:
            if not business.website and not business.phone_number:
                enriched.confidence_score = 0.1

        enriched.source = self._search.provider.provider_name

        validation = await self.validation_agent.execute(
            ValidationAgentRequest(
                business=business.to_dict(),
                enriched=enriched.to_dict(),
            )
        )
        if validation.issues:
            logger.info(
                "[Coordinator] Validation issues for %s: %s",
                business.name, "; ".join(validation.issues),
            )
            self._memory.set(business.fsq_place_id, "validation_issues", validation.issues)
        else:
            self._memory.set(business.fsq_place_id, "validation_issues", [])

        if validation.feedback:
            logger.info(
                "[Coordinator] Applying agent feedback for %s: %s",
                business.name, validation.feedback,
            )
            enriched.merge(**validation.feedback)
            for key, value in validation.feedback.items():
                self._memory.set(business.fsq_place_id, key, value)
            enriched.confidence_score = self._calculate_confidence(business, enriched)

        if self.human_review_agent is not None and self._needs_review(
            enriched, validation.issues
        ):
            logger.info(
                "[Coordinator] Routing lead to human review: id=%s name=%s confidence=%.2f issues=%s",
                business.fsq_place_id,
                business.name,
                enriched.confidence_score,
                "; ".join(validation.issues) if validation.issues else "none",
            )
            await self.human_review_agent.submit(
                fsq_place_id=business.fsq_place_id,
                reviewer="system",
                enriched=enriched.to_dict(),
            )

        self._cache.set(business.fsq_place_id, enriched)
        return enriched

    async def _execute_tool_action(
        self,
        action: Action,
        business: Business,
        enriched: EnrichedBusiness,
    ) -> None:
        tool = self._action_tool_map.get(action.name)
        if tool is not None:
            await self._execute_via_tool(tool, action, business, enriched)
            return

        agent = self._action_agent_map.get(action.name)
        if agent is not None:
            await self._execute_via_agent(agent, action, business, enriched)
            return

        logger.warning(
            "[Coordinator] No agent or tool registered for action: %s", action.name,
        )

    async def _execute_via_tool(
        self,
        tool: BaseTool,
        action: Action,
        business: Business,
        enriched: EnrichedBusiness,
    ) -> None:
        website = enriched.website or business.website
        result = await tool.execute(
            action.name,
            business,
            website=website,
            business_name=business.name,
            category=business.categories[0] if business.categories else "",
            location=business.formatted_address,
        )

        if result.success and result.data:
            enriched.merge(**result.data)
            for key, value in result.data.items():
                self._memory.set(business.fsq_place_id, key, value)

            logger.info(
                "[Coordinator] Tool %s returned for %s: %s",
                action.name, business.name,
                ", ".join(f"{k}={v}" for k, v in result.data.items()),
            )

    async def _execute_via_agent(
        self,
        agent: object,
        action: Action,
        business: Business,
        enriched: EnrichedBusiness,
    ) -> None:
        website = enriched.website or business.website
        request = AgentRequest(
            action=action.name,
            business=business.to_dict(),
            website=website,
            business_name=business.name,
            category=business.categories[0] if business.categories else "",
            location=business.formatted_address,
        )
        result = await agent.execute(request)  # type: ignore[attr-defined]

        if result.success and result.data:
            enriched.merge(**result.data)
            for key, value in result.data.items():
                self._memory.set(business.fsq_place_id, key, value)

            logger.info(
                "[Coordinator] Agent %s returned for %s: %s",
                action.name, business.name,
                ", ".join(f"{k}={v}" for k, v in result.data.items()),
            )

    def _build_state(self, business: Business, enriched: EnrichedBusiness) -> dict[str, Any]:
        return {
            "name": business.name,
            "business_name": enriched.business_name,
            "website": enriched.website or business.website,
            "email": enriched.email,
            "phone": enriched.phone or business.phone_number,
            "fsq_place_id": business.fsq_place_id,
            "formatted_address": business.formatted_address,
        }

    @staticmethod
    def _needs_review(enriched: EnrichedBusiness, issues: list[str]) -> bool:
        return (
            enriched.confidence_score < 0.7
            or not enriched.website
            or not enriched.email
            or bool(issues)
        )

    def _calculate_confidence(
        self,
        business: Business,
        enriched: EnrichedBusiness,
    ) -> float:
        score = 0.0
        if business.name:
            score += 0.15
        if business.formatted_address:
            score += 0.15
        if business.phone_number or enriched.phone:
            score += 0.15
        if business.website or enriched.website:
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

    @staticmethod
    def _dict_to_business(d: dict) -> Business:
        return Business.from_dict(d)
