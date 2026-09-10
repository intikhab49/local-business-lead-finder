from unittest.mock import AsyncMock, patch

import pytest

from app.agents.coordinator_agent import CoordinatorAgent
from app.agents.decision_engine import (
    DISCOVER_SOCIAL,
    DISCOVER_WEBSITE,
    EXTRACT_EMAIL,
    SCRAPE_WEBSITE,
    VERIFY_WEBSITE,
    RuleBasedDecisionEngine,
)
from app.agents.discovery_agent import DiscoveryAgent
from app.agents.enrichment_agent import EnrichmentAgent
from app.agents.models import (
    AgentRequest,
    AgentResponse,
    SearchAgentRequest,
    ValidationAgentRequest,
    ValidationAgentResponse,
)
from app.agents.search_agent import SearchAgent
from app.agents.validation_agent import ValidationAgent
from app.cache.cache_manager import CacheManager
from app.models.enriched_business import EnrichedBusiness
from app.providers.base import Business
from app.services.lead_discovery_service import (
    DiscoveredField,
    DiscoveryResult,
)


class FakeProvider:
    provider_name = "foursquare"


class FakeSearchService:
    def __init__(self):
        self.provider = FakeProvider()

    async def search(self, location="", business_category="", radius=5000, max_results=20):
        return [
            {
                "fsq_place_id": "id1",
                "name": "Pizza Place",
                "formatted_address": "123 Main St",
                "phone_number": "555-0100",
                "website": "https://pizza.example.com",
                "rating": 7.5,
                "categories": ["pizza", "italian"],
                "latitude": 40.7128,
                "longitude": -74.0060,
                "opening_hours": ["Mon-Fri 9-5"],
                "price_level": 2,
            },
            {
                "fsq_place_id": "id2",
                "name": "Sushi Bar",
                "formatted_address": "456 Oak Ave",
                "phone_number": "555-0200",
                "website": None,
                "rating": 8.0,
                "categories": ["sushi", "japanese"],
                "latitude": 40.7282,
                "longitude": -73.7949,
                "opening_hours": [],
                "price_level": 3,
            },
        ]


FAKE_SCRAPE = {
    "emails": ["contact@pizzaplace.com"],
    "linkedin": "https://linkedin.com/company/id1",
    "facebook": None,
    "instagram": None,
    "twitter": None,
    "youtube": None,
}


def fake_discovery_result(**overrides):
    kwargs = {
        "website": DiscoveredField(value="https://sushibar.com", confidence=0.8, source="test", method="test"),
        "verification_notes": ["discovered via test"],
    }
    kwargs.update(overrides)
    return DiscoveryResult(**kwargs)


class TestAgentModels:
    def test_search_agent_request(self):
        req = SearchAgentRequest(location="NYC", business_category="pizza", radius=1000, max_results=5)
        assert req.location == "NYC"
        assert req.business_category == "pizza"
        assert req.radius == 1000
        assert req.max_results == 5

    def test_agent_request_defaults(self):
        req = AgentRequest(action="scrape_website", business_name="Pizza")
        assert req.website is None
        assert req.business == {}
        assert req.category == ""

    def test_agent_response(self):
        resp = AgentResponse(success=True, data={"email": "a@b.com"})
        assert resp.success is True
        assert resp.data == {"email": "a@b.com"}
        assert resp.error is None

    def test_validation_response(self):
        resp = ValidationAgentResponse(valid=False, confidence_score=0.2, issues=["missing email"])
        assert resp.valid is False
        assert resp.issues == ["missing email"]


class TestSearchAgent:
    @pytest.mark.asyncio
    async def test_execute_success(self):
        agent = SearchAgent(search_service=FakeSearchService())
        response = await agent.execute(
            SearchAgentRequest(location="NYC", business_category="restaurant")
        )
        assert response.success is True
        assert response.total_results == 2
        assert response.businesses[0]["name"] == "Pizza Place"

    @pytest.mark.asyncio
    async def test_execute_failure(self):
        class FailingSearchService:
            provider = FakeProvider()

            async def search(self, **kwargs):
                raise RuntimeError("boom")

        agent = SearchAgent(search_service=FailingSearchService())
        response = await agent.execute(SearchAgentRequest(location="NYC", business_category="x"))
        assert response.success is False
        assert response.error == "boom"
        assert response.businesses == []
        assert response.total_results == 0

    @pytest.mark.asyncio
    async def test_execute_logs(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        agent = SearchAgent(search_service=FakeSearchService())
        await agent.execute(SearchAgentRequest(location="NYC", business_category="restaurant"))
        assert any("[SearchAgent] Starting search" in msg for msg in caplog.messages)
        assert any("[SearchAgent] Search complete: found=2 businesses" in msg for msg in caplog.messages)


class TestEnrichmentAgent:
    @pytest.mark.asyncio
    async def test_scrape_website(self):
        agent = EnrichmentAgent()
        with patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)):
            response = await agent.execute(
                AgentRequest(
                    action="scrape_website",
                    business_name="Pizza Place",
                    website="https://pizza.example.com",
                )
            )
        assert response.success is True
        assert response.data["email"] == "contact@pizzaplace.com"
        assert response.data["linkedin"] == "https://linkedin.com/company/id1"

    @pytest.mark.asyncio
    async def test_extract_email(self):
        agent = EnrichmentAgent()
        with patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)):
            response = await agent.execute(
                AgentRequest(action="extract_email", website="https://pizza.example.com")
            )
        assert response.success is True
        assert response.data["email"] == "contact@pizzaplace.com"

    @pytest.mark.asyncio
    async def test_no_website_returns_empty(self):
        agent = EnrichmentAgent()
        response = await agent.execute(AgentRequest(action="scrape_website", business_name="Pizza"))
        assert response.success is True
        assert response.data == {}

    @pytest.mark.asyncio
    async def test_unsupported_action(self):
        agent = EnrichmentAgent()
        response = await agent.execute(AgentRequest(action="bogus"))
        assert response.success is False
        assert response.error is not None

    @pytest.mark.asyncio
    async def test_logs_prefix(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        agent = EnrichmentAgent()
        with patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)):
            await agent.execute(
                AgentRequest(action="scrape_website", business_name="Pizza", website="https://pizza.example.com")
            )
        assert any("[EnrichmentAgent] Executing action=scrape_website" in msg for msg in caplog.messages)
        assert any("[EnrichmentAgent] Action scrape_website succeeded" in msg for msg in caplog.messages)


class TestDiscoveryAgent:
    @pytest.mark.asyncio
    async def test_discover_website(self):
        agent = DiscoveryAgent()
        with patch.object(agent._discovery, "discover", new=AsyncMock(return_value=fake_discovery_result())):
            response = await agent.execute(
                AgentRequest(
                    action="discover_website",
                    business_name="Sushi Bar",
                    category="sushi",
                    location="456 Oak Ave",
                )
            )
        assert response.success is True
        assert response.data["website"] == "https://sushibar.com"

    @pytest.mark.asyncio
    async def test_discover_social(self):
        agent = DiscoveryAgent()
        result = fake_discovery_result(
            website=None,
            linkedin=DiscoveredField(value="https://linkedin.com/company/sushi", confidence=0.7, source="test", method="test"),
        )
        with patch.object(agent._discovery, "discover", new=AsyncMock(return_value=result)):
            response = await agent.execute(
                AgentRequest(
                    action="discover_social",
                    business_name="Sushi Bar",
                    website="https://sushibar.com",
                )
            )
        assert response.success is True
        assert response.data["linkedin"] == "https://linkedin.com/company/sushi"

    @pytest.mark.asyncio
    async def test_verify_website(self):
        agent = DiscoveryAgent()
        with patch.object(agent._discovery, "_verify_website", new=AsyncMock(return_value=fake_discovery_result().website)):
            response = await agent.execute(
                AgentRequest(
                    action="verify_website",
                    business_name="Sushi Bar",
                    website="https://sushibar.com",
                )
            )
        assert response.success is True
        assert response.data["website"] == "https://sushibar.com"

    @pytest.mark.asyncio
    async def test_unsupported_action(self):
        agent = DiscoveryAgent()
        response = await agent.execute(AgentRequest(action="bogus"))
        assert response.success is False

    @pytest.mark.asyncio
    async def test_logs_prefix(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        agent = DiscoveryAgent()
        with patch.object(agent._discovery, "discover", new=AsyncMock(return_value=fake_discovery_result())):
            await agent.execute(
                AgentRequest(action="discover_website", business_name="Sushi", category="sushi")
            )
        assert any("[DiscoveryAgent] Executing action=discover_website" in msg for msg in caplog.messages)


class TestValidationAgent:
    @pytest.mark.asyncio
    async def test_valid_lead(self):
        agent = ValidationAgent()
        response = await agent.execute(
            ValidationAgentRequest(
                business={"name": "Pizza", "formatted_address": "123 Main St"},
                enriched={
                    "email": "a@b.com",
                    "phone": "555",
                    "website": "https://pizza.example.com",
                    "confidence_score": 0.8,
                },
            )
        )
        assert response.valid is True
        assert response.issues == []
        assert response.confidence_score == 0.8

    @pytest.mark.asyncio
    async def test_missing_critical_fields_invalid(self):
        agent = ValidationAgent()
        response = await agent.execute(
            ValidationAgentRequest(
                business={},
                enriched={"email": "a@b.com", "confidence_score": 0.9},
            )
        )
        assert response.valid is False
        assert "missing business name" in response.issues
        assert "missing address" in response.issues

    @pytest.mark.asyncio
    async def test_soft_issues_still_valid(self):
        agent = ValidationAgent()
        response = await agent.execute(
            ValidationAgentRequest(
                business={"name": "Pizza", "formatted_address": "Addr"},
                enriched={"confidence_score": 0.5},
            )
        )
        assert response.valid is True
        assert "missing email" in response.issues
        assert "missing phone" in response.issues

    @pytest.mark.asyncio
    async def test_low_confidence_flagged(self):
        agent = ValidationAgent()
        response = await agent.execute(
            ValidationAgentRequest(
                business={"name": "Pizza", "formatted_address": "Addr"},
                enriched={"confidence_score": 0.1},
            )
        )
        assert "low confidence" in response.issues

    @pytest.mark.asyncio
    async def test_logs_prefix(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        agent = ValidationAgent()
        await agent.execute(
            ValidationAgentRequest(
                business={"name": "Pizza", "formatted_address": "Addr"},
                enriched={"confidence_score": 0.9},
            )
        )
        assert any("[ValidationAgent] Validated business=Pizza" in msg for msg in caplog.messages)


class TestCoordinatorAgent:
    def test_constructor_sets_agents(self):
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)
        assert isinstance(agent.search_agent, SearchAgent)
        assert isinstance(agent.enrichment_agent, EnrichmentAgent)
        assert isinstance(agent.discovery_agent, DiscoveryAgent)
        assert isinstance(agent.validation_agent, ValidationAgent)
        assert agent.provider_name == "foursquare"
        assert agent._engine is not None
        assert agent._memory is not None
        assert agent._task_manager is not None

    def test_action_agent_map_routes_to_agents(self):
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)
        assert agent._action_agent_map[SCRAPE_WEBSITE.name] is agent.enrichment_agent
        assert agent._action_agent_map[EXTRACT_EMAIL.name] is agent.enrichment_agent
        assert agent._action_agent_map[DISCOVER_WEBSITE.name] is agent.discovery_agent
        assert agent._action_agent_map[DISCOVER_SOCIAL.name] is agent.discovery_agent
        assert agent._action_agent_map[VERIFY_WEBSITE.name] is agent.discovery_agent

    @pytest.mark.asyncio
    async def test_full_workflow_uses_agents(self):
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)

        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            result = await agent.search(
                location="New York, NY",
                business_category="restaurant",
            )

        assert result["total_results"] == 2
        leads = {lead["business_name"]: lead for lead in result["leads"]}
        assert leads["Pizza Place"]["email"] == "contact@pizzaplace.com"
        assert leads["Pizza Place"]["linkedin"] == "https://linkedin.com/company/id1"

    @pytest.mark.asyncio
    async def test_coordinator_delegates_to_enrichment_agent(self):
        search = FakeSearchService()

        class ScrapeOnlyEngine:
            async def decide(self, state):
                if state.get("email"):
                    return []
                return [SCRAPE_WEBSITE]

        agent = CoordinatorAgent(search_service=search, decision_engine=ScrapeOnlyEngine())
        spy = AsyncMock(return_value=AgentResponse(success=True, data={"email": "spy@example.com"}))
        agent.enrichment_agent.execute = spy

        business = Business(
            fsq_place_id="id1",
            name="Pizza Place",
            formatted_address="123 Main St",
            website="https://pizza.example.com",
        )
        enriched = await agent._process_single_lead(business)

        assert enriched.email == "spy@example.com"
        spy.assert_awaited()
        request = spy.await_args.args[0]
        assert request.action == "scrape_website"
        assert request.website == "https://pizza.example.com"
        assert request.business_name == "Pizza Place"

    @pytest.mark.asyncio
    async def test_coordinator_delegates_to_discovery_agent(self):
        search = FakeSearchService()

        class DiscoverSocialEngine:
            async def decide(self, state):
                return [DISCOVER_SOCIAL]

        agent = CoordinatorAgent(search_service=search, decision_engine=DiscoverSocialEngine())
        spy = AsyncMock(return_value=AgentResponse(success=True, data={"linkedin": "https://linkedin.com/company/sushi"}))
        agent.discovery_agent.execute = spy

        with patch.object(agent._scraper, "scrape", new=AsyncMock(return_value={
            "emails": [], "linkedin": None, "facebook": None, "instagram": None, "twitter": None, "youtube": None,
        })):
            business = Business(
                fsq_place_id="id1",
                name="Pizza Place",
                formatted_address="123 Main St",
                website="https://pizza.example.com",
            )
            enriched = await agent._process_single_lead(business)

        assert enriched.linkedin == "https://linkedin.com/company/sushi"
        spy.assert_awaited()
        request = spy.await_args.args[0]
        assert request.action == "discover_social"

    @pytest.mark.asyncio
    async def test_agent_action_exception_is_isolated(self):
        search = FakeSearchService()

        class ScrapeOnlyEngine:
            async def decide(self, state):
                if state.get("email"):
                    return []
                return [SCRAPE_WEBSITE]

        agent = CoordinatorAgent(search_service=search, decision_engine=ScrapeOnlyEngine())

        async def _boom(request):
            raise RuntimeError("enrichment agent exploded")

        agent.enrichment_agent.execute = _boom

        business = Business(
            fsq_place_id="id1",
            name="Pizza Place",
            formatted_address="123 Main St",
            website="https://pizza.example.com",
        )
        enriched = await agent._process_single_lead(business)

        assert enriched is not None
        assert enriched.business_name == "Pizza Place"

    @pytest.mark.asyncio
    async def test_failing_lead_does_not_stop_pipeline(self):
        search = FakeSearchService()

        class FailingEngineForSushi:
            async def decide(self, state):
                if state.get("name") == "Sushi Bar":
                    raise RuntimeError("engine exploded for sushi")
                return []

        agent = CoordinatorAgent(search_service=search, decision_engine=FailingEngineForSushi())

        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            result = await agent.search(location="NYC", business_category="food")

        assert result["total_results"] == 2
        names = {lead["business_name"] for lead in result["leads"]}
        assert names == {"Pizza Place", "Sushi Bar"}

    @pytest.mark.asyncio
    async def test_coordinator_uses_cache(self):
        search = FakeSearchService()
        cache = CacheManager(ttl_seconds=60)
        cached = EnrichedBusiness.from_business(
            Business(fsq_place_id="id1", name="Pizza Place", formatted_address="123 Main St")
        )
        cached.email = "cached@example.com"
        cache.set("id1", cached)

        agent = CoordinatorAgent(search_service=search, cache=cache)
        business = Business(
            fsq_place_id="id1",
            name="Pizza Place",
            formatted_address="123 Main St",
            website="https://pizza.example.com",
        )
        enriched = await agent._process_single_lead(business)
        assert enriched.email == "cached@example.com"

    @pytest.mark.asyncio
    async def test_validation_issues_recorded_in_memory(self):
        search = FakeSearchService()

        class SkipEngine(RuleBasedDecisionEngine):
            async def decide(self, state):
                return []

        agent = CoordinatorAgent(search_service=search, decision_engine=SkipEngine())
        with patch.object(agent._scraper, "scrape", new=AsyncMock(return_value={
            "emails": [], "linkedin": None, "facebook": None, "instagram": None, "twitter": None, "youtube": None,
        })):
            business = Business(
                fsq_place_id="id1",
                name="Pizza Place",
                formatted_address="123 Main St",
                website="https://pizza.example.com",
            )
            enriched = await agent._process_single_lead(business)
        assert enriched is not None
        issues = agent._memory.get("id1", "validation_issues")
        assert isinstance(issues, list)


class TestCoordinatorLogging:
    @pytest.mark.asyncio
    async def test_all_agent_prefixes_logged(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)

        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            await agent.search(location="NYC", business_category="food")

        messages = caplog.messages
        assert any("[Coordinator] Starting lead search" in msg for msg in messages)
        assert any("[SearchAgent] Starting search" in msg for msg in messages)
        assert any("[Coordinator] Lead search finished" in msg for msg in messages)

    @pytest.mark.asyncio
    async def test_enrichment_and_validation_prefixes_logged(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)

        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            await agent.search(location="NYC", business_category="food")

        messages = caplog.messages
        assert any("[EnrichmentAgent]" in msg for msg in messages)
        assert any("[ValidationAgent]" in msg for msg in messages)

    @pytest.mark.asyncio
    async def test_agent_failure_logged_but_pipeline_continues(self, caplog):
        import logging

        caplog.set_level(logging.WARNING)
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)

        async def _boom(request):
            raise RuntimeError("discovery exploded")

        agent.discovery_agent.execute = _boom

        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            await agent.search(location="NYC", business_category="food")

        assert any("Action discover_social failed" in msg for msg in caplog.messages)
