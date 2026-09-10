import pytest

from app.core.exceptions import NoResultsError, ProviderError, ValidationError
from app.providers.base import Business, SearchQuery, SearchResult
from app.schemas.requests import (
    BusinessDetailResponse,
    BusinessResponse,
    BusinessSearchQueryRequest,
    BusinessSearchRequest,
    ErrorResponse,
    HealthResponse,
    SearchResponse,
)


class TestSchemas:
    def test_business_search_request(self):
        req = BusinessSearchRequest(location="NYC", business_category="restaurant")
        assert req.location == "NYC"
        assert req.business_category == "restaurant"
        assert req.radius == 5000
        assert req.max_results is None

    def test_business_search_request_validation(self):
        with pytest.raises(ValueError):
            BusinessSearchRequest(location="", business_category="restaurant")
        with pytest.raises(ValueError):
            BusinessSearchRequest(location="NYC", business_category="", radius=50)
        with pytest.raises(ValueError):
            BusinessSearchRequest(location="NYC", business_category="food", max_results=0)

    def test_business_search_query_request(self):
        req = BusinessSearchQueryRequest(query="pizza near me")
        assert req.query == "pizza near me"
        assert req.radius == 5000

    def test_business_response(self):
        resp = BusinessResponse(
            fsq_place_id="abc123",
            name="Test Biz",
            formatted_address="123 Main St",
        )
        assert resp.fsq_place_id == "abc123"
        assert resp.name == "Test Biz"
        assert resp.rating is None
        assert resp.categories == []

    def test_business_response_full(self):
        resp = BusinessResponse(
            fsq_place_id="abc123",
            name="Full Biz",
            formatted_address="456 Oak Ave",
            phone_number="+1-555-1234",
            website="https://example.com",
            rating=8.5,
            categories=["restaurant", "pizza"],
            latitude=40.71,
            longitude=-74.00,
            opening_hours=["Mon-Fri: 9-5"],
            price_level=2,
        )
        assert resp.phone_number == "+1-555-1234"
        assert resp.rating == 8.5
        assert len(resp.categories) == 2
        assert resp.price_level == 2

    def test_search_response(self):
        businesses = [
            BusinessResponse(fsq_place_id="1", name="A", formatted_address="Addr1"),
            BusinessResponse(fsq_place_id="2", name="B", formatted_address="Addr2"),
        ]
        resp = SearchResponse(
            businesses=businesses,
            total_results=2,
            search_location="NYC",
            search_category="restaurant",
            search_radius=5000,
        )
        assert resp.total_results == 2
        assert len(resp.businesses) == 2

    def test_business_detail_response(self):
        b = BusinessResponse(fsq_place_id="1", name="Test", formatted_address="Addr")
        resp = BusinessDetailResponse(business=b)
        assert resp.business.name == "Test"

    def test_health_response(self):
        resp = HealthResponse(status="healthy", service="test", version="1.0", provider="foursquare")
        assert resp.status == "healthy"
        assert resp.provider == "foursquare"

    def test_error_response(self):
        resp = ErrorResponse(error="NotFound", message="No results", status_code=404)
        assert resp.error == "NotFound"
        assert resp.details is None

        resp2 = ErrorResponse(error="BadRequest", message="Invalid", status_code=400, details={"field": "location"})
        assert resp2.details == {"field": "location"}


class TestBusinessDataclass:
    def test_business_minimal(self):
        b = Business(fsq_place_id="abc", name="Test", formatted_address="Addr")
        assert b.fsq_place_id == "abc"
        assert b.name == "Test"

    def test_business_to_dict(self):
        b = Business(
            fsq_place_id="abc",
            name="Test",
            formatted_address="Addr",
            phone_number="555",
            rating=7.5,
            categories=["food"],
            latitude=40.0,
            longitude=-74.0,
            opening_hours=["9-5"],
            price_level=2,
        )
        d = b.to_dict()
        assert d["fsq_place_id"] == "abc"
        assert d["rating"] == 7.5
        assert d["categories"] == ["food"]
        assert d["price_level"] == 2

    def test_search_query(self):
        q = SearchQuery(
            query="pizza",
            location="NYC",
            radius=5000,
            type="restaurant",
            keyword="vegan",
            min_rating=4.0,
            max_results=20,
        )
        assert q.query == "pizza"
        assert q.location == "NYC"
        assert q.type == "restaurant"

    def test_search_result(self):
        businesses = [Business(fsq_place_id="1", name="A", formatted_address="Addr")]
        sr = SearchResult(businesses=businesses, total_results=1)
        assert sr.total_results == 1
        assert len(sr.businesses) == 1


class TestExceptions:
    def test_validation_error(self):
        e = ValidationError("Location is required", field="location")
        assert "Location is required" in e.message
        assert e.status_code == 400
        assert e.details == {"field": "location"}

    def test_provider_error(self):
        e = ProviderError("API failed", provider="foursquare", status_code=502)
        assert e.provider == "foursquare"
        assert e.status_code == 502

    def test_no_results_error(self):
        e = NoResultsError(provider="foursquare", query="restaurant near NYC")
        assert "No results found" in e.message
        assert e.status_code == 404


class TestSearchResultsCleaning:
    """Search Results must show the same cleaned categories as Saved Leads/CSV/Excel."""

    @staticmethod
    def _make_response(categories):
        from app.api.routes.business import _business_to_response

        return _business_to_response(
            {
                "fsq_place_id": "abc123",
                "name": "Test Biz",
                "formatted_address": "123 Main St",
                "categories": categories,
            }
        )

    def test_search_results_drop_generic_categories(self):
        resp = self._make_response(
            ["educational_institution", "point_of_interest", "establishment"]
        )
        assert resp.categories == ["educational_institution"]

    def test_search_results_keep_meaningful_categories(self):
        resp = self._make_response(["cafe", "point_of_interest"])
        assert resp.categories == ["cafe"]

    def test_search_results_empty_categories(self):
        resp = self._make_response([])
        assert resp.categories == []

    def test_search_results_keep_email_none_when_no_public_email_is_found(self):
        resp = self._make_response([])
        assert resp.email is None
