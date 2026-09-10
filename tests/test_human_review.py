import asyncio
import logging
import os
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.agent_memory import AgentMemory
from app.agents.coordinator_agent import CoordinatorAgent
from app.agents.decision_engine import SCRAPE_WEBSITE
from app.agents.human_review import (
    HumanReviewAgent,
    ReviewEntry,
    ReviewRequest,
    ReviewStatus,
)
from app.agents.message_bus import create_message_bus
from app.agents.messaging import AgentMessage, AgentState, MessageType
from app.providers.base import Business
from app.services.lead_discovery_service import DiscoveryResult

EMPTY_SCRAPE = {
    "emails": [],
    "linkedin": None,
    "facebook": None,
    "instagram": None,
    "twitter": None,
    "youtube": None,
}

FAKE_SCRAPE = {
    "emails": ["contact@pizzaplace.com"],
    "linkedin": "https://linkedin.com/company/id1",
    "facebook": None,
    "instagram": None,
    "twitter": None,
    "youtube": None,
}


class FakeProvider:
    provider_name = "foursquare"


class FakeSearchService:
    def __init__(self):
        self.provider = FakeProvider()

    async def search(self, location="", business_category="", radius=5000, max_results=20):
        return []


class FakeSearchServiceOneLead:
    def __init__(self):
        self.provider = FakeProvider()

    async def search(self, location="", business_category="", radius=5000, max_results=20):
        return [
            {
                "fsq_place_id": "id1",
                "name": "Pizza Place",
                "formatted_address": "123 Main St",
                "phone_number": "555-0100",
                "website": None,
                "rating": None,
                "categories": ["pizza"],
                "latitude": None,
                "longitude": None,
                "opening_hours": [],
                "price_level": None,
            }
        ]


class NoOpEngine:
    async def decide(self, state):
        return []


class ScrapeOnlyEngine:
    async def decide(self, state):
        if state.get("email"):
            return []
        return [SCRAPE_WEBSITE]


class TestReviewStatus:
    def test_enum_values(self):
        assert ReviewStatus.PENDING.value == "pending"
        assert ReviewStatus.APPROVED.value == "approved"
        assert ReviewStatus.REJECTED.value == "rejected"
        assert ReviewStatus.EDITED.value == "edited"


class TestReviewEntry:
    def test_to_dict(self):
        entry = ReviewEntry(
            status="pending",
            reviewer="Alice",
            timestamp=123.0,
            reason="checking",
            updated_fields={"email": "a@b.com"},
        )
        data = entry.to_dict()
        assert data["status"] == "pending"
        assert data["reviewer"] == "Alice"
        assert data["timestamp"] == 123.0
        assert data["reason"] == "checking"
        assert data["updated_fields"] == {"email": "a@b.com"}

    def test_defaults(self):
        entry = ReviewEntry(status="pending")
        assert entry.reviewer == ""
        assert entry.reason == ""
        assert entry.updated_fields == {}
        assert entry.timestamp > 0


class TestHumanReviewAgentUnit:
    @pytest.mark.asyncio
    async def test_submit_creates_pending_review(self):
        human = HumanReviewAgent()
        response = await human.submit(
            fsq_place_id="id1",
            reviewer="system",
            enriched={"business_name": "Pizza Place", "confidence_score": 0.4},
        )
        assert response.success is True
        assert response.status == ReviewStatus.PENDING.value
        assert response.lead["business_name"] == "Pizza Place"
        assert len(response.history) == 1
        assert response.history[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_submit_is_idempotent(self):
        human = HumanReviewAgent()
        await human.submit(fsq_place_id="id1", enriched={"business_name": "Cafe"})
        await human.approve(fsq_place_id="id1", reviewer="Alice")
        response = await human.submit(fsq_place_id="id1", enriched={"business_name": "Cafe"})
        assert response.status == ReviewStatus.APPROVED.value
        assert len(response.history) == 2

    @pytest.mark.asyncio
    async def test_approve_records_decision(self):
        human = HumanReviewAgent()
        await human.submit(fsq_place_id="id1", enriched={"business_name": "Cafe"})
        response = await human.approve(fsq_place_id="id1", reviewer="Alice", reason="looks good")
        assert response.status == ReviewStatus.APPROVED.value
        assert response.reviewer == "Alice"
        assert response.reason == "looks good"
        assert len(response.history) == 2
        assert response.history[-1]["status"] == "approved"

    @pytest.mark.asyncio
    async def test_reject_records_decision(self):
        human = HumanReviewAgent()
        await human.submit(fsq_place_id="id1", enriched={"business_name": "Cafe"})
        response = await human.reject(fsq_place_id="id1", reviewer="Bob", reason="duplicate")
        assert response.status == ReviewStatus.REJECTED.value
        assert response.reviewer == "Bob"
        assert response.reason == "duplicate"

    @pytest.mark.asyncio
    async def test_edit_applies_fields_and_records(self):
        human = HumanReviewAgent()
        await human.submit(
            fsq_place_id="id1",
            enriched={"business_name": "Cafe", "email": None, "website": "https://cafe.com"},
        )
        response = await human.edit(
            fsq_place_id="id1",
            reviewer="Carol",
            updated_fields={"email": "hello@cafe.com"},
        )
        assert response.status == ReviewStatus.EDITED.value
        assert response.lead["email"] == "hello@cafe.com"
        assert response.lead["website"] == "https://cafe.com"
        assert response.updated_fields == {"email": "hello@cafe.com"}
        assert response.history[-1]["status"] == "edited"

    @pytest.mark.asyncio
    async def test_actions_on_missing_review_return_error(self):
        human = HumanReviewAgent()
        approve = await human.approve(fsq_place_id="nope", reviewer="Alice")
        assert approve.success is False
        assert "No review found" in approve.error
        reject = await human.reject(fsq_place_id="nope", reviewer="Bob")
        assert reject.success is False
        edit = await human.edit(fsq_place_id="nope", reviewer="Carol")
        assert edit.success is False

    @pytest.mark.asyncio
    async def test_get_review_returns_history(self):
        human = HumanReviewAgent()
        await human.submit(fsq_place_id="id1", reviewer="system", enriched={"business_name": "Cafe"})
        await human.approve(fsq_place_id="id1", reviewer="Alice")
        await human.edit(fsq_place_id="id1", reviewer="Carol", updated_fields={"email": "a@b.com"})
        response = human.get_review("id1")
        assert response.status == ReviewStatus.EDITED.value
        assert [entry["status"] for entry in response.history] == ["pending", "approved", "edited"]

    @pytest.mark.asyncio
    async def test_get_review_missing(self):
        human = HumanReviewAgent()
        response = human.get_review("nope")
        assert response.success is False

    @pytest.mark.asyncio
    async def test_list_pending_only_pending(self):
        human = HumanReviewAgent()
        await human.submit(fsq_place_id="id1", enriched={"business_name": "A"})
        await human.submit(fsq_place_id="id2", enriched={"business_name": "B"})
        await human.approve(fsq_place_id="id2", reviewer="Alice")
        pending = human.list_pending()
        assert [r["fsq_place_id"] for r in pending] == ["id1"]
        assert pending[0]["status"] == ReviewStatus.PENDING.value

    @pytest.mark.asyncio
    async def test_clear(self):
        human = HumanReviewAgent()
        await human.submit(fsq_place_id="id1", enriched={"business_name": "A"})
        human.clear()
        assert human.list_pending() == []
        assert human.get_review("id1").success is False

    @pytest.mark.asyncio
    async def test_execute_dispatches_actions(self):
        human = HumanReviewAgent()
        submit = await human.execute(
            ReviewRequest(action="submit", fsq_place_id="id1", enriched={"business_name": "Cafe"})
        )
        assert submit.status == ReviewStatus.PENDING.value
        approve = await human.execute(
            ReviewRequest(action="approve", fsq_place_id="id1", reviewer="Alice")
        )
        assert approve.status == ReviewStatus.APPROVED.value
        reject = await human.execute(
            ReviewRequest(action="reject", fsq_place_id="id1", reviewer="Bob")
        )
        assert reject.status == ReviewStatus.REJECTED.value
        edit = await human.execute(
            ReviewRequest(action="edit", fsq_place_id="id1", reviewer="Carol", updated_fields={"email": "a@b.com"})
        )
        assert edit.status == ReviewStatus.EDITED.value
        get = await human.execute(ReviewRequest(action="get", fsq_place_id="id1"))
        assert get.success is True

    @pytest.mark.asyncio
    async def test_execute_unknown_action(self):
        human = HumanReviewAgent()
        response = await human.execute(ReviewRequest(action="bogus", fsq_place_id="id1"))
        assert response.success is False
        assert "unknown review action" in response.error

    @pytest.mark.asyncio
    async def test_handle_message_approve_via_bus(self):
        bus = create_message_bus()
        human = HumanReviewAgent(bus=bus, memory=AgentMemory())
        bus.subscribe("HumanReviewAgent", human.handle_message)
        await human.submit(fsq_place_id="id1", reviewer="system", enriched={"business_name": "Cafe"})

        response = await bus.request(
            sender="CoordinatorAgent",
            recipient="HumanReviewAgent",
            payload={"action": "approve", "fsq_place_id": "id1", "reviewer": "Alice"},
            message_type=MessageType.REQUEST,
            timeout=5.0,
        )
        assert response is not None
        data = response.payload.get("data", {})
        assert data["status"] == "approved"
        assert data["reviewer"] == "Alice"

    @pytest.mark.asyncio
    async def test_handle_message_unsupported_type(self):
        human = HumanReviewAgent()
        message = AgentMessage(
            sender="X",
            recipient="HumanReviewAgent",
            message_type=MessageType.EVENT,
            payload={},
        )
        response = await human.handle_message(message)
        assert response is not None
        assert response.payload.get("success") is False

    @pytest.mark.asyncio
    async def test_state_tracking(self):
        human = HumanReviewAgent()
        assert human.get_state() == AgentState.IDLE
        await human.submit(fsq_place_id="id1", enriched={"business_name": "Cafe"})
        assert human.get_state() == AgentState.COMPLETE

    @pytest.mark.asyncio
    async def test_logs_pending_approved_rejected_edited(self, caplog):
        caplog.set_level(logging.INFO)
        human = HumanReviewAgent()
        await human.submit(fsq_place_id="id1", reviewer="system", enriched={"business_name": "Cafe"})
        await human.approve(fsq_place_id="id1", reviewer="Alice")
        await human.reject(fsq_place_id="id1", reviewer="Bob", reason="duplicate")
        await human.edit(fsq_place_id="id1", reviewer="Carol", updated_fields={"email": "a@b.com"})
        assert any("[HumanReview] Pending fsq_place_id=id1" in m for m in caplog.messages)
        assert any("[HumanReview] Approved fsq_place_id=id1" in m for m in caplog.messages)
        assert any("[HumanReview] Rejected fsq_place_id=id1" in m for m in caplog.messages)
        assert any("[HumanReview] Edited fsq_place_id=id1" in m for m in caplog.messages)


class TestCoordinatorReviewRouting:
    @pytest.mark.asyncio
    async def test_low_confidence_lead_routed_to_review(self):
        search = FakeSearchService()
        human = HumanReviewAgent()
        agent = CoordinatorAgent(
            search_service=search,
            decision_engine=NoOpEngine(),
            human_review_agent=human,
        )

        business = Business(
            fsq_place_id="id1",
            name="Pizza Place",
            formatted_address="123 Main St",
            phone_number="555-0100",
            website=None,
        )
        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=EMPTY_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            enriched = await agent._process_single_lead(business)

        assert enriched is not None
        assert human.get_review("id1").success is True
        assert human.get_review("id1").status == ReviewStatus.PENDING.value
        assert human.get_review("id1").lead["business_name"] == "Pizza Place"
        assert agent._memory.get("id1", "review_status") == ReviewStatus.PENDING.value
        assert len(agent._memory.get("id1", "review_history")) == 1

    @pytest.mark.asyncio
    async def test_high_confidence_lead_not_routed(self):
        search = FakeSearchService()
        human = HumanReviewAgent()
        agent = CoordinatorAgent(
            search_service=search,
            decision_engine=ScrapeOnlyEngine(),
            human_review_agent=human,
        )

        business = Business(
            fsq_place_id="id1",
            name="Pizza Place",
            formatted_address="123 Main St",
            phone_number="555-0100",
            website="https://pizza.example.com",
            latitude=40.7128,
            longitude=-74.0060,
        )
        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            enriched = await agent._process_single_lead(business)

        assert enriched is not None
        assert enriched.email == "contact@pizzaplace.com"
        assert human.get_review("id1").success is False

    @pytest.mark.asyncio
    async def test_no_review_agent_no_review_records(self):
        search = FakeSearchService()
        agent = CoordinatorAgent(
            search_service=search,
            decision_engine=NoOpEngine(),
        )
        business = Business(
            fsq_place_id="id1",
            name="Pizza Place",
            formatted_address="123 Main St",
            website=None,
        )
        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=EMPTY_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            enriched = await agent._process_single_lead(business)

        assert enriched is not None
        assert agent._memory.get("id1", "review_status") is None

    @pytest.mark.asyncio
    async def test_routed_lead_can_be_approved_afterwards(self):
        search = FakeSearchService()
        human = HumanReviewAgent()
        agent = CoordinatorAgent(
            search_service=search,
            decision_engine=NoOpEngine(),
            human_review_agent=human,
        )
        business = Business(
            fsq_place_id="id1",
            name="Pizza Place",
            formatted_address="123 Main St",
            website=None,
        )
        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=EMPTY_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            await agent._process_single_lead(business)

        response = await human.approve(fsq_place_id="id1", reviewer="Alice", reason="verified")
        assert response.status == ReviewStatus.APPROVED.value
        assert len(response.history) == 2

    @pytest.mark.asyncio
    async def test_human_review_agent_bound_to_context_and_bus(self):
        search = FakeSearchService()
        human = HumanReviewAgent()
        agent = CoordinatorAgent(
            search_service=search,
            human_review_agent=human,
        )
        assert agent.human_review_agent is human
        assert human._context is agent._context
        assert human._bus is agent._bus
        assert agent._bus.subscriber_count("HumanReviewAgent") == 1
        assert agent._context.get_state("HumanReviewAgent") == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_full_search_routes_qualifying_lead(self):
        human = HumanReviewAgent()
        agent = CoordinatorAgent(
            search_service=FakeSearchServiceOneLead(),
            decision_engine=NoOpEngine(),
            human_review_agent=human,
        )
        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=EMPTY_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            result = await agent.search(location="New York, NY", business_category="restaurant")

        assert result["total_results"] == 1
        assert human.get_review("id1").status == ReviewStatus.PENDING.value
        assert agent._memory.get("id1", "review_status") == ReviewStatus.PENDING.value

    @pytest.mark.asyncio
    async def test_review_routing_logged(self, caplog):
        caplog.set_level(logging.INFO)
        human = HumanReviewAgent()
        agent = CoordinatorAgent(
            search_service=FakeSearchService(),
            decision_engine=NoOpEngine(),
            human_review_agent=human,
        )
        business = Business(
            fsq_place_id="id1",
            name="Pizza Place",
            formatted_address="123 Main St",
            website=None,
        )
        with (
            patch.object(agent._scraper, "scrape", new=AsyncMock(return_value=EMPTY_SCRAPE)),
            patch.object(agent._discovery, "discover", new=AsyncMock(return_value=DiscoveryResult())),
        ):
            await agent._process_single_lead(business)
        assert any(
            "[Coordinator] Routing lead to human review" in m for m in caplog.messages
        )
        assert any("[HumanReview] Pending fsq_place_id=id1" in m for m in caplog.messages)


class TestReviewEndpoints:
    @pytest.fixture(autouse=True)
    def _clean_review_agent(self):
        from app.api.routes.review import review_agent

        review_agent.clear()
        yield
        review_agent.clear()

    @pytest.fixture()
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.routes.review import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def _submit(self, fsq_place_id, **kwargs):
        from app.api.routes.review import review_agent

        kwargs.setdefault("reviewer", "system")
        kwargs.setdefault("enriched", {"business_name": "Cafe", "confidence_score": 0.4})
        asyncio.run(review_agent.submit(fsq_place_id, **kwargs))

    def test_list_pending_empty(self, client):
        response = client.get("/review/pending")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["reviews"] == []

    def test_list_pending_after_submit(self, client):
        self._submit("id1")
        response = client.get("/review/pending")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["reviews"][0]["fsq_place_id"] == "id1"
        assert data["reviews"][0]["status"] == "pending"

    def test_get_review(self, client):
        self._submit("id1")
        response = client.get("/review/id1")
        assert response.status_code == 200
        data = response.json()
        assert data["fsq_place_id"] == "id1"
        assert data["status"] == "pending"
        assert data["lead"]["business_name"] == "Cafe"
        assert len(data["history"]) == 1

    def test_get_review_404(self, client):
        response = client.get("/review/missing")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "NotFound"

    def test_approve_review(self, client):
        self._submit("id1")
        response = client.post("/review/id1/approve", json={"reviewer": "Alice", "reason": "ok"})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "approved"
        assert data["reviewer"] == "Alice"
        assert data["reason"] == "ok"
        assert len(data["history"]) == 2

    def test_reject_review(self, client):
        self._submit("id1")
        response = client.post("/review/id1/reject", json={"reviewer": "Bob", "reason": "duplicate"})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "rejected"
        assert data["reviewer"] == "Bob"
        assert data["reason"] == "duplicate"

    def test_edit_review(self, client):
        self._submit("id1")
        response = client.post(
            "/review/id1/edit",
            json={"reviewer": "Carol", "updated_fields": {"email": "hello@cafe.com"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "edited"
        assert data["lead"]["email"] == "hello@cafe.com"
        assert data["updated_fields"] == {"email": "hello@cafe.com"}

    def test_actions_on_missing_review_404(self, client):
        for path in ("/review/missing/approve", "/review/missing/reject", "/review/missing/edit"):
            response = client.post(path, json={"reviewer": "Alice"})
            assert response.status_code == 404

    def test_full_lifecycle_history(self, client):
        self._submit("id1")
        client.post("/review/id1/approve", json={"reviewer": "Alice"})
        client.post("/review/id1/edit", json={"reviewer": "Carol", "updated_fields": {"email": "a@b.com"}})
        response = client.get("/review/id1")
        assert response.status_code == 200
        data = response.json()
        statuses = [entry["status"] for entry in data["history"]]
        assert statuses == ["pending", "approved", "edited"]
        assert data["status"] == "edited"


class TestReviewPageUI:
    @staticmethod
    def _fixture_reviews():
        return {
            "reviews": [
                {
                    "fsq_place_id": "abc123",
                    "status": "pending",
                    "reviewer": "",
                    "timestamp": 100.0,
                    "reason": "",
                    "updated_fields": {},
                    "history": [
                        {
                            "status": "pending",
                            "reviewer": "system",
                            "timestamp": 100.0,
                            "reason": "",
                            "updated_fields": {},
                        }
                    ],
                    "lead": {
                        "business_name": "Test Cafe",
                        "confidence_score": 0.45,
                        "email": "cafe@example.com",
                        "website": "https://cafe.example.com",
                        "phone": "555-0100",
                    },
                }
            ],
            "total": 1,
        }

    @staticmethod
    def _load_frontend_app():
        import importlib.util
        import sys

        if "frontend_app" in sys.modules:
            return sys.modules["frontend_app"]
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        frontend_dir = os.path.join(root, "frontend")
        path = os.path.join(frontend_dir, "app.py")
        sys.path.insert(0, frontend_dir)
        try:
            spec = importlib.util.spec_from_file_location("frontend_app", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["frontend_app"] = module
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(frontend_dir)
        return module

    @staticmethod
    def _page(**overrides):
        from contextlib import contextmanager

        from streamlit.testing.v1 import AppTest

        @contextmanager
        def _run():
            appmod = TestReviewPageUI._load_frontend_app()
            harness = os.path.join(os.path.dirname(__file__), "_review_harness.py")
            if overrides:
                with patch.multiple(appmod, **overrides):
                    at = AppTest.from_file(harness, default_timeout=20)
                    at.run()
                    yield at
            else:
                at = AppTest.from_file(harness, default_timeout=20)
                at.run()
                yield at

        return _run()

    def test_renders_pending_reviews(self):
        with self._page(get_pending_reviews=lambda: self._fixture_reviews()) as at:
            assert not at.exception
            assert at.button(key="approve_abc123")
            assert at.button(key="reject_abc123")

    def test_empty_state(self):
        with self._page(get_pending_reviews=lambda: {"reviews": [], "total": 0}) as at:
            assert not at.exception
            assert at.info

    def test_approve_flow(self):
        calls = {}

        def fake_approve(fsq_place_id, reviewer="", reason=""):
            calls["fsq_place_id"] = fsq_place_id
            calls["reviewer"] = reviewer
            calls["reason"] = reason

        with self._page(
            get_pending_reviews=lambda: self._fixture_reviews(),
            approve_review=fake_approve,
        ) as at:
            at.text_input(key="review_reviewer").set_value("Alice")
            at.button(key="approve_abc123").click().run()
            assert not at.exception
        assert calls["fsq_place_id"] == "abc123"
        assert calls["reviewer"] == "Alice"

    def test_reject_flow(self):
        calls = {}

        def fake_reject(fsq_place_id, reviewer="", reason=""):
            calls["fsq_place_id"] = fsq_place_id
            calls["reviewer"] = reviewer
            calls["reason"] = reason

        with self._page(
            get_pending_reviews=lambda: self._fixture_reviews(),
            reject_review=fake_reject,
        ) as at:
            at.text_input(key="review_reviewer").set_value("Bob")
            at.text_input(key="reason_abc123").set_value("No contact info")
            at.button(key="reject_abc123").click().run()
            assert not at.exception
        assert calls["fsq_place_id"] == "abc123"
        assert calls["reviewer"] == "Bob"
        assert calls["reason"] == "No contact info"

    def test_edit_flow(self):
        calls = {}

        def fake_edit(fsq_place_id, reviewer="", updated_fields=None):
            calls["fsq_place_id"] = fsq_place_id
            calls["reviewer"] = reviewer
            calls["updated_fields"] = updated_fields or {}

        with self._page(
            get_pending_reviews=lambda: self._fixture_reviews(),
            edit_review=fake_edit,
        ) as at:
            at.text_input(key="review_reviewer").set_value("Carol")
            at.text_input(key="edit_email_abc123").set_value("new@cafe.com")
            at.button(key="save_edit_abc123").click().run()
            assert not at.exception
        assert calls["fsq_place_id"] == "abc123"
        assert calls["reviewer"] == "Carol"
        assert calls["updated_fields"].get("email") == "new@cafe.com"
