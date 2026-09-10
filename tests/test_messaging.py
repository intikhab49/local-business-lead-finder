import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.base_agent import BaseAgent, track_state
from app.agents.context import AgentContext
from app.agents.coordinator_agent import CoordinatorAgent
from app.agents.decision_engine import RuleBasedDecisionEngine
from app.agents.discovery_agent import DiscoveryAgent
from app.agents.enrichment_agent import EnrichmentAgent
from app.agents.message_bus import (
    BaseMessageBus,
    InMemoryMessageBus,
    create_message_bus,
)
from app.agents.messaging import (
    AgentMessage,
    AgentState,
    MessageStatus,
    MessageType,
)
from app.agents.models import (
    ValidationAgentRequest,
)
from app.agents.search_agent import SearchAgent
from app.agents.validation_agent import ValidationAgent
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


class FailingSearchService:
    provider = FakeProvider()

    async def search(self, **kwargs):
        raise RuntimeError("boom")


class TrackedAgent(BaseAgent):
    name = "TrackedAgent"

    @track_state
    async def execute(self, request):
        raise RuntimeError("boom")


def discovery_with_website(url):
    return DiscoveryResult(
        website=DiscoveredField(value=url, confidence=0.9, source="test", method="test"),
        verification_notes=["discovered via test"],
    )


class TestAgentMessage:
    def test_defaults(self):
        msg = AgentMessage(sender="a", recipient="b", message_type=MessageType.REQUEST)
        assert msg.message_id
        assert msg.timestamp > 0
        assert msg.payload == {}
        assert msg.correlation_id is None
        assert msg.status == MessageStatus.PENDING.value

    def test_to_dict(self):
        msg = AgentMessage(
            sender="a",
            recipient="b",
            message_type=MessageType.FEEDBACK,
            payload={"k": "v"},
            correlation_id="corr-1",
        )
        data = msg.to_dict()
        assert data["sender"] == "a"
        assert data["recipient"] == "b"
        assert data["message_type"] == "feedback"
        assert data["payload"] == {"k": "v"}
        assert data["correlation_id"] == "corr-1"
        assert data["status"] == "pending"

    def test_unique_message_ids(self):
        msg1 = AgentMessage(sender="a", recipient="b", message_type=MessageType.EVENT)
        msg2 = AgentMessage(sender="a", recipient="b", message_type=MessageType.EVENT)
        assert msg1.message_id != msg2.message_id

    def test_message_type_values(self):
        assert MessageType.REQUEST.value == "request"
        assert MessageType.RESPONSE.value == "response"
        assert MessageType.EVENT.value == "event"
        assert MessageType.FEEDBACK.value == "feedback"
        assert MessageType.ERROR.value == "error"

    def test_message_status_values(self):
        assert MessageStatus.PENDING.value == "pending"
        assert MessageStatus.DELIVERED.value == "delivered"
        assert MessageStatus.FAILED.value == "failed"

    def test_agent_state_values(self):
        assert AgentState.IDLE.value == "idle"
        assert AgentState.BUSY.value == "busy"
        assert AgentState.WAITING.value == "waiting"
        assert AgentState.COMPLETE.value == "complete"
        assert AgentState.FAILED.value == "failed"


class TestAgentContext:
    def test_set_and_get_shared(self):
        ctx = AgentContext()
        ctx.set_shared("key", "value")
        assert ctx.get_shared("key") == "value"
        assert ctx.get_shared("missing", "default") == "default"

    def test_update_shared(self):
        ctx = AgentContext()
        ctx.update_shared(a=1, b=2)
        assert ctx.get_shared("a") == 1
        assert ctx.get_shared("b") == 2
        assert ctx.get_shared_data() == {"a": 1, "b": 2}

    def test_states_default_to_idle(self):
        ctx = AgentContext()
        assert ctx.get_state("UnknownAgent") == AgentState.IDLE

    def test_set_and_get_state(self):
        ctx = AgentContext()
        ctx.set_state("SearchAgent", AgentState.BUSY)
        assert ctx.get_state("SearchAgent") == AgentState.BUSY
        assert ctx.get_states() == {"SearchAgent": "busy"}

    def test_append_and_get_conversation(self):
        ctx = AgentContext()
        msg = AgentMessage(sender="a", recipient="b", message_type=MessageType.REQUEST)
        ctx.append_message(msg)
        assert ctx.conversation_size() == 1
        assert ctx.get_conversation()[0].sender == "a"

    def test_get_conversation_for(self):
        ctx = AgentContext()
        ctx.append_message(AgentMessage(sender="a", recipient="b", message_type=MessageType.REQUEST))
        ctx.append_message(AgentMessage(sender="c", recipient="d", message_type=MessageType.EVENT))
        assert len(ctx.get_conversation_for("a")) == 1
        assert len(ctx.get_conversation_for("b")) == 1
        assert len(ctx.get_conversation_for("z")) == 0

    def test_clear_conversation(self):
        ctx = AgentContext()
        ctx.append_message(AgentMessage(sender="a", recipient="b", message_type=MessageType.EVENT))
        ctx.clear_conversation()
        assert ctx.conversation_size() == 0

    def test_clear(self):
        ctx = AgentContext()
        ctx.set_shared("k", 1)
        ctx.set_state("a", AgentState.COMPLETE)
        ctx.append_message(AgentMessage(sender="a", recipient="b", message_type=MessageType.EVENT))
        ctx.clear()
        assert ctx.get_shared("k") is None
        assert ctx.get_state("a") == AgentState.IDLE
        assert ctx.conversation_size() == 0

    def test_to_dict(self):
        ctx = AgentContext()
        ctx.set_shared("k", "v")
        ctx.set_state("a", AgentState.COMPLETE)
        ctx.append_message(AgentMessage(sender="a", recipient="b", message_type=MessageType.EVENT))
        data = ctx.to_dict()
        assert data["shared"] == {"k": "v"}
        assert data["states"] == {"a": "complete"}
        assert data["conversation_size"] == 1
        assert data["conversation"][0]["message_type"] == "event"

    @pytest.mark.asyncio
    async def test_thread_safe_shared_writes(self):
        ctx = AgentContext()

        def writer(i):
            ctx.set_shared(f"key{i}", i)
            ctx.set_state(f"Agent{i}", AgentState.COMPLETE)

        await asyncio.gather(*(asyncio.to_thread(writer, i) for i in range(16)))
        assert len(ctx.get_shared_data()) == 16
        assert ctx.get_state("Agent7") == AgentState.COMPLETE

    @pytest.mark.asyncio
    async def test_thread_safe_conversation(self):
        ctx = AgentContext()

        def append(i):
            ctx.append_message(
                AgentMessage(sender=f"S{i}", recipient="R", message_type=MessageType.EVENT)
            )

        await asyncio.gather(*(asyncio.to_thread(append, i) for i in range(20)))
        assert ctx.conversation_size() == 20

    def test_context_logs_state(self, caplog):
        caplog.set_level(logging.INFO)
        ctx = AgentContext()
        ctx.set_state("worker", AgentState.BUSY)
        assert any("[Context] agent=worker state=busy" in msg for msg in caplog.messages)


class TestInMemoryMessageBus:
    @pytest.mark.asyncio
    async def test_publish_delivers_to_recipient(self):
        bus = InMemoryMessageBus()
        received = []

        async def handler(msg):
            received.append(msg)
            return None

        bus.subscribe("worker", handler)
        msg = AgentMessage(sender="client", recipient="worker", message_type=MessageType.REQUEST)
        responses = await bus.publish(msg)

        assert len(received) == 1
        assert received[0].sender == "client"
        assert responses == []
        assert bus.get_published_count() == 1
        assert len(bus.get_history()) == 1

    @pytest.mark.asyncio
    async def test_publish_marks_message_delivered(self):
        bus = InMemoryMessageBus()

        async def handler(msg):
            return None

        bus.subscribe("worker", handler)
        msg = AgentMessage(sender="client", recipient="worker", message_type=MessageType.EVENT)
        await bus.publish(msg)
        assert msg.status == MessageStatus.DELIVERED.value

    @pytest.mark.asyncio
    async def test_wildcard_subscriber_receives_all(self):
        bus = InMemoryMessageBus()
        seen = []

        async def wildcard(msg):
            seen.append(msg.recipient)
            return None

        bus.subscribe("*", wildcard)
        await bus.publish(AgentMessage(sender="a", recipient="r1", message_type=MessageType.EVENT))
        await bus.publish(AgentMessage(sender="b", recipient="r2", message_type=MessageType.EVENT))
        assert seen == ["r1", "r2"]

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self):
        bus = InMemoryMessageBus()
        received = []

        async def handler(msg):
            received.append(msg)
            return None

        bus.subscribe("worker", handler)
        bus.unsubscribe("worker", handler)
        await bus.publish(AgentMessage(sender="a", recipient="worker", message_type=MessageType.EVENT))
        assert received == []
        assert bus.subscriber_count("worker") == 0

    @pytest.mark.asyncio
    async def test_history_filtered_by_recipient(self):
        bus = InMemoryMessageBus()

        async def noop(msg):
            return None

        bus.subscribe("worker", noop)
        await bus.publish(AgentMessage(sender="a", recipient="worker", message_type=MessageType.EVENT))
        await bus.publish(AgentMessage(sender="b", recipient="worker", message_type=MessageType.EVENT))
        await bus.publish(AgentMessage(sender="c", recipient="other", message_type=MessageType.EVENT))
        assert len(bus.get_history("worker")) == 2
        assert len(bus.get_history()) == 3

    @pytest.mark.asyncio
    async def test_request_response_round_trip(self):
        bus = InMemoryMessageBus()

        async def worker(msg):
            return AgentMessage(
                sender="worker",
                recipient=msg.sender,
                message_type=MessageType.RESPONSE,
                correlation_id=msg.correlation_id,
                payload={"success": True, "data": {"echo": msg.payload.get("x")}},
            )

        bus.subscribe("worker", worker)
        response = await bus.request("client", "worker", {"x": 42}, timeout=2.0)
        assert response is not None
        assert response.payload["data"]["echo"] == 42

    @pytest.mark.asyncio
    async def test_request_timeout_returns_none(self):
        bus = InMemoryMessageBus()
        response = await bus.request("client", "nobody", {"x": 1}, timeout=0.2)
        assert response is None

    @pytest.mark.asyncio
    async def test_request_uses_feedback_type(self):
        bus = InMemoryMessageBus()
        seen_type = {}

        async def worker(msg):
            seen_type["type"] = msg.message_type
            return AgentMessage(
                sender="worker",
                recipient=msg.sender,
                message_type=MessageType.FEEDBACK,
                correlation_id=msg.correlation_id,
                payload={"success": True, "data": {"ok": 1}},
            )

        bus.subscribe("worker", worker)
        response = await bus.request(
            "client", "worker", {"a": 1}, message_type=MessageType.FEEDBACK, timeout=1.0
        )
        assert seen_type["type"] == MessageType.FEEDBACK
        assert response is not None
        assert response.message_type == MessageType.FEEDBACK

    @pytest.mark.asyncio
    async def test_handler_exception_is_isolated(self):
        bus = InMemoryMessageBus()

        async def boom(msg):
            raise RuntimeError("handler exploded")

        async def ok(msg):
            return None

        bus.subscribe("worker", boom)
        bus.subscribe("worker", ok)
        responses = await bus.publish(
            AgentMessage(sender="a", recipient="worker", message_type=MessageType.EVENT)
        )
        assert responses == []

    @pytest.mark.asyncio
    async def test_clear_resets_bus(self):
        bus = InMemoryMessageBus()

        async def handler(msg):
            return None

        bus.subscribe("worker", handler)
        await bus.publish(AgentMessage(sender="a", recipient="worker", message_type=MessageType.EVENT))
        bus.clear()
        assert bus.get_published_count() == 0
        assert len(bus.get_history()) == 0
        assert bus.subscriber_count("worker") == 0

    @pytest.mark.asyncio
    async def test_close_clears_bus(self):
        bus = InMemoryMessageBus()
        await bus.publish(AgentMessage(sender="a", recipient="b", message_type=MessageType.EVENT))
        bus.close()
        assert len(bus.get_history()) == 0

    @pytest.mark.asyncio
    async def test_thread_safe_subscribe(self):
        bus = InMemoryMessageBus()

        async def handler(msg):
            return None

        def subscribe(i):
            bus.subscribe(f"agent{i}", handler)

        await asyncio.gather(*(asyncio.to_thread(subscribe, i) for i in range(8)))
        assert bus.subscriber_count("agent3") == 1
        assert bus.subscriber_count("agent7") == 1


class TestMessageBusFactory:
    def test_in_memory_backend(self):
        bus = create_message_bus("in-memory")
        assert isinstance(bus, InMemoryMessageBus)

    def test_default_backend_from_settings(self):
        bus = create_message_bus()
        assert isinstance(bus, BaseMessageBus)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError):
            create_message_bus("redis")


class TestBaseAgent:
    def test_bind_attaches_context_and_bus(self):
        agent = ValidationAgent()
        ctx = AgentContext()
        bus = InMemoryMessageBus()
        agent.bind(ctx, bus)
        assert agent._context is ctx
        assert agent._bus is bus

    @pytest.mark.asyncio
    async def test_send_message_without_bus_returns_none(self):
        agent = ValidationAgent()
        assert await agent.send_message("target", {"k": "v"}) is None

    @pytest.mark.asyncio
    async def test_send_message_publishes_event_and_records_conversation(self):
        agent = ValidationAgent()
        ctx = AgentContext()
        bus = InMemoryMessageBus()
        agent.bind(ctx, bus)
        msg = await agent.send_message("target", {"k": "v"}, message_type=MessageType.EVENT)
        assert msg is not None
        assert msg.sender == "ValidationAgent"
        assert msg.message_type == MessageType.EVENT
        assert bus.get_published_count() == 1
        assert ctx.conversation_size() == 1

    @pytest.mark.asyncio
    async def test_request_work_without_bus_returns_none(self):
        agent = ValidationAgent()
        assert await agent.request_work("target", {}) is None

    @pytest.mark.asyncio
    async def test_request_work_with_bus_returns_response(self):
        agent = ValidationAgent()
        ctx = AgentContext()
        bus = InMemoryMessageBus()

        async def worker(msg):
            return AgentMessage(
                sender="worker",
                recipient=msg.sender,
                message_type=MessageType.RESPONSE,
                correlation_id=msg.correlation_id,
                payload={"success": True, "data": {"ok": 1}},
            )

        bus.subscribe("worker", worker)
        agent.bind(ctx, bus)
        response = await agent.request_work("worker", {"a": 1}, timeout=1.0)
        assert response is not None
        assert response.payload["data"] == {"ok": 1}
        assert ctx.conversation_size() == 1

    @pytest.mark.asyncio
    async def test_default_handle_message_unsupported(self):
        agent = ValidationAgent()
        msg = AgentMessage(
            sender="client",
            recipient="ValidationAgent",
            message_type=MessageType.EVENT,
            payload={},
        )
        reply = await agent.handle_message(msg)
        assert reply is not None
        assert reply.payload["success"] is False
        assert "not supported" in reply.payload["error"]

    @pytest.mark.asyncio
    async def test_track_state_success(self):
        ctx = AgentContext()
        agent = TrackedAgent(context=ctx, bus=InMemoryMessageBus())
        with pytest.raises(RuntimeError):
            await agent.execute(None)
        assert agent.get_state() == AgentState.FAILED
        assert ctx.get_state("TrackedAgent") == AgentState.FAILED


class TestAgentHandleMessage:
    @pytest.mark.asyncio
    async def test_search_handle_message(self):
        agent = SearchAgent(search_service=FakeSearchService())
        msg = AgentMessage(
            sender="coordinator",
            recipient="SearchAgent",
            message_type=MessageType.REQUEST,
            payload={"location": "NYC", "business_category": "pizza"},
        )
        reply = await agent.handle_message(msg)
        assert reply.payload["success"] is True
        assert len(reply.payload["data"]["businesses"]) == 2
        assert reply.correlation_id == msg.correlation_id

    @pytest.mark.asyncio
    async def test_enrichment_handle_message(self):
        agent = EnrichmentAgent()
        with patch.object(
            agent._scraper,
            "scrape",
            new=AsyncMock(
                return_value={"emails": ["a@b.com"], "linkedin": None, "facebook": None}
            ),
        ):
            msg = AgentMessage(
                sender="coordinator",
                recipient="EnrichmentAgent",
                message_type=MessageType.REQUEST,
                payload={"action": "scrape_website", "website": "https://x.com"},
            )
            reply = await agent.handle_message(msg)
        assert reply.payload["success"] is True
        assert reply.payload["data"]["email"] == "a@b.com"

    @pytest.mark.asyncio
    async def test_discovery_handle_message(self):
        agent = DiscoveryAgent()
        with patch.object(
            agent._discovery,
            "discover",
            new=AsyncMock(return_value=discovery_with_website("https://found.com")),
        ):
            msg = AgentMessage(
                sender="coordinator",
                recipient="DiscoveryAgent",
                message_type=MessageType.REQUEST,
                payload={"action": "discover_website", "business_name": "X", "location": "A"},
            )
            reply = await agent.handle_message(msg)
        assert reply.payload["success"] is True
        assert reply.payload["data"]["website"] == "https://found.com"

    @pytest.mark.asyncio
    async def test_validation_handle_message(self):
        agent = ValidationAgent()
        msg = AgentMessage(
            sender="coordinator",
            recipient="ValidationAgent",
            message_type=MessageType.REQUEST,
            payload={
                "business": {"name": "X", "formatted_address": "A"},
                "enriched": {"confidence_score": 0.9},
            },
        )
        reply = await agent.handle_message(msg)
        assert reply is not None
        assert reply.payload["data"]["valid"] is True

    @pytest.mark.asyncio
    async def test_non_request_message_unsupported(self):
        agent = SearchAgent(search_service=FakeSearchService())
        msg = AgentMessage(sender="a", recipient="SearchAgent", message_type=MessageType.EVENT)
        reply = await agent.handle_message(msg)
        assert reply.payload["success"] is False


class TestAgentToAgent:
    @pytest.mark.asyncio
    async def test_agent_to_agent_search_request(self):
        ctx = AgentContext()
        bus = InMemoryMessageBus()
        search_agent = SearchAgent(search_service=FakeSearchService(), context=ctx, bus=bus)
        bus.subscribe("SearchAgent", search_agent.handle_message)

        response = await search_agent.request_work(
            "SearchAgent",
            {"location": "NYC", "business_category": "pizza"},
            timeout=2.0,
        )
        assert response is not None
        assert response.payload["success"] is True
        assert len(response.payload["data"]["businesses"]) == 2

    @pytest.mark.asyncio
    async def test_agent_to_agent_state_tracked_in_context(self):
        ctx = AgentContext()
        bus = InMemoryMessageBus()
        search_agent = SearchAgent(search_service=FakeSearchService(), context=ctx, bus=bus)
        bus.subscribe("SearchAgent", search_agent.handle_message)
        await search_agent.request_work(
            "SearchAgent", {"location": "NYC", "business_category": "pizza"}, timeout=2.0
        )
        assert ctx.get_state("SearchAgent") == AgentState.COMPLETE


class TestFeedbackLoop:
    @pytest.mark.asyncio
    async def test_validation_requests_rediscovery(self):
        ctx = AgentContext()
        bus = InMemoryMessageBus()
        discovery = DiscoveryAgent(context=ctx, bus=bus)
        bus.subscribe(discovery.name, discovery.handle_message)

        with patch.object(
            discovery._discovery,
            "discover",
            new=AsyncMock(return_value=discovery_with_website("https://found.com")),
        ):
            validation = ValidationAgent(context=ctx, bus=bus, feedback_timeout=2.0)
            response = await validation.execute(
                ValidationAgentRequest(
                    business={
                        "name": "No Web Co",
                        "formatted_address": "1 Main St",
                        "phone_number": "555-0100",
                    },
                    enriched={"business_name": "No Web Co", "confidence_score": 0.2},
                )
            )

        assert response.feedback.get("website") == "https://found.com"
        assert ctx.conversation_size() >= 1

    @pytest.mark.asyncio
    async def test_no_feedback_when_website_present(self):
        validation = ValidationAgent()
        response = await validation.execute(
            ValidationAgentRequest(
                business={"name": "X", "formatted_address": "A"},
                enriched={"website": "https://x.com", "confidence_score": 0.9},
            )
        )
        assert response.feedback == {}

    @pytest.mark.asyncio
    async def test_feedback_timeout_returns_empty(self):
        ctx = AgentContext()
        bus = InMemoryMessageBus()
        validation = ValidationAgent(context=ctx, bus=bus, feedback_timeout=0.2)
        response = await validation.execute(
            ValidationAgentRequest(
                business={"name": "No Web", "formatted_address": "A"},
                enriched={"confidence_score": 0.1},
            )
        )
        assert response.feedback == {}

    @pytest.mark.asyncio
    async def test_no_bus_skips_feedback(self):
        validation = ValidationAgent()
        response = await validation.execute(
            ValidationAgentRequest(
                business={"name": "No Web", "formatted_address": "A"},
                enriched={"confidence_score": 0.1},
            )
        )
        assert response.feedback == {}

    @pytest.mark.asyncio
    async def test_coordinator_applies_validation_feedback(self):
        search = FakeSearchService()

        class SkipEngine(RuleBasedDecisionEngine):
            async def decide(self, state):
                return []

        agent = CoordinatorAgent(search_service=search, decision_engine=SkipEngine())

        with patch.object(
            agent._discovery,
            "discover",
            new=AsyncMock(return_value=discovery_with_website("https://found.com")),
        ):
            business = Business(
                fsq_place_id="id1",
                name="No Web Co",
                formatted_address="1 Main St",
                phone_number="555-0100",
            )
            enriched = await agent._process_single_lead(business)

        assert enriched.website == "https://found.com"
        assert agent._memory.get("id1", "website") == "https://found.com"
        assert agent._context.conversation_size() >= 1

    @pytest.mark.asyncio
    async def test_validation_requests_enrichment_retry_for_missing_email(self):
        ctx = AgentContext()
        bus = InMemoryMessageBus()
        enrichment = EnrichmentAgent(context=ctx, bus=bus)
        bus.subscribe(enrichment.name, enrichment.handle_message)

        with patch.object(
            enrichment._scraper,
            "scrape",
            new=AsyncMock(
                return_value={
                    "emails": ["retry@example.com"],
                    "linkedin": None,
                    "facebook": None,
                    "instagram": None,
                    "twitter": None,
                    "youtube": None,
                }
            ),
        ):
            validation = ValidationAgent(context=ctx, bus=bus, feedback_timeout=2.0)
            response = await validation.execute(
                ValidationAgentRequest(
                    business={
                        "name": "Retry Co",
                        "formatted_address": "1 Main St",
                        "phone_number": "555-0100",
                        "website": "https://retry.example.com",
                    },
                    enriched={
                        "business_name": "Retry Co",
                        "website": "https://retry.example.com",
                        "confidence_score": 0.5,
                    },
                )
            )

        assert response.feedback.get("email") == "retry@example.com"

    @pytest.mark.asyncio
    async def test_validation_requests_enrichment_on_low_confidence(self):
        ctx = AgentContext()
        bus = InMemoryMessageBus()
        enrichment = EnrichmentAgent(context=ctx, bus=bus)
        bus.subscribe(enrichment.name, enrichment.handle_message)

        with patch.object(
            enrichment._scraper,
            "scrape",
            new=AsyncMock(
                return_value={
                    "emails": ["retry@example.com"],
                    "linkedin": None,
                    "facebook": None,
                    "instagram": None,
                    "twitter": None,
                    "youtube": None,
                }
            ),
        ):
            validation = ValidationAgent(context=ctx, bus=bus, feedback_timeout=2.0)
            response = await validation.execute(
                ValidationAgentRequest(
                    business={
                        "name": "Low Conf",
                        "formatted_address": "1 Main St",
                        "website": "https://low.example.com",
                    },
                    enriched={
                        "business_name": "Low Conf",
                        "website": "https://low.example.com",
                        "email": "a@b.com",
                        "confidence_score": 0.1,
                    },
                )
            )

        assert response.feedback.get("email") == "retry@example.com"

    @pytest.mark.asyncio
    async def test_coordinator_applies_enrichment_feedback(self):
        search = FakeSearchService()

        class SkipEngine(RuleBasedDecisionEngine):
            async def decide(self, state):
                return []

        agent = CoordinatorAgent(search_service=search, decision_engine=SkipEngine())

        with patch.object(
            agent._scraper,
            "scrape",
            new=AsyncMock(
                return_value={
                    "emails": ["retry@example.com"],
                    "linkedin": None,
                    "facebook": None,
                    "instagram": None,
                    "twitter": None,
                    "youtube": None,
                }
            ),
        ):
            business = Business(
                fsq_place_id="id1",
                name="Retry Co",
                formatted_address="1 Main St",
                phone_number="555-0100",
                website="https://retry.example.com",
            )
            enriched = await agent._process_single_lead(business)

        assert enriched.email == "retry@example.com"
        assert agent._memory.get("id1", "email") == "retry@example.com"
        assert agent._context.conversation_size() >= 1


class TestCoordinatorMessaging:
    def test_constructor_binds_context_and_bus_to_agents(self):
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)
        for child in (
            agent.search_agent,
            agent.enrichment_agent,
            agent.discovery_agent,
            agent.validation_agent,
        ):
            assert child._context is agent._context
            assert child._bus is agent._bus

    def test_agents_subscribed_to_bus(self):
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)
        assert agent._bus.subscriber_count("SearchAgent") == 1
        assert agent._bus.subscriber_count("EnrichmentAgent") == 1
        assert agent._bus.subscriber_count("DiscoveryAgent") == 1
        assert agent._bus.subscriber_count("ValidationAgent") == 1

    @pytest.mark.asyncio
    async def test_event_updates_shared_context(self):
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)
        await agent._bus.publish(
            AgentMessage(
                sender="SomeAgent",
                recipient="*",
                message_type=MessageType.EVENT,
                payload={"location": "NYC"},
            )
        )
        assert agent._context.get_shared("location") == "NYC"

    @pytest.mark.asyncio
    async def test_non_event_message_ignored_by_coordinator(self):
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)
        await agent._bus.publish(
            AgentMessage(
                sender="a",
                recipient="*",
                message_type=MessageType.REQUEST,
                payload={"x": 1},
            )
        )
        assert agent._context.get_shared("x") is None

    @pytest.mark.asyncio
    async def test_coordinator_tracks_states_after_search(self):
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)
        with (
            patch.object(
                agent._scraper,
                "scrape",
                new=AsyncMock(
                    return_value={
                        "emails": ["a@b.com"],
                        "linkedin": None,
                        "facebook": None,
                        "instagram": None,
                        "twitter": None,
                        "youtube": None,
                    }
                ),
            ),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            await agent.search(location="NYC", business_category="food")

        states = agent._context.get_states()
        assert states["CoordinatorAgent"] == AgentState.COMPLETE.value
        assert states["SearchAgent"] == AgentState.COMPLETE.value
        assert states["ValidationAgent"] == AgentState.COMPLETE.value

    @pytest.mark.asyncio
    async def test_agentbus_and_event_logging(self, caplog):
        caplog.set_level(logging.INFO)
        search = FakeSearchService()
        agent = CoordinatorAgent(search_service=search)
        await agent._bus.publish(
            AgentMessage(sender="a", recipient="*", message_type=MessageType.EVENT, payload={"x": 1})
        )
        messages = caplog.messages
        assert any("[AgentBus]" in msg for msg in messages)
        assert any("[Coordinator] Event received" in msg for msg in messages)

    @pytest.mark.asyncio
    async def test_validation_feedback_logged(self, caplog):
        caplog.set_level(logging.INFO)
        search = FakeSearchService()

        class SkipEngine(RuleBasedDecisionEngine):
            async def decide(self, state):
                return []

        agent = CoordinatorAgent(search_service=search, decision_engine=SkipEngine())
        with patch.object(
            agent._discovery,
            "discover",
            new=AsyncMock(return_value=discovery_with_website("https://found.com")),
        ):
            business = Business(
                fsq_place_id="id1",
                name="No Web Co",
                formatted_address="1 Main St",
                phone_number="555-0100",
            )
            await agent._process_single_lead(business)

        messages = caplog.messages
        assert any(
            "[ValidationAgent] Requesting DiscoveryAgent" in msg for msg in messages
        )
        assert any("[Coordinator] Applying agent feedback" in msg for msg in messages)
