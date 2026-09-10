import pytest

from app.agents.decision_engine import (
    DISCOVER_SOCIAL,
    DISCOVER_WEBSITE,
    EXTRACT_EMAIL,
    SCRAPE_WEBSITE,
    SKIP_ENRICHMENT,
    Action,
    RuleBasedDecisionEngine,
)


class TestAction:
    def test_frozen(self):
        a = Action("test", priority=10)
        with pytest.raises(AttributeError):
            a.name = "changed"

    def test_equality_by_name_only(self):
        a1 = Action("test", priority=10)
        a2 = Action("test", priority=99)
        assert a1 == a2

    def test_different_names_not_equal(self):
        a1 = Action("a")
        a2 = Action("b")
        assert a1 != a2

    def test_priority_default(self):
        a = Action("test")
        assert a.priority == 100


class TestRuleBasedDecisionEngine:
    @pytest.mark.asyncio
    async def test_rule_discover_website_when_missing(self):
        engine = RuleBasedDecisionEngine()
        state = {"website": None, "email": None}
        actions = await engine.decide(state)

        assert DISCOVER_WEBSITE in actions
        assert SCRAPE_WEBSITE in actions
        assert EXTRACT_EMAIL in actions

    @pytest.mark.asyncio
    async def test_rule_no_discover_website_when_present(self):
        engine = RuleBasedDecisionEngine()
        state = {"website": "https://example.com", "email": None}
        actions = await engine.decide(state)

        assert DISCOVER_WEBSITE not in actions
        assert SCRAPE_WEBSITE in actions
        assert EXTRACT_EMAIL in actions

    @pytest.mark.asyncio
    async def test_rule_skip_email_extraction_when_email_exists(self):
        engine = RuleBasedDecisionEngine()
        state = {"website": "https://example.com", "email": "a@b.com"}
        actions = await engine.decide(state)

        assert EXTRACT_EMAIL not in actions
        assert SCRAPE_WEBSITE not in actions
        assert DISCOVER_SOCIAL in actions

    @pytest.mark.asyncio
    async def test_rule_discover_social_when_website_exists(self):
        engine = RuleBasedDecisionEngine()
        state = {"website": "https://example.com", "email": None}
        actions = await engine.decide(state)

        assert DISCOVER_SOCIAL in actions

    @pytest.mark.asyncio
    async def test_rule_discover_social_when_website_missing(self):
        engine = RuleBasedDecisionEngine()
        state = {"website": None, "email": None}
        actions = await engine.decide(state)

        assert DISCOVER_SOCIAL not in actions

    @pytest.mark.asyncio
    async def test_rule_skip_enrichment_when_all_present(self):
        engine = RuleBasedDecisionEngine()
        state = {"website": "https://example.com", "email": "a@b.com"}
        actions = await engine.decide(state)

        assert SKIP_ENRICHMENT not in actions
        assert DISCOVER_SOCIAL in actions

    @pytest.mark.asyncio
    async def test_no_skip_enrichment_when_actions_needed(self):
        engine = RuleBasedDecisionEngine()
        state = {"website": None, "email": None}
        actions = await engine.decide(state)

        assert SKIP_ENRICHMENT not in actions

    @pytest.mark.asyncio
    async def test_empty_state(self):
        engine = RuleBasedDecisionEngine()
        actions = await engine.decide({})

        assert DISCOVER_WEBSITE in actions
        assert SCRAPE_WEBSITE in actions
        assert EXTRACT_EMAIL in actions

    @pytest.mark.asyncio
    async def test_action_priority_order(self):
        engine = RuleBasedDecisionEngine()

        state_no_website = {"website": None, "email": None}
        actions = await engine.decide(state_no_website)
        priorities = [a.priority for a in actions]
        assert priorities == sorted(priorities)

        state_with_website = {"website": "https://x.com", "email": None}
        actions = await engine.decide(state_with_website)
        priorities = [a.priority for a in actions]
        assert priorities == sorted(priorities)

    @pytest.mark.asyncio
    async def test_logging(self, caplog):
        import logging
        caplog.set_level(logging.INFO)

        engine = RuleBasedDecisionEngine()
        await engine.decide({"name": "Pizza Place", "website": None, "email": None})

        assert any("[Decision]" in msg for msg in caplog.messages)
        assert any("Pizza Place" in msg for msg in caplog.messages)
        assert any("discover_website" in msg for msg in caplog.messages)


class TestBaseDecisionEngine:
    def test_abc_cannot_instantiate(self):
        from app.agents.decision_engine import BaseDecisionEngine
        with pytest.raises(TypeError):
            BaseDecisionEngine()

    @pytest.mark.asyncio
    async def test_can_extend(self):
        from app.agents.decision_engine import Action, BaseDecisionEngine

        class CustomEngine(BaseDecisionEngine):
            async def decide(self, state):
                return [Action("custom")]

        engine = CustomEngine()
        actions = await engine.decide({})
        assert len(actions) == 1
        assert actions[0].name == "custom"
