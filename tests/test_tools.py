from unittest.mock import AsyncMock, patch

import pytest

from app.providers.base import Business
from app.services.lead_discovery_service import (
    DiscoveredField,
    DiscoveryResult,
)
from app.tools.base_tool import BaseTool, ToolResult
from app.tools.discovery_tool import DiscoveryTool
from app.tools.enrichment_tool import EnrichmentTool
from app.tools.search_tool import SearchTool


class TestToolResult:
    def test_defaults(self):
        r = ToolResult()
        assert r.success is True
        assert r.data == {}
        assert r.error is None

    def test_full(self):
        r = ToolResult(success=False, data={"x": 1}, error="something broke")
        assert r.success is False
        assert r.data == {"x": 1}
        assert r.error == "something broke"


class TestBaseTool:
    def test_abc_cannot_instantiate(self):
        with pytest.raises(TypeError):
            BaseTool()

    @pytest.mark.asyncio
    async def test_can_extend(self):
        class CustomTool(BaseTool):
            name = "custom"

            async def execute(self, action, business=None, **kwargs):
                return ToolResult(success=True, data={"result": "ok"})

        tool = CustomTool()
        assert tool.name == "custom"
        assert tool.supported_actions == ["custom"]

        result = await tool.execute("custom")
        assert result.success is True
        assert result.data == {"result": "ok"}


class FakeProvider:
    provider_name = "foursquare"


class FakeSearchService:
    def __init__(self):
        self.provider = FakeProvider()

    async def search(self, location="", business_category="", radius=5000, max_results=20):
        return [
            {"fsq_place_id": "id1", "name": "Pizza Place", "formatted_address": "123 Main St"},
        ]


class TestSearchTool:
    @pytest.mark.asyncio
    async def test_execute_success(self):
        tool = SearchTool(search_service=FakeSearchService())
        result = await tool.execute(
            "search", None,
            location="NYC",
            business_category="restaurant",
            radius=5000,
            max_results=10,
        )

        assert result.success is True
        assert "businesses" in result.data
        assert len(result.data["businesses"]) == 1
        assert result.data["businesses"][0]["name"] == "Pizza Place"

    @pytest.mark.asyncio
    async def test_execute_failure(self):
        class FailingSearchService:
            def __init__(self):
                self.provider = FakeProvider()

            async def search(self, **kwargs):
                raise RuntimeError("API down")

        tool = SearchTool(search_service=FailingSearchService())
        result = await tool.execute("search", None, location="NYC", business_category="food")

        assert result.success is False
        assert "API down" in result.error

    def test_name(self):
        tool = SearchTool(search_service=FakeSearchService())
        assert tool.name == "search"
        assert tool.supported_actions == ["search"]


class TestEnrichmentTool:
    @pytest.mark.asyncio
    async def test_scrape_success(self):
        tool = EnrichmentTool()
        scraped_data = {
            "emails": ["contact@test.com"],
            "linkedin": "https://linkedin.com/co/test",
            "facebook": None,
            "instagram": None,
            "twitter": None,
            "youtube": None,
        }

        with patch.object(tool._scraper, "scrape", new=AsyncMock(return_value=scraped_data)):
            business = Business(
                fsq_place_id="id1", name="Test", formatted_address="Addr",
                website="https://test.com",
            )
            result = await tool.execute("scrape_website", business)

        assert result.success is True
        assert result.data["email"] == "contact@test.com"
        assert result.data["linkedin"] == "https://linkedin.com/co/test"

    @pytest.mark.asyncio
    async def test_scrape_no_website(self):
        tool = EnrichmentTool()
        business = Business(fsq_place_id="id1", name="Test", formatted_address="Addr")
        result = await tool.execute("scrape_website", business)

        assert result.success is True
        assert result.data == {}

    @pytest.mark.asyncio
    async def test_scrape_via_kwargs_website(self):
        tool = EnrichmentTool()
        scraped_data = {"emails": ["a@b.com"], "linkedin": None, "facebook": None, "instagram": None, "twitter": None, "youtube": None}

        with patch.object(tool._scraper, "scrape", new=AsyncMock(return_value=scraped_data)):
            result = await tool.execute("scrape_website", None, website="https://example.com")

        assert result.success is True
        assert result.data["email"] == "a@b.com"

    @pytest.mark.asyncio
    async def test_scrape_failure(self):
        tool = EnrichmentTool()

        with patch.object(tool._scraper, "scrape", new=AsyncMock(side_effect=RuntimeError("timeout"))):
            business = Business(
                fsq_place_id="id1", name="Test", formatted_address="Addr",
                website="https://test.com",
            )
            result = await tool.execute("scrape_website", business)

        assert result.success is False
        assert "timeout" in result.error

    @pytest.mark.asyncio
    async def test_extract_email_success(self):
        tool = EnrichmentTool()
        scraped_data = {"emails": ["contact@test.com"]}

        with patch.object(tool._scraper, "scrape", new=AsyncMock(return_value=scraped_data)):
            result = await tool.execute("extract_email", None, website="https://test.com")

        assert result.success is True
        assert result.data["email"] == "contact@test.com"

    @pytest.mark.asyncio
    async def test_extract_email_no_emails(self):
        tool = EnrichmentTool()
        scraped_data = {"emails": []}

        with patch.object(tool._scraper, "scrape", new=AsyncMock(return_value=scraped_data)):
            result = await tool.execute("extract_email", None, website="https://test.com")

        assert result.success is True
        assert result.data == {}

    @pytest.mark.asyncio
    async def test_unsupported_action(self):
        tool = EnrichmentTool()
        result = await tool.execute("unknown", None)
        assert result.success is False
        assert "Unsupported action" in result.error

    def test_name(self):
        tool = EnrichmentTool()
        assert tool.name == "enrichment"
        assert tool.supported_actions == ["scrape_website", "extract_email"]


class TestDiscoveryTool:
    @pytest.mark.asyncio
    async def test_discover_website_success(self):
        tool = DiscoveryTool()
        expected = DiscoveryResult(
            website=DiscoveredField(
                value="https://found.com", confidence=0.9,
                source="test", method="test",
            ),
        )

        with patch.object(tool._discovery, "discover", new=AsyncMock(return_value=expected)):
            business = Business(
                fsq_place_id="id1", name="Unknown Biz",
                formatted_address="Addr", categories=["food"],
            )
            result = await tool.execute("discover_website", business)

        assert result.success is True
        assert result.data["website"] == "https://found.com"

    @pytest.mark.asyncio
    async def test_discover_website_no_result(self):
        tool = DiscoveryTool()

        with patch.object(tool._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())):
            business = Business(
                fsq_place_id="id1", name="Unknown Biz",
                formatted_address="Addr",
            )
            result = await tool.execute("discover_website", business)

        assert result.success is True
        assert result.data == {}

    @pytest.mark.asyncio
    async def test_discover_website_failure(self):
        tool = DiscoveryTool()

        with patch.object(tool._discovery, "discover", new=AsyncMock(side_effect=RuntimeError("API error"))):
            business = Business(
                fsq_place_id="id1", name="Test", formatted_address="Addr",
            )
            result = await tool.execute("discover_website", business)

        assert result.success is False
        assert "API error" in result.error

    @pytest.mark.asyncio
    async def test_discover_social_success(self):
        tool = DiscoveryTool()
        expected = DiscoveryResult(
            linkedin=DiscoveredField(value="https://linkedin.com/co/x", confidence=0.7, source="test", method="test"),
            facebook=DiscoveredField(value="https://facebook.com/x", confidence=0.7, source="test", method="test"),
        )

        with patch.object(tool._discovery, "discover", new=AsyncMock(return_value=expected)):
            business = Business(
                fsq_place_id="id1", name="Test", formatted_address="Addr",
                website="https://test.com", categories=["tech"],
            )
            result = await tool.execute("discover_social", business)

        assert result.success is True
        assert result.data["linkedin"] == "https://linkedin.com/co/x"
        assert result.data["facebook"] == "https://facebook.com/x"

    @pytest.mark.asyncio
    async def test_discover_social_no_website(self):
        tool = DiscoveryTool()
        business = Business(fsq_place_id="id1", name="Test", formatted_address="Addr")
        result = await tool.execute("discover_social", business)

        assert result.success is True
        assert result.data == {}

    @pytest.mark.asyncio
    async def test_verify_website_success(self):
        tool = DiscoveryTool()

        with patch.object(tool._discovery, "_verify_website", new=AsyncMock(
            return_value=DiscoveredField(value="https://verified.com", confidence=0.9, source="test", method="test"),
        )):
            business = Business(
                fsq_place_id="id1", name="Test", formatted_address="Addr",
                website="https://verified.com",
            )
            result = await tool.execute("verify_website", business)

        assert result.success is True
        assert result.data["website"] == "https://verified.com"

    @pytest.mark.asyncio
    async def test_verify_website_no_url(self):
        tool = DiscoveryTool()
        business = Business(fsq_place_id="id1", name="Test", formatted_address="Addr")
        result = await tool.execute("verify_website", business)

        assert result.success is True
        assert result.data == {}

    @pytest.mark.asyncio
    async def test_unsupported_action(self):
        tool = DiscoveryTool()
        result = await tool.execute("unknown", None)
        assert result.success is False
        assert "Unsupported action" in result.error

    def test_name(self):
        tool = DiscoveryTool()
        assert tool.name == "discovery"
        assert tool.supported_actions == ["discover_website", "discover_social", "verify_website"]


class TestToolLogging:
    @pytest.mark.asyncio
    async def test_search_tool_logs_prefix(self, caplog):
        import logging
        caplog.set_level(logging.INFO)

        tool = SearchTool(search_service=FakeSearchService())
        await tool.execute("search", None, location="NYC", business_category="food")

        assert any("[Tool]" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_enrichment_tool_logs_prefix(self, caplog):
        import logging
        caplog.set_level(logging.INFO)

        tool = EnrichmentTool()
        scraped_data = {"emails": ["a@b.com"], "linkedin": None, "facebook": None, "instagram": None, "twitter": None, "youtube": None}

        with patch.object(tool._scraper, "scrape", new=AsyncMock(return_value=scraped_data)):
            await tool.execute("scrape_website", None, website="https://test.com")

        assert any("[Tool]" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_discovery_tool_logs_prefix(self, caplog):
        import logging
        caplog.set_level(logging.INFO)

        tool = DiscoveryTool()

        with patch.object(tool._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())):
            business = Business(
                fsq_place_id="id1", name="Test", formatted_address="Addr",
            )
            await tool.execute("discover_website", business)

        assert any("[Tool]" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_tool_supports_agent_injection(self):
        class HunterIoTool(BaseTool):
            name = "hunterio"

            @property
            def supported_actions(self):
                return ["find_email"]

            async def execute(self, action, business=None, **kwargs):
                return ToolResult(success=True, data={"email": "found@company.com"})

        from app.agents.lead_agent import LeadAgent

        search = FakeSearchService()
        custom_tools = [
            SearchTool(search),
            HunterIoTool(),
        ]

        from app.agents.decision_engine import RuleBasedDecisionEngine

        class EmailOnlyEngine(RuleBasedDecisionEngine):
            async def decide(self, state):
                if not state.get("email"):
                    return [type("Action", (), {"name": "find_email", "priority": 10})()]
                return []

        agent = LeadAgent(
            search_service=search,
            decision_engine=EmailOnlyEngine(),
            tools=custom_tools,
        )

        assert "find_email" in agent._action_tool_map
        tool = agent._action_tool_map["find_email"]
        assert tool.name == "hunterio"
