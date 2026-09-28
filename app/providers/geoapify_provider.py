import asyncio
import logging
import math
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import (
    NoResultsError,
    ProviderConfigurationError,
    ProviderError,
    RateLimitError,
)
from app.core.retry import retry_async
from app.providers.base import BaseProvider, Business, SearchQuery, SearchResult

logger = logging.getLogger(__name__)

_GEOAPIFY_PAGE_SIZE = 20


class GeoapifyProvider(BaseProvider):
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        max_results: int | None = None,
    ):
        api_key = api_key or settings.geoapify_api_key
        if not api_key:
            raise ProviderConfigurationError(
                provider="geoapify",
                message="Geoapify API key is required",
            )

        super().__init__(
            api_key=api_key,
            base_url=base_url or "https://api.geoapify.com",
            timeout=timeout or 30,
            max_results=max_results or settings.geoapify_max_results,
        )
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_name(self) -> str:
        return "geoapify"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={"Accept": "application/json"},
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    async def search(self, query: SearchQuery) -> SearchResult:
        client = await self._get_client()

        effective_max = (
            min(query.max_results, self.max_results)
            if query.max_results is not None
            else self.max_results
        )

        category = self._map_category(query.type or query.query or "")
        coords = self._parse_coordinates(query.location)

        if not coords and query.location:
            coords = await self._geocode(client, query.location)

        if not coords:
            businesses = await self._search_by_text(client, query, category, effective_max)
        else:
            businesses = await self._search_by_geo(client, coords, query.radius, category, effective_max)

        return SearchResult(
            businesses=businesses,
            total_results=len(businesses),
            next_page_token=None,
        )

    async def _search_by_geo(
        self,
        client: httpx.AsyncClient,
        coords: tuple[float, float],
        radius: int,
        category: str,
        max_results: int,
    ) -> list[Business]:
        lon, lat = coords
        params: dict[str, Any] = {
            "apiKey": self.api_key,
            "categories": category,
            "filter": f"circle:{lon},{lat},{radius}",
            "limit": min(max_results, 500),
        }

        places = await self._fetch_all_pages(client, params, max_results)
        return [self._parse_business(p) for p in places]

    async def _search_by_text(
        self,
        client: httpx.AsyncClient,
        query: SearchQuery,
        category: str,
        max_results: int,
    ) -> list[Business]:
        params: dict[str, Any] = {
            "apiKey": self.api_key,
            "categories": category,
            "filter": f"place:{query.location}" if query.location else "",
            "limit": min(max_results, 500),
        }
        if not params["filter"]:
            del params["filter"]

        places = await self._fetch_all_pages(client, params, max_results)
        return [self._parse_business(p) for p in places]

    async def _fetch_all_pages(
        self,
        client: httpx.AsyncClient,
        params: dict[str, Any],
        max_results: int,
    ) -> list[dict[str, Any]]:
        url = f"{self.base_url}/v2/places"
        all_places: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        offset = 0

        while len(all_places) < max_results:
            page_params = dict(params)
            page_params["offset"] = offset
            page_params["limit"] = min(_GEOAPIFY_PAGE_SIZE, max_results - len(all_places))

            try:
                data = await retry_async(
                    lambda: self._get_page(client, url, page_params),
                    context=f"Geoapify search offset={offset}",
                )
            except NoResultsError:
                break

            features = data.get("features", [])
            if not features:
                break

            for feature in features:
                props = feature.get("properties") or {}
                pid = props.get("place_id", "")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    all_places.append(feature)

            if len(features) < _GEOAPIFY_PAGE_SIZE:
                break

            offset += _GEOAPIFY_PAGE_SIZE

        return all_places[:max_results]

    async def _get_page(
        self, client: httpx.AsyncClient, url: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        response = await client.get(url, params=params)
        if response.status_code == 429:
            raise RateLimitError(self.provider_name)
        if response.status_code == 400:
            raise NoResultsError(provider=self.provider_name, query=str(params.get("categories", "")))
        if response.status_code >= 500:
            raise ProviderError(
                message=f"Geoapify server error ({response.status_code})",
                provider=self.provider_name,
            )
        response.raise_for_status()
        return response.json()

    async def get_details(self, place_id: str) -> Business:
        client = await self._get_client()
        url = f"{self.base_url}/v2/place-details"
        params = {"apiKey": self.api_key, "id": place_id}

        try:
            data = await retry_async(
                lambda: self._get_page(client, url, params),
                context=f"Geoapify details {place_id}",
            )
            props = data.get("properties") or {}
            return self._parse_business_from_props(props)
        except NoResultsError:
            raise
        except Exception as e:
            raise ProviderError(
                message=f"Failed to fetch place details: {e}",
                provider=self.provider_name,
            ) from e

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            response = await client.get(
                f"{self.base_url}/v2/places",
                params={
                    "apiKey": self.api_key,
                    "categories": "catering.restaurant",
                    "filter": "circle:-87.6298,41.8781,100",
                    "limit": 1,
                },
                timeout=5,
            )
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _parse_business(self, feature: dict[str, Any]) -> Business:
        props = feature.get("properties") or {}
        return self._parse_business_from_props(props)

    @staticmethod
    def _parse_business_from_props(props: dict[str, Any]) -> Business:
        categories = props.get("categories", [])
        hours = props.get("opening_hours", "")

        return Business(
            fsq_place_id=props.get("place_id", ""),
            name=props.get("name", ""),
            formatted_address=props.get("formatted", ""),
            phone_number=props.get("phone") or (
                (props.get("contact") or {}).get("phone") if isinstance(props.get("contact"), dict) else None
            ),
            website=props.get("website") or (
                (props.get("contact") or {}).get("website") if isinstance(props.get("contact"), dict) else None
            ),
            rating=None,
            categories=categories if isinstance(categories, list) else [],
            latitude=props.get("lat"),
            longitude=props.get("lon"),
            opening_hours=[hours] if hours else [],
            price_level=None,
        )

    @staticmethod
    def _map_category(category: str) -> str:
        # Every value checked against the live Places API on 2026-09-29: an
        # unsupported category is a 400, which reads as "no results".
        mapping = {
            "restaurant": "catering.restaurant",
            "restaurants": "catering.restaurant",
            "cafe": "catering.cafe",
            "bar": "catering.bar",
            "bar and restaurant": "catering.bar",
            "pub": "catering.pub",
            "fast food": "catering.fast_food",
            "bakery": "commercial.food_and_drink.bakery",
            "gym": "sport.fitness",
            "fitness": "sport.fitness",
            "fitness center": "sport.fitness",
            "gym and fitness": "sport.fitness",
            "hotel": "accommodation.hotel",
            "motel": "accommodation.motel",
            "supermarket": "commercial.supermarket",
            "grocery": "commercial.supermarket",
            "pharmacy": "healthcare.pharmacy",
            "dentist": "healthcare.dentist",
            "doctor": "healthcare.clinic_or_praxis",
            "hospital": "healthcare.hospital",
            "clinic": "healthcare.clinic_or_praxis",
            "school": "education.school",
            "bank": "service.financial.bank",
            "gas station": "service.vehicle.fuel",
            "car repair": "service.vehicle.repair",
            "car wash": "service.vehicle.car_wash",
            "lawyer": "office.lawyer",
            "insurance": "office.insurance",
        }
        lower = category.strip().lower()
        if lower in mapping:
            return mapping[lower]
        return lower.replace(" ", "_").lower()

    @staticmethod
    def _parse_coordinates(location: str | None) -> tuple[float, float] | None:
        """Parse a "lat,lng" string (the grid's cell format) into (lon, lat)."""
        if not location or "," not in location:
            return None
        parts = [part.strip() for part in location.split(",")]
        if len(parts) != 2:
            return None
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        return lon, lat

    async def _geocode(self, client: httpx.AsyncClient, location: str) -> tuple[float, float] | None:
        """Convert a city name like 'Chicago, IL' to coordinates."""
        url = f"{self.base_url}/v1/geocode/search"
        params = {"apiKey": self.api_key, "text": location, "limit": 1}
        try:
            data = await retry_async(
                lambda: self._get_page(client, url, params),
                context=f"Geocode {location}",
            )
            features = data.get("features", [])
            if features:
                props = features[0].get("properties") or {}
                lon = props.get("lon")
                lat = props.get("lat")
                if lon is not None and lat is not None:
                    return float(lon), float(lat)
        except Exception:
            logger.warning("Geocoding failed for: %s", location)
        return None