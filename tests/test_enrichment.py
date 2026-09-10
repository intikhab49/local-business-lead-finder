import pytest

from app.models.enriched_business import EnrichedBusiness
from app.providers.base import Business
from app.services.enrichment_service import EnrichmentService


class TestEnrichedBusiness:
    def test_from_business(self):
        business = Business(
            fsq_place_id="abc123",
            name="Test Restaurant",
            formatted_address="123 Main St, City, ST 12345",
            phone_number="+1-555-123-4567",
            website="https://test.com",
            latitude=40.7128,
            longitude=-74.0060,
        )

        enriched = EnrichedBusiness.from_business(business)

        assert enriched.business_name == "Test Restaurant"
        assert enriched.address == "123 Main St, City, ST 12345"
        assert enriched.phone == "+1-555-123-4567"
        assert enriched.website == "https://test.com"
        assert enriched.latitude == 40.7128
        assert enriched.longitude == -74.0060
        assert enriched.email is None
        assert enriched.linkedin is None
        assert enriched.facebook is None
        assert enriched.instagram is None
        assert enriched.twitter is None
        assert enriched.youtube is None
        assert enriched.confidence_score == 0.0
        assert enriched.source == "Google Places"

    def test_from_business_minimal(self):
        business = Business(
            fsq_place_id="abc",
            name="Minimal Biz",
            formatted_address="Addr",
        )

        enriched = EnrichedBusiness.from_business(business)

        assert enriched.business_name == "Minimal Biz"
        assert enriched.address == "Addr"
        assert enriched.phone is None
        assert enriched.website is None
        assert enriched.latitude is None

    def test_to_dict(self):
        enriched = EnrichedBusiness(
            business_name="Test",
            address="Addr",
            phone="555",
            website="https://example.com",
            email="test@example.com",
            linkedin="https://linkedin.com/in/test",
            facebook="https://facebook.com/test",
            instagram="https://instagram.com/test",
            twitter="https://twitter.com/test",
            youtube="https://youtube.com/@test",
            latitude=40.71,
            longitude=-74.00,
            confidence_score=0.5,
            source="Foursquare",
        )

        d = enriched.to_dict()

        assert d["business_name"] == "Test"
        assert d["email"] == "test@example.com"
        assert d["linkedin"] == "https://linkedin.com/in/test"
        assert d["twitter"] == "https://twitter.com/test"
        assert d["youtube"] == "https://youtube.com/@test"
        assert d["confidence_score"] == 0.5
        assert d["source"] == "Foursquare"

    def test_merge(self):
        enriched = EnrichedBusiness(business_name="Test", address="Addr")
        enriched.merge(phone="555", website="https://site.com")

        assert enriched.phone == "555"
        assert enriched.website == "https://site.com"

    def test_merge_ignores_none(self):
        enriched = EnrichedBusiness(business_name="Test", address="Addr")
        enriched.merge(phone=None, website="https://site.com")

        assert enriched.phone is None
        assert enriched.website == "https://site.com"


class TestEnrichmentService:
    def test_enrich(self):
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

        enriched = service.enrich(business)

        assert enriched.business_name == "Test Restaurant"
        assert enriched.address == "123 Main St"
        assert enriched.phone == "+1-555-1234"
        assert enriched.website == "https://test.com"
        assert enriched.latitude == 40.71
        assert enriched.longitude == -74.00
        assert enriched.email is None
        assert enriched.linkedin is None
        assert enriched.facebook is None
        assert enriched.instagram is None
        assert enriched.twitter is None
        assert enriched.youtube is None
        assert enriched.source == "Google Places"

    def test_confidence_business_fields_only(self):
        service = EnrichmentService()
        business = Business(
            fsq_place_id="abc",
            name="Full Biz",
            formatted_address="Addr",
            phone_number="555",
            website="https://site.com",
            latitude=40.0,
            longitude=-74.0,
        )
        enriched = service.enrich(business)
        assert enriched.confidence_score == 0.65

    def test_confidence_no_fields(self):
        service = EnrichmentService()
        business = Business(
            fsq_place_id="abc",
            name="",
            formatted_address="",
        )
        enriched = service.enrich(business)
        assert enriched.confidence_score == 0.0

    def test_confidence_partial(self):
        service = EnrichmentService()
        business = Business(
            fsq_place_id="abc",
            name="Partial Biz",
            formatted_address="Addr",
        )
        enriched = service.enrich(business)
        assert enriched.confidence_score == 0.3

    @pytest.mark.asyncio
    async def test_enrich_with_scraper_no_website(self):
        service = EnrichmentService()
        business = Business(
            fsq_place_id="abc",
            name="No Web",
            formatted_address="Addr",
        )
        enriched = await service.enrich_with_scraper(business)
        assert enriched.business_name == "No Web"
        assert enriched.email is None
