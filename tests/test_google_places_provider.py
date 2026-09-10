import asyncio
import json

import httpx

from app.providers.base import SearchQuery
from app.providers.google_places_provider import GooglePlacesProvider


async def test_search_follows_google_page_tokens_for_more_than_twenty_results():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        page_token = payload.get("pageToken")
        start = {None: 0, "page-2": 20, "page-3": 40}[page_token]
        places = [
            {
                "id": str(index),
                "displayName": {"text": f"Business {index}"},
                "formattedAddress": f"{index} Main Street",
            }
            for index in range(start, min(start + 20, 45))
        ]
        response: dict = {"places": places}
        if start == 0:
            response["nextPageToken"] = "page-2"
        elif start == 20:
            response["nextPageToken"] = "page-3"
        return httpx.Response(200, json=response)

    provider = GooglePlacesProvider(api_key="test-key", max_results=50)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    result = await provider.search(SearchQuery(query="dentist", max_results=45))

    assert result.total_results == 45
    assert len(requests) >= 3
    assert all(payload["maxResultCount"] == 20 for payload in requests)
    assert sum(payload.get("pageToken") == "page-2" for payload in requests) >= 1
    assert sum(payload.get("pageToken") == "page-3" for payload in requests) >= 1

    await provider.close()


async def test_uncapped_search_exhausts_pages_and_query_variants():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        query_number = [
            "dentist near Austin",
            "dentist businesses near Austin",
            "best dentist near Austin",
        ].index(payload["textQuery"])
        if payload.get("pageToken"):
            places = [
                {
                    "id": f"page-2-{query_number}",
                    "displayName": {"text": "Page Two Business"},
                    "formattedAddress": "2 Main Street",
                }
            ]
            return httpx.Response(200, json={"places": places})
        places = [
            {
                "id": f"page-1-{query_number}-{index}",
                "displayName": {"text": "Page One Business"},
                "formattedAddress": "1 Main Street",
            }
            for index in range(20)
        ]
        return httpx.Response(
            200,
            json={"places": places, "nextPageToken": f"token-{query_number}"},
        )

    provider = GooglePlacesProvider(api_key="test-key", max_results=50)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    result = await provider.search(SearchQuery(query="dentist", location="Austin"))

    assert result.total_results == 63
    assert len({business.fsq_place_id for business in result.businesses}) == 63
    assert len(requests) == 6
    assert all(payload["maxResultCount"] == 20 for payload in requests)

    await provider.close()


async def test_search_deduplicates_place_ids_across_pages_and_queries():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get("pageToken"):
            return httpx.Response(200, json={"places": []})
        places = [
            {
                "id": "shared",
                "displayName": {"text": "Shared Business"},
                "formattedAddress": "1 Main Street",
            },
            {
                "id": f"unique-{len(requests)}",
                "displayName": {"text": "Unique Business"},
                "formattedAddress": "2 Main Street",
            },
        ]
        return httpx.Response(200, json={"places": places})

    provider = GooglePlacesProvider(api_key="test-key", max_results=3)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    result = await provider.search(
        SearchQuery(query="dentist", location="Austin", max_results=3)
    )

    assert result.total_results == 3
    assert [business.fsq_place_id for business in result.businesses] == [
        "shared",
        "unique-1",
        "unique-2",
    ]
    assert len(requests) == 3

    await provider.close()


async def test_search_subdivides_large_coordinate_radius():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        index = len(requests)
        return httpx.Response(
            200,
            json={
                "places": [
                    {
                        "id": f"place-{index}",
                        "displayName": {"text": f"Business {index}"},
                        "formattedAddress": f"{index} Main Street",
                    }
                ]
            },
        )

    provider = GooglePlacesProvider(api_key="test-key", max_results=5)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    result = await provider.search(
        SearchQuery(query="dentist", location="30.0,-97.0", radius=10000, max_results=5)
    )

    assert result.total_results == 5
    assert len(requests) == 27
    assert all("locationBias" in payload for payload in requests)
    assert len({
        (
            payload["locationBias"]["circle"]["center"]["latitude"],
            payload["locationBias"]["circle"]["center"]["longitude"],
        )
        for payload in requests
    }) == 9

    await provider.close()


def test_coordinate_search_uses_query_variations_for_each_search_area():
    provider = GooglePlacesProvider(api_key="test-key")

    payloads = provider._search_payloads(
        "dentist",
        SearchQuery(query="dentist", location="30.0,-97.0", radius=10000),
    )

    assert {payload["textQuery"] for payload in payloads} == {
        "dentist",
        "dentist businesses",
        "best dentist",
    }
    assert len(payloads) == 27


async def test_search_filters_coordinate_results_outside_requested_radius():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "places": [
                    {
                        "id": "inside",
                        "displayName": {"text": "Nearby Business"},
                        "formattedAddress": "1 Main Street",
                        "location": {"latitude": 30.005, "longitude": -97.0},
                    },
                    {
                        "id": "outside",
                        "displayName": {"text": "Distant Business"},
                        "formattedAddress": "2 Main Street",
                        "location": {"latitude": 30.02, "longitude": -97.0},
                    },
                ]
            },
        )

    provider = GooglePlacesProvider(api_key="test-key")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    result = await provider.search(
        SearchQuery(query="dentist", location="30.0,-97.0", radius=1000)
    )

    assert [business.fsq_place_id for business in result.businesses] == ["inside"]

    await provider.close()


async def test_repeated_search_uses_cached_result_without_an_api_call():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "places": [
                    {
                        "id": "cached-place",
                        "displayName": {"text": "Cached Business"},
                        "formattedAddress": "1 Main Street",
                    }
                ]
            },
        )

    provider = GooglePlacesProvider(api_key="test-key")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    query = SearchQuery(query="dentist", location="Austin", max_results=5)

    first = await provider.search(query)
    second = await provider.search(query)

    assert calls == 3
    assert [business.fsq_place_id for business in second.businesses] == [
        "cached-place"
    ]
    assert first is not second
    await provider.close()


async def test_identical_concurrent_searches_share_one_request_sequence():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "places": [
                    {
                        "id": "shared-place",
                        "displayName": {"text": "Shared Business"},
                        "formattedAddress": "1 Main Street",
                    }
                ]
            },
        )

    provider = GooglePlacesProvider(api_key="test-key")
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    query = SearchQuery(query="dentist", location="Austin", max_results=5)

    results = await asyncio.gather(
        provider.search(query), provider.search(query), provider.search(query)
    )

    assert calls == 3
    assert all(result.total_results == 1 for result in results)
    await provider.close()
