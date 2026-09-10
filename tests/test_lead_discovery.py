import pytest

from app.models.enriched_business import EnrichedBusiness
from app.providers.base import Business
from app.services.enrichment_service import EnrichmentService
from app.services.lead_discovery_service import (
    DiscoveredField,
    DiscoveryResult,
    LeadDiscoveryService,
)


class TestDiscoveredField:
    def test_minimal(self):
        df = DiscoveredField(value="https://example.com")
        assert df.value == "https://example.com"
        assert df.confidence == 0.0
        assert df.source == ""

    def test_full(self):
        df = DiscoveredField(
            value="https://linkedin.com/company/test",
            confidence=0.8,
            source="website_scrape",
            method="social_link_extraction",
        )
        assert df.confidence == 0.8
        assert df.source == "website_scrape"


class TestDiscoveryResult:
    def test_empty(self):
        r = DiscoveryResult()
        assert r.website is None
        assert r.get_flat() == {
            "website": None,
            "linkedin": None,
            "facebook": None,
            "instagram": None,
            "twitter": None,
            "youtube": None,
        }

    def test_with_website(self):
        r = DiscoveryResult(
            website=DiscoveredField(
                value="https://example.com",
                confidence=0.9,
                source="website_verification",
                method="page_content_check",
            ),
        )
        d = r.to_dict()
        assert d["website"]["value"] == "https://example.com"
        assert d["website"]["confidence"] == 0.9

    def test_get_flat(self):
        r = DiscoveryResult(
            website=DiscoveredField(value="https://site.com"),
            linkedin=DiscoveredField(value="https://linkedin.com/co/test"),
        )
        flat = r.get_flat()
        assert flat["website"] == "https://site.com"
        assert flat["linkedin"] == "https://linkedin.com/co/test"
        assert flat["facebook"] is None


class TestLeadDiscoveryService:
    @pytest.mark.asyncio
    async def test_find_website_placeholder(self):
        service = LeadDiscoveryService()
        result = await service.find_website("Test Business", "restaurant", "NYC")
        assert result is None

    @pytest.mark.asyncio
    async def test_discover_no_website(self):
        service = LeadDiscoveryService()
        result = await service.discover(
            business_name="Test Business",
            category="restaurant",
            location="NYC",
        )
        assert result.website is None
        assert result.linkedin is None

    @pytest.mark.asyncio
    async def test_discover_with_existing_website(self):
        service = LeadDiscoveryService()

        result = await service.discover(
            business_name="DoesNotExist",
            category="test",
            location="nowhere",
            existing_website="https://example.com",
        )

        assert result.website is not None
        assert result.website.value == "https://example.com"
        assert result.website.confidence >= 0.0

    def test_enrich_merge_does_not_overwrite_with_none(self):
        enriched = EnrichedBusiness(
            business_name="Test",
            address="Addr",
            website="https://existing.com",
            linkedin="https://linkedin.com/in/test",
        )

        enriched.merge(website=None, linkedin="https://linkedin.com/in/new")

        assert enriched.website == "https://existing.com"
        assert enriched.linkedin == "https://linkedin.com/in/new"

    def test_enrich_merge_overwrites_with_value(self):
        enriched = EnrichedBusiness(
            business_name="Test",
            address="Addr",
            website="https://old.com",
        )

        enriched.merge(website="https://new.com")
        assert enriched.website == "https://new.com"


class TestEnrichWithDiscovery:
    @pytest.mark.asyncio
    async def test_enrich_with_discovery_basic(self):
        service = EnrichmentService()
        business = Business(
            fsq_place_id="abc123",
            name="Test Restaurant",
            formatted_address="123 Main St",
            phone_number="+1-555-1234",
            website="https://test.com",
            latitude=40.71,
            longitude=-74.00,
        )

        enriched = await service.enrich_with_discovery(business)

        assert enriched.business_name == "Test Restaurant"
        assert enriched.website == "https://test.com"
        assert enriched.confidence_score > 0

    @pytest.mark.asyncio
    async def test_enrich_with_discovery_no_website(self):
        service = EnrichmentService()
        business = Business(
            fsq_place_id="abc",
            name="No Web Biz",
            formatted_address="Addr",
        )

        enriched = await service.enrich_with_discovery(business)

        assert enriched.business_name == "No Web Biz"
        assert enriched.website is None
