from unittest.mock import AsyncMock, patch

import pytest

from app.agents.decision_engine import (
    DISCOVER_WEBSITE,
    EXTRACT_EMAIL,
    SCRAPE_WEBSITE,
    Action,
    RuleBasedDecisionEngine,
)
from app.agents.gemini_decision_engine import GeminiDecisionEngine
from app.llm.gemini_client import (
    GeminiClient,
    GeminiError,
    GeminiInvalidResponseError,
)


class FakeGeminiClient:
    def __init__(self, result=None, error=None, configured=True):
        self.result = result
        self.error = error
        self.is_configured = configured
        self.calls = []

    async def generate(self, prompt, system_prompt=None, **kwargs):
        self.calls.append((prompt, system_prompt))
        if self.error:
            raise self.error
        return self.result


class TestGeminiDecisionEngine:
    @pytest.mark.asyncio
    async def test_success_returns_actions(self):
        client = FakeGeminiClient(result={"actions": ["scrape_website", "extract_email"], "reasoning": "x"})
        engine = GeminiDecisionEngine(client=client)

        actions = await engine.decide({"website": "https://x.com", "email": None})

        assert len(actions) == 2
        assert actions[0].name == "scrape_website"
        assert actions[1].name == "extract_email"
        assert actions[0].priority < actions[1].priority

    @pytest.mark.asyncio
    async def test_success_returns_single_action(self):
        client = FakeGeminiClient(result={"actions": ["discover_website"], "reasoning": "no website"})
        engine = GeminiDecisionEngine(client=client)

        actions = await engine.decide({"website": None, "email": None})

        assert actions == [DISCOVER_WEBSITE]

    @pytest.mark.asyncio
    async def test_empty_actions_returns_empty_list(self):
        client = FakeGeminiClient(result={"actions": [], "reasoning": "nothing needed"})
        engine = GeminiDecisionEngine(client=client)

        actions = await engine.decide({"website": "https://x.com", "email": "a@b.com"})

        assert actions == []

    @pytest.mark.asyncio
    async def test_invalid_json_falls_back(self):
        client = FakeGeminiClient(error=GeminiInvalidResponseError("bad json"))
        fallback = RuleBasedDecisionEngine()
        engine = GeminiDecisionEngine(client=client, fallback_engine=fallback)

        actions = await engine.decide({"website": None, "email": None})

        assert DISCOVER_WEBSITE in actions
        assert SCRAPE_WEBSITE in actions

    @pytest.mark.asyncio
    async def test_timeout_falls_back(self):
        client = FakeGeminiClient(error=GeminiError("timed out"))
        engine = GeminiDecisionEngine(client=client)

        actions = await engine.decide({"website": "https://x.com", "email": None})

        assert SCRAPE_WEBSITE in actions
        assert EXTRACT_EMAIL in actions

    @pytest.mark.asyncio
    async def test_unconfigured_client_falls_back(self):
        client = FakeGeminiClient(configured=False)
        engine = GeminiDecisionEngine(client=client)

        actions = await engine.decide({"website": None, "email": None})

        assert DISCOVER_WEBSITE in actions

    @pytest.mark.asyncio
    async def test_unknown_actions_ignored(self):
        client = FakeGeminiClient(result={"actions": ["scrape_website", "not_a_real_action"], "reasoning": "x"})
        engine = GeminiDecisionEngine(client=client)

        actions = await engine.decide({"website": "https://x.com", "email": None})

        assert [a.name for a in actions] == ["scrape_website"]

    @pytest.mark.asyncio
    async def test_duplicate_actions_deduplicated(self):
        client = FakeGeminiClient(result={"actions": ["scrape_website", "scrape_website", "extract_email"], "reasoning": "x"})
        engine = GeminiDecisionEngine(client=client)

        actions = await engine.decide({"website": "https://x.com", "email": None})

        assert len(actions) == 2
        assert [a.name for a in actions] == ["scrape_website", "extract_email"]

    @pytest.mark.asyncio
    async def test_actions_sorted_by_priority(self):
        client = FakeGeminiClient(result={"actions": ["extract_email", "discover_social", "scrape_website"], "reasoning": "x"})
        engine = GeminiDecisionEngine(client=client)

        actions = await engine.decide({"website": "https://x.com", "email": None})

        names = [a.name for a in actions]
        assert names == ["scrape_website", "extract_email", "discover_social"]

    @pytest.mark.asyncio
    async def test_fallback_engine_custom(self):
        class CustomFallback:
            async def decide(self, state):
                return [Action("custom_action", priority=5)]

        client = FakeGeminiClient(error=GeminiError("boom"))
        engine = GeminiDecisionEngine(client=client, fallback_engine=CustomFallback())

        actions = await engine.decide({})

        assert actions == [Action("custom_action", priority=5)]

    @pytest.mark.asyncio
    async def test_prompt_contains_state(self):
        client = FakeGeminiClient(result={"actions": ["discover_website"], "reasoning": "x"})
        engine = GeminiDecisionEngine(client=client)

        await engine.decide({
            "name": "Pizza Place",
            "website": None,
            "email": None,
        })

        assert len(client.calls) == 1
        prompt, system_prompt = client.calls[0]
        assert "Pizza Place" in prompt
        assert "discover_website" in system_prompt
        assert "scrape_website" in system_prompt
        assert "extract_email" in system_prompt
        assert "discover_social" in system_prompt
        assert "verify_website" in system_prompt

    @pytest.mark.asyncio
    async def test_uses_real_client_by_default(self):
        engine = GeminiDecisionEngine()
        assert isinstance(engine._client, GeminiClient)
        assert isinstance(engine._fallback, RuleBasedDecisionEngine)

    @pytest.mark.asyncio
    async def test_logs_llm_messages(self, caplog):
        import logging
        caplog.set_level(logging.INFO)

        client = FakeGeminiClient(result={"actions": ["discover_social"], "reasoning": "x"})
        engine = GeminiDecisionEngine(client=client)

        await engine.decide({"website": "https://x.com", "email": "a@b.com"})

        assert any("[LLM] Prompt sent" in msg for msg in caplog.messages)
        assert any("[LLM] Response received" in msg for msg in caplog.messages)
        assert any("[LLM] Parsed actions" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_logs_fallback_message(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)

        client = FakeGeminiClient(error=GeminiError("boom"))
        engine = GeminiDecisionEngine(client=client)

        await engine.decide({"website": None, "email": None})

        assert any("Falling back to RuleBasedDecisionEngine" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_agent_integration_with_gemini(self):
        from app.agents.lead_agent import LeadAgent

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
                        "categories": ["pizza"],
                        "latitude": 40.7128,
                        "longitude": -74.0060,
                    }
                ]

        search = FakeSearchService()
        client = FakeGeminiClient(result={"actions": ["discover_social"], "reasoning": "x"})
        engine = GeminiDecisionEngine(client=client)

        agent = LeadAgent(
            search_service=search,
            decision_engine=engine,
        )

        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value={
                "emails": ["contact@pizzaplace.com"],
                "linkedin": "https://linkedin.com/company/id1",
                "facebook": None, "instagram": None, "twitter": None, "youtube": None,
            })),
        ):
            result = await agent.search(
                location="New York, NY",
                business_category="restaurant",
            )

        assert result["total_results"] == 1
        lead = result["leads"][0]
        assert lead["business_name"] == "Pizza Place"
        assert len(client.calls) >= 1
