from unittest.mock import AsyncMock, patch

import pytest

from app.providers.base import Business
from app.services.enrichment_service import EnrichmentService

FAKE_SCRAPE = {
    "emails": ["contact@acme.com"],
    "linkedin": "https://linkedin.com/company/acme",
    "facebook": None,
    "instagram": None,
    "twitter": None,
    "youtube": None,
}


def _business(fsq_id="fsq_1", website="https://acme.example.com"):
    return Business(
        fsq_place_id=fsq_id,
        name="Acme Cafe",
        formatted_address="123 Main St",
        website=website,
        categories=["cafe"],
    )


class TestDiscoverEmails:
    @pytest.mark.asyncio
    async def test_discovers_emails_for_businesses_with_website(self):
        service = EnrichmentService()
        with patch.object(service._scraper, "scrape", new=AsyncMock(return_value=FAKE_SCRAPE)):
            result = await service.discover_emails([_business()])
        assert result == {"fsq_1": "contact@acme.com"}

    @pytest.mark.asyncio
    async def test_skips_businesses_without_website(self):
        service = EnrichmentService()
        scrape = AsyncMock(return_value=FAKE_SCRAPE)
        with patch.object(service._scraper, "scrape", new=scrape):
            result = await service.discover_emails([_business(website=None)])
        assert result == {}
        scrape.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_caches_results_and_avoids_extra_scrapes(self):
        service = EnrichmentService()
        scrape = AsyncMock(return_value=FAKE_SCRAPE)
        with patch.object(service._scraper, "scrape", new=scrape):
            first = await service.discover_emails([_business()])
            second = await service.discover_emails([_business()])
        assert first == {"fsq_1": "contact@acme.com"}
        assert second == {"fsq_1": "contact@acme.com"}
        assert scrape.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_email_returns_no_entry(self):
        service = EnrichmentService()
        empty = {"emails": [], "linkedin": None, "facebook": None, "instagram": None, "twitter": None, "youtube": None}
        with patch.object(service._scraper, "scrape", new=AsyncMock(return_value=empty)):
            result = await service.discover_emails([_business()])
        assert result == {}

    @pytest.mark.asyncio
    async def test_scrape_failure_does_not_fail_batch(self):
        service = EnrichmentService()
        with patch.object(service._scraper, "scrape", new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await service.discover_emails([_business()])
        assert result == {}

    @pytest.mark.asyncio
    async def test_partial_results_preserved(self):
        service = EnrichmentService()

        async def fake_scrape(url):
            if url == "https://acme.example.com":
                return FAKE_SCRAPE
            return {"emails": [], "linkedin": None, "facebook": None, "instagram": None, "twitter": None, "youtube": None}

        businesses = [
            _business(fsq_id="fsq_1"),
            _business(fsq_id="fsq_2", website="https://other.example.com"),
        ]
        with patch.object(service._scraper, "scrape", new=AsyncMock(side_effect=fake_scrape)):
            result = await service.discover_emails(businesses)
        assert result == {"fsq_1": "contact@acme.com"}

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_dict(self):
        service = EnrichmentService()
        assert await service.discover_emails([]) == {}


class TestBusinessFromDict:
    def test_from_dict_roundtrip(self):
        data = {
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
        business = Business.from_dict(data)
        assert business.fsq_place_id == "id1"
        assert business.categories == ["food"]
        assert business.latitude == 40.0
        assert business.to_dict() == data


class _SearchService:
    provider_name = "foursquare"

    async def search(self, **kwargs):
        return [
            {
                "fsq_place_id": "fsq_1",
                "name": "Acme Cafe",
                "formatted_address": "123 Main St",
                "phone_number": "555",
                "website": "https://acme.example.com",
                "rating": 7.5,
                "categories": ["cafe"],
            }
        ]

    async def search_by_query(self, **kwargs):
        return await self.search(**kwargs)

    async def get_business_details(self, place_id):
        from app.core.exceptions import NoResultsError

        raise NoResultsError(provider="foursquare", query=place_id)


class TestSearchEndpointAutoEmail:
    @staticmethod
    def _app(tmp_path):
        from fastapi import FastAPI
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.api.routes import business
        from app.db.database import Base, get_db
        from app.dependencies import get_business_search_service

        engine = create_engine(
            f"sqlite:///{tmp_path / 'bus.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

        def override_get_db():
            session = SessionLocal()
            try:
                yield session
            finally:
                session.close()

        app = FastAPI()
        app.include_router(business.router)
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_business_search_service] = lambda: _SearchService()
        return app, SessionLocal, engine

    @staticmethod
    def _enriched(**overrides):
        from app.models.enriched_business import EnrichedBusiness

        defaults = {
            "business_name": "Acme Cafe",
            "address": "123 Main St",
            "phone": "555",
            "website": "https://acme.example.com",
        }
        defaults.update(overrides)
        return EnrichedBusiness(**defaults)

    def test_search_returns_discovered_email(self, tmp_path):
        from fastapi.testclient import TestClient

        app, SessionLocal, engine = self._app(tmp_path)
        enriched = self._enriched(email="contact@acme.com")
        with patch.object(
            EnrichmentService,
            "enrich_many",
            new=AsyncMock(return_value={"fsq_1": enriched}),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/search",
                    json={"location": "NYC", "business_category": "cafe", "radius": 5000},
                )
        assert response.status_code == 200
        body = response.json()
        assert body["total_results"] == 1
        assert body["businesses"][0]["email"] == "contact@acme.com"

    def test_search_returns_discovered_socials(self, tmp_path):
        from fastapi.testclient import TestClient

        app, SessionLocal, engine = self._app(tmp_path)
        enriched = self._enriched(
            email="contact@acme.com",
            linkedin="https://linkedin.com/company/acme",
            instagram="https://instagram.com/acme",
        )
        with patch.object(
            EnrichmentService,
            "enrich_many",
            new=AsyncMock(return_value={"fsq_1": enriched}),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/search",
                    json={"location": "NYC", "business_category": "cafe", "radius": 5000},
                )
        assert response.status_code == 200
        business = response.json()["businesses"][0]
        assert business["linkedin"] == "https://linkedin.com/company/acme"
        assert business["instagram"] == "https://instagram.com/acme"
        assert business["twitter"] is None
        assert business["youtube"] is None

    def test_search_without_email_stays_none(self, tmp_path):
        from fastapi.testclient import TestClient

        app, SessionLocal, engine = self._app(tmp_path)
        with patch.object(
            EnrichmentService,
            "enrich_many",
            new=AsyncMock(return_value={}),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/search",
                    json={"location": "NYC", "business_category": "cafe", "radius": 5000},
                )
        assert response.status_code == 200
        body = response.json()
        assert body["businesses"][0]["email"] is None

    def test_search_query_returns_discovered_email(self, tmp_path):
        from fastapi.testclient import TestClient

        app, SessionLocal, engine = self._app(tmp_path)
        enriched = self._enriched(email="contact@acme.com")
        with patch.object(
            EnrichmentService,
            "enrich_many",
            new=AsyncMock(return_value={"fsq_1": enriched}),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/search/query",
                    json={"query": "best cafe in NYC", "radius": 5000},
                )
        assert response.status_code == 200
        body = response.json()
        assert body["businesses"][0]["email"] == "contact@acme.com"

    def test_search_includes_confidence_and_enriched_flag(self, tmp_path):
        from fastapi.testclient import TestClient

        app, SessionLocal, engine = self._app(tmp_path)
        enriched = self._enriched(email="contact@acme.com")
        enriched.confidence_score = 0.85
        enriched.source = "Foursquare"
        with patch.object(
            EnrichmentService,
            "enrich_many",
            new=AsyncMock(return_value={"fsq_1": enriched}),
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/search",
                    json={"location": "NYC", "business_category": "cafe", "radius": 5000},
                )
        assert response.status_code == 200
        business = response.json()["businesses"][0]
        assert business.get("confidence_score") == 0.85
        assert business.get("email") == "contact@acme.com"
        # auto_enriched is an internal flag for the UI merger; API may include it
        assert business.get("auto_enriched") is True
