from unittest.mock import AsyncMock, patch

import pytest

from app.agents.decision_engine import RuleBasedDecisionEngine
from app.agents.lead_agent import LeadAgent
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

    async def get_business_details(self, place_id):
        return {"fsq_place_id": place_id, "name": "Test", "formatted_address": "Addr"}


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


class TestLeadAgent:
    def test_constructor(self):
        search = FakeSearchService()
        agent = LeadAgent(search_service=search)
        assert agent.provider_name == "foursquare"
        assert agent._engine is not None
        assert agent._memory is not None
        assert agent._task_manager is not None

    def test_constructor_with_explicit_services(self):
        search = FakeSearchService()
        engine = RuleBasedDecisionEngine()
        agent = LeadAgent(search_service=search, decision_engine=engine)
        assert agent.provider_name == "foursquare"

    def test_dict_to_business(self):
        search = FakeSearchService()
        agent = LeadAgent(search_service=search)
        d = {
            "fsq_place_id": "id1",
            "name": "Test",
            "formatted_address": "Addr",
            "phone_number": "555",
            "website": "https://test.com",
            "rating": 5.0,
            "categories": ["food"],
            "latitude": 40.0,
            "longitude": -74.0,
            "opening_hours": ["9-5"],
            "price_level": 2,
        }
        biz = agent._dict_to_business(d)
        assert biz.fsq_place_id == "id1"
        assert biz.name == "Test"
        assert biz.categories == ["food"]
        assert biz.latitude == 40.0

    @pytest.mark.asyncio
    async def test_search_success(self):
        search = FakeSearchService()
        agent = LeadAgent(search_service=search)

        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            result = await agent.search(
                location="New York, NY",
                business_category="restaurant",
                radius=5000,
                max_results=10,
            )

        assert result["total_results"] == 2
        assert len(result["leads"]) == 2
        assert result["search_location"] == "New York, NY"
        assert result["search_category"] == "restaurant"
        assert result["search_radius"] == 5000
        assert result["processing_time_ms"] >= 0

        leads = result["leads"]
        assert leads[0]["confidence_score"] >= leads[1]["confidence_score"]

        first = leads[0]
        assert first["business_name"] == "Pizza Place"
        assert first["email"] == "contact@pizzaplace.com"
        assert first["linkedin"] is not None

    @pytest.mark.asyncio
    async def test_search_sorted_by_confidence(self):
        search = FakeSearchService()

        class LowHighEngine:
            def __init__(self):
                self.call_count = 0

            async def decide(self, state):
                self.call_count += 1
                return []

        agent = LeadAgent(search_service=search, decision_engine=LowHighEngine())

        with patch.object(agent._scraper, "scrape", new=AsyncMock(return_value={
            "emails": [], "linkedin": None, "facebook": None, "instagram": None, "twitter": None, "youtube": None,
        })):
            result = await agent.search(
                location="NYC",
                business_category="food",
            )

        leads = result["leads"]
        assert leads[0]["confidence_score"] >= leads[1]["confidence_score"]

    @pytest.mark.asyncio
    async def test_search_partial_failure(self):
        search = FakeSearchService()

        agent = LeadAgent(search_service=search)

        scraped_ok = dict(FAKE_SCRAPE)
        scraped_fail: dict = {"emails": [], "linkedin": None, "facebook": None, "instagram": None, "twitter": None, "youtube": None}

        side_effects = [scraped_ok, scraped_fail]

        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(side_effect=side_effects)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            result = await agent.search(
                location="New York, NY",
                business_category="restaurant",
            )

        assert result["total_results"] == 2
        leads = result["leads"]

        id1_lead = next(lead for lead in leads if lead["business_name"] == "Pizza Place")
        assert id1_lead["email"] == "contact@pizzaplace.com"

        id2_lead = next(lead for lead in leads if lead["business_name"] == "Sushi Bar")
        assert id2_lead["email"] is None

    @pytest.mark.asyncio
    async def test_search_empty_results(self):
        class EmptySearchService:
            def __init__(self):
                self.provider = FakeProvider()

            async def search(self, **kwargs):
                return []

        search = EmptySearchService()
        agent = LeadAgent(search_service=search)

        result = await agent.search(
            location="Nowhere",
            business_category="nothing",
        )

        assert result["total_results"] == 0
        assert len(result["leads"]) == 0

    @pytest.mark.asyncio
    async def test_search_logs_decision(self, caplog):
        import logging
        caplog.set_level(logging.INFO)

        search = FakeSearchService()
        agent = LeadAgent(search_service=search)

        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            await agent.search(location="NYC", business_category="food")

        assert any("[Decision]" in msg for msg in caplog.messages)
        assert any("[Coordinator] Starting lead search" in msg for msg in caplog.messages)
        assert any("[SearchAgent] Starting search" in msg for msg in caplog.messages)
        assert any("[Coordinator] Search complete" in msg for msg in caplog.messages)
        assert any("[Coordinator] Processing lead" in msg for msg in caplog.messages)
        assert any("[Coordinator] Lead enriched" in msg for msg in caplog.messages)
        assert any("[Coordinator] Lead search finished" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_search_logs_action_failure(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)

        search = FakeSearchService()
        agent = LeadAgent(search_service=search)

        with patch.object(agent._scraper, "scrape", new=AsyncMock(side_effect=RuntimeError("Scrape failed"))):
            await agent.search(location="NYC", business_category="food")

        tool_msgs = [m for m in caplog.messages if "scrape failed" in m.lower()]
        assert len(tool_msgs) > 0

    @pytest.mark.asyncio
    async def test_agent_search_response_schema(self):
        from app.schemas.requests import (
            AgentSearchRequest,
            AgentSearchResponse,
            EnrichedBusinessResponse,
        )

        req = AgentSearchRequest(
            location="NYC",
            business_category="restaurant",
            radius=5000,
            max_results=20,
        )
        assert req.location == "NYC"
        assert req.business_category == "restaurant"

        lead = EnrichedBusinessResponse(
            business_name="Test",
            address="Addr",
            phone="555",
            website="https://test.com",
            email="a@b.com",
            linkedin="https://linkedin.com/co/test",
            confidence_score=0.85,
            source="test",
        )

        resp = AgentSearchResponse(
            leads=[lead],
            total_results=1,
            search_location="NYC",
            search_category="restaurant",
            search_radius=5000,
            processing_time_ms=123,
        )
        assert resp.total_results == 1
        assert resp.processing_time_ms == 123
        assert resp.leads[0].business_name == "Test"

    @pytest.mark.asyncio
    async def test_discover_website_action_executed(self):
        search = FakeSearchService()

        class DiscoverOnlyEngine:
            async def decide(self, state):
                from app.agents.decision_engine import DISCOVER_WEBSITE
                return [DISCOVER_WEBSITE]

        agent = LeadAgent(search_service=search, decision_engine=DiscoverOnlyEngine())

        result = await agent._discovery.discover(
            business_name="Sushi Bar",
            category="sushi",
            location="456 Oak Ave",
            existing_website=None,
        )
        assert result.website is None

        business = Business(
            fsq_place_id="id2",
            name="Sushi Bar",
            formatted_address="456 Oak Ave",
            categories=["sushi"],
        )
        enriched = await agent._process_single_lead(business)
        assert enriched is not None
        assert enriched.business_name == "Sushi Bar"

    @pytest.mark.asyncio
    async def test_scrape_website_action_executed(self):
        search = FakeSearchService()

        class ScrapeOnlyEngine:
            async def decide(self, state):
                from app.agents.decision_engine import SCRAPE_WEBSITE
                if state.get("email"):
                    return []
                return [SCRAPE_WEBSITE]

        agent = LeadAgent(search_service=search, decision_engine=ScrapeOnlyEngine())

        with patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)):
            business = Business(
                fsq_place_id="id1",
                name="Pizza Place",
                formatted_address="123 Main St",
                website="https://pizza.example.com",
            )
            enriched = await agent._process_single_lead(business)

        assert enriched is not None
        assert enriched.email == "contact@pizzaplace.com"
        assert enriched.linkedin == "https://linkedin.com/company/id1"

    @pytest.mark.asyncio
    async def test_search_runs_leads_in_parallel(self):
        import asyncio

        active = 0
        max_active = 0
        lock = asyncio.Lock()

        async def tracked_scrape(url):
            nonlocal active, max_active
            async with lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            async with lock:
                active -= 1
            return dict(FAKE_SCRAPE)

        class FakeSearchWithWebsites(FakeSearchService):
            async def search(self, **kwargs):
                results = await super().search(**kwargs)
                for biz in results:
                    biz["website"] = biz["website"] or f"https://{biz['fsq_place_id']}.example.com"
                return results

        agent = LeadAgent(search_service=FakeSearchWithWebsites())

        with (
            patch.object(agent._scraper, "scrape", new=tracked_scrape),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            result = await agent.search(
                location="New York, NY",
                business_category="restaurant",
            )

        assert result["total_results"] == 2
        assert max_active > 1
        assert result["processing_time_ms"] >= 0

    @pytest.mark.asyncio
    async def test_process_single_lead_checks_cache_before_enrichment(self):
        search = FakeSearchService()
        cache = CacheManager(ttl_seconds=60)
        cached_lead = EnrichedBusiness.from_business(
            Business(
                fsq_place_id="id1",
                name="Pizza Place",
                formatted_address="123 Main St",
            )
        )
        cached_lead.email = "cached@example.com"
        cache.set("id1", cached_lead)

        agent = LeadAgent(search_service=search, cache=cache)

        business = Business(
            fsq_place_id="id1",
            name="Pizza Place",
            formatted_address="123 Main St",
            website="https://pizza.example.com",
        )
        enriched = await agent._process_single_lead(business)

        assert enriched is not None
        assert enriched.email == "cached@example.com"

    @pytest.mark.asyncio
    async def test_process_single_lead_stores_result_in_cache(self):
        search = FakeSearchService()
        cache = CacheManager(ttl_seconds=60)
        agent = LeadAgent(search_service=search, cache=cache)

        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            business = Business(
                fsq_place_id="id1",
                name="Pizza Place",
                formatted_address="123 Main St",
                website="https://pizza.example.com",
            )
            enriched = await agent._process_single_lead(business)

        assert enriched is not None
        cached = cache.get("id1")
        assert cached is not None
        assert cached.email == "contact@pizzaplace.com"
        assert cached.linkedin == "https://linkedin.com/company/id1"

    @pytest.mark.asyncio
    async def test_search_skips_enrichment_for_cached_leads(self):
        search = FakeSearchService()
        cache = CacheManager(ttl_seconds=60)

        for fid, name in [("id1", "Pizza Place"), ("id2", "Sushi Bar")]:
            cached = EnrichedBusiness.from_business(
                Business(
                    fsq_place_id=fid,
                    name=name,
                    formatted_address="Somewhere",
                )
            )
            cached.email = f"{fid}@cached.example.com"
            cache.set(fid, cached)

        agent = LeadAgent(search_service=search, cache=cache)

        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(side_effect=RuntimeError("should not run"))),
            patch.object(agent._discovery, "discover", new=AsyncMock(side_effect=RuntimeError("should not run"))),
        ):
            result = await agent.search(
                location="New York, NY",
                business_category="restaurant",
            )

        assert result["total_results"] == 2
        emails = {lead["business_name"]: lead["email"] for lead in result["leads"]}
        assert emails["Pizza Place"] == "id1@cached.example.com"
        assert emails["Sushi Bar"] == "id2@cached.example.com"

    @pytest.mark.asyncio
    async def test_search_uses_cache_on_second_call(self):
        search = FakeSearchService()
        cache = CacheManager(ttl_seconds=60)
        agent = LeadAgent(search_service=search, cache=cache)

        scrape_calls = 0

        async def counting_scrape(url):
            nonlocal scrape_calls
            scrape_calls += 1
            return dict(FAKE_SCRAPE)

        with (
            patch.object(agent._scraper, "scrape", new=counting_scrape),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            first = await agent.search(location="NYC", business_category="food")
            first_calls = scrape_calls

            second = await agent.search(location="NYC", business_category="food")
            second_calls = scrape_calls

        assert first["total_results"] == 2
        assert second["total_results"] == 2
        assert first_calls > 0
        assert second_calls == first_calls
