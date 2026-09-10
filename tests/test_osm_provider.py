import httpx
import pytest

from app.core.exceptions import NoResultsError, ProviderError
from app.core.rate_limiter import AdaptiveRateLimiter
from app.providers.base import SearchQuery
from app.providers.osm_provider import (
    OSMProvider,
    build_overpass_filters,
    parse_osm_id,
)

BAKERY = {
    "type": "node",
    "id": 42,
    "lat": 41.88,
    "lon": -87.63,
    "tags": {
        "name": "Corner Bakery",
        "shop": "bakery",
        "addr:housenumber": "12",
        "addr:street": "Main Street",
        "addr:city": "Chicago",
        "addr:state": "IL",
        "addr:postcode": "60601",
        "contact:phone": "+1 312 555 0100",
        "website": "https://cornerbakery.example",
        "contact:email": "hi@cornerbakery.example",
        "opening_hours": "Mo-Fr 07:00-18:00",
    },
}


def make_provider(handler, **kwargs) -> OSMProvider:
    provider = OSMProvider(
        limiter=AdaptiveRateLimiter(min_interval=0.0, max_interval=0.05, name="test"),
        **kwargs,
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def query(**kwargs) -> SearchQuery:
    params = dict(
        query="bakery", location="41.88,-87.63", radius=2000, type="bakery",
        max_results=50,
    )
    params.update(kwargs)
    return SearchQuery(**params)


class TestFilters:
    def test_known_category_uses_real_osm_tags(self):
        assert build_overpass_filters("bakery") == ['nwr["shop"="bakery"]']

    def test_multi_tag_category(self):
        filters = build_overpass_filters("gym")
        assert 'nwr["leisure"="fitness_centre"]' in filters

    def test_qualified_category_matches_its_head_noun(self):
        assert build_overpass_filters("italian restaurant") == [
            'nwr["amenity"="restaurant"]'
        ]

    def test_unknown_category_falls_back_to_a_regex_search(self):
        filters = build_overpass_filters("vape shop")
        assert len(filters) == 2
        assert all("~" in f for f in filters)

    def test_place_id_roundtrip(self):
        assert parse_osm_id("osm:n42") == ("node", 42)
        assert parse_osm_id("osm:w7") == ("way", 7)
        assert parse_osm_id("garbage") is None


class TestSearch:
    async def test_parses_an_overpass_element(self):
        provider = make_provider(
            lambda request: httpx.Response(200, json={"elements": [BAKERY]})
        )
        result = await provider.search(query())
        business = result.businesses[0]
        assert business.fsq_place_id == "osm:n42"
        assert business.name == "Corner Bakery"
        assert business.formatted_address == "12 Main Street, Chicago, IL, 60601"
        assert business.phone_number == "+1 312 555 0100"
        assert business.website == "https://cornerbakery.example"
        assert business.email == "hi@cornerbakery.example"
        assert business.categories == ["shop:bakery"]
        assert business.opening_hours == ["Mo-Fr 07:00-18:00"]
        await provider.close()

    async def test_unnamed_elements_are_dropped(self):
        elements = [BAKERY, {"type": "node", "id": 7, "tags": {"shop": "bakery"}}]
        provider = make_provider(
            lambda request: httpx.Response(200, json={"elements": elements})
        )
        result = await provider.search(query())
        assert result.total_results == 1
        await provider.close()

    async def test_way_uses_its_center_coordinates(self):
        way = {
            "type": "way", "id": 9, "center": {"lat": 1.5, "lon": 2.5},
            "tags": {"name": "Big Store", "shop": "bakery"},
        }
        provider = make_provider(
            lambda request: httpx.Response(200, json={"elements": [way]})
        )
        result = await provider.search(query())
        assert (result.businesses[0].latitude, result.businesses[0].longitude) == (1.5, 2.5)
        assert result.businesses[0].fsq_place_id == "osm:w9"
        await provider.close()

    async def test_max_results_is_honoured(self):
        elements = [dict(BAKERY, id=i) for i in range(10)]
        provider = make_provider(
            lambda request: httpx.Response(200, json={"elements": elements})
        )
        result = await provider.search(query(max_results=3))
        assert result.total_results == 3
        await provider.close()

    async def test_empty_response_raises_no_results(self):
        provider = make_provider(
            lambda request: httpx.Response(200, json={"elements": []})
        )
        with pytest.raises(NoResultsError):
            await provider.search(query())
        await provider.close()

    async def test_text_location_without_geocoding_is_an_error(self):
        provider = make_provider(
            lambda request: httpx.Response(200, json=[])  # nominatim: no match
        )
        with pytest.raises(ProviderError):
            await provider.search(query(location="Nowhere At All"))
        await provider.close()

    async def test_query_targets_the_requested_area(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.content.decode())
            return httpx.Response(200, json={"elements": [BAKERY]})

        provider = make_provider(handler)
        await provider.search(query(location="41.880000,-87.630000", radius=1500))
        assert "around:1500,41.880000,-87.630000" in seen[0]
        assert '["shop"="bakery"]' in seen[0]
        await provider.close()


class TestThrottling:
    async def test_429_rotates_to_the_next_mirror(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"elements": [BAKERY]})

        provider = make_provider(handler)
        result = await provider.search(query())
        assert result.total_results == 1
        assert len({url for url in calls}) == 2, "second attempt used another mirror"
        assert provider.limiter.stats().throttled == 1
        await provider.close()

    async def test_all_mirrors_failing_raises_provider_error(self):
        provider = make_provider(lambda request: httpx.Response(400, text="bad query"))
        with pytest.raises(ProviderError):
            await provider.search(query())
        await provider.close()

    async def test_geocode_caches_results(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=[{"lat": "41.9", "lon": "-87.6"}])

        provider = make_provider(handler)
        assert await provider.geocode("Chicago") == (41.9, -87.6)
        assert await provider.geocode("chicago") == (41.9, -87.6)
        assert len(calls) == 1
        await provider.close()
