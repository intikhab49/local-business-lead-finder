import asyncio
import json
import logging
import math
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import (
    NoResultsError,
    ProviderConfigurationError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)
from app.core.rate_limiter import AdaptiveRateLimiter
from app.core.retry import retry_async
from app.providers.base import BaseProvider, Business, SearchQuery, SearchResult

logger = logging.getLogger(__name__)

_SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.nationalPhoneNumber,places.websiteUri,places.rating,"
    "places.types,places.location,places.currentOpeningHours,places.priceLevel"
)
_DETAILS_FIELD_MASK = (
    "id,displayName,formattedAddress,nationalPhoneNumber,websiteUri,"
    "rating,types,location,currentOpeningHours,priceLevel"
)

_PRICE_LEVEL_TO_INT = {
    "PRICE_LEVEL_FREE": 0,
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}

# Google Places returns at most 20 per page. Pagination via nextPageToken.
_GOOGLE_PAGE_SIZE = 20
_EARTH_RADIUS_METERS = 6_371_000


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        return None


class GooglePlacesProvider(BaseProvider):
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        max_results: int | None = None,
        limiter: AdaptiveRateLimiter | None = None,
    ):
        api_key = api_key or settings.google_places_api_key
        if not api_key:
            raise ProviderConfigurationError(
                provider="google_places",
                message="Google Places API key is required",
            )

        super().__init__(
            api_key=api_key,
            base_url=base_url or settings.google_places_base_url,
            timeout=timeout or settings.google_places_timeout,
            max_results=max_results or settings.google_places_max_results,
        )
        self._client: httpx.AsyncClient | None = None
        self._limiter = limiter or AdaptiveRateLimiter(name="google_places")

    @property
    def provider_name(self) -> str:
        return "google_places"

    @property
    def limiter(self) -> AdaptiveRateLimiter:
        return self._limiter

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "X-Goog-Api-Key": self.api_key,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                limits=httpx.Limits(
                    max_connections=10, max_keepalive_connections=5
                ),
            )
        return self._client

    async def search(self, query: SearchQuery) -> SearchResult:
        """Search with full pagination — fetches up to max_results across pages."""
        client = await self._get_client()
        url = f"{self.base_url}/places:searchText"

        effective_max = (
            min(query.max_results, self.max_results)
            if query.max_results is not None
            else self.max_results
        )

        text_query = query.keyword or query.type or query.query or ""

        payloads = self._build_search_payloads(text_query, query)
        logger.info(
            "Google Places search: query=%s max=%d slices=%d",
            text_query, effective_max, len(payloads),
        )

        all_places: dict[str, dict[str, Any]] = {}
        next_page_token: str | None = None

        for payload in payloads:
            page_payload = dict(payload)
            page_token: str | None = None
            seen_tokens: set[str] = set()

            while True:
                p = dict(page_payload)
                if page_token:
                    p["pageToken"] = page_token
                try:
                    data = await self._post_with_retries(
                        client, url,
                        json=p,
                        field_mask=_SEARCH_FIELD_MASK,
                        context=f"Google Places search '{text_query}'",
                    )
                except NoResultsError:
                    break
                except httpx.HTTPStatusError as e:
                    # Map 429/4xx/5xx onto the typed errors callers handle.
                    self._handle_error_response(e, context="search")
                except httpx.TimeoutException:
                    raise ProviderTimeoutError(
                        provider=self.provider_name, timeout=self.timeout
                    ) from None
                except httpx.RequestError as e:
                    raise ProviderError(
                        message=f"Network error during search: {e}",
                        provider=self.provider_name,
                    ) from e

                page_places = data.get("places", [])
                for place in page_places:
                    pid = place.get("id")
                    if pid:
                        all_places.setdefault(pid, place)

                if len(all_places) >= effective_max:
                    break

                page_token = data.get("nextPageToken")
                if not page_token or page_token in seen_tokens:
                    break
                seen_tokens.add(page_token)

        if len(all_places) >= effective_max:
            next_page_token = "more_available"

        places_list = list(all_places.values())[:effective_max]
        businesses = [self._parse_business(place) for place in places_list]

        logger.info(
            "Google Places returned %d businesses (next_page=%s)",
            len(businesses), next_page_token is not None,
        )

        return SearchResult(
            businesses=businesses,
            total_results=len(businesses),
            next_page_token=next_page_token,
        )

    async def search_with_token(
        self, query: SearchQuery, page_token: str
    ) -> SearchResult:
        """Continue a search using a Google nextPageToken."""
        client = await self._get_client()
        url = f"{self.base_url}/places:searchText"

        effective_max = (
            min(query.max_results, self.max_results)
            if query.max_results is not None
            else self.max_results
        )

        text_query = query.keyword or query.type or query.query or ""
        payload = {"textQuery": text_query}

        if query.location:
            coords = self._parse_coordinates(query.location)
            if coords:
                lat, lng = coords
                payload["locationBias"] = {
                    "circle": {
                        "center": {"latitude": lat, "longitude": lng},
                        "radius": query.radius,
                    }
                }

        page_payload = dict(payload)

        try:
            data = await self._post_with_retries(
                client, url,
                json=page_payload,
                field_mask=_SEARCH_FIELD_MASK,
                context=f"Google Places search '{text_query}'",
            )
        except NoResultsError:
            return SearchResult(businesses=[], total_results=0, next_page_token=None)

        places = data.get("places", [])
        next_token = data.get("nextPageToken")

        businesses = [self._parse_business(place) for place in places[:effective_max]]
        return SearchResult(
            businesses=businesses,
            total_results=len(businesses),
            next_page_token=next_token,
        )

    def _build_search_payloads(
        self, text_query: str, query: SearchQuery
    ) -> list[dict[str, Any]]:
        """Build search payloads with location bias variants for broad coverage."""
        coords = self._parse_coordinates(query.location)

        if coords is None:
            location_suffix = f" near {query.location}" if query.location else ""
            return [{"textQuery": f"{text_query}{location_suffix}"}]

        lat, lng = coords
        radius = query.radius
        base = {
            "textQuery": text_query,
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": radius,
                }
            },
        }

        results = [base]
        if radius > 5000:
            sub_radius = max(1000, min(50000, int(radius / 2)))
            offset = radius / 2
            lat_delta = offset / 111_320
            lng_delta = offset / (111_320 * max(math.cos(math.radians(lat)), 0.1))
            for row in (-1, 0, 1):
                for col in (-1, 0, 1):
                    if row == 0 and col == 0:
                        continue
                    results.append({
                        "textQuery": text_query,
                        "locationBias": {
                            "circle": {
                                "center": {
                                    "latitude": lat + row * lat_delta,
                                    "longitude": lng + col * lng_delta,
                                },
                                "radius": sub_radius,
                            }
                        },
                    })
        return results

    async def get_details(self, place_id: str) -> Business:
        client = await self._get_client()
        url = f"{self.base_url}/places/{place_id}"
        try:
            data = await self._get_with_retries(
                client, url,
                field_mask=_DETAILS_FIELD_MASK,
                context=f"Google Places details {place_id}",
            )
            return self._parse_business(data)
        except httpx.HTTPStatusError as e:
            self._handle_error_response(e, context="details")
        except httpx.TimeoutException:
            raise ProviderError(
                message=f"Google Places request timed out after {self.timeout}s",
                provider=self.provider_name,
            ) from None
        except httpx.RequestError as e:
            raise ProviderError(
                message=f"Network error fetching details: {e}",
                provider=self.provider_name,
            ) from e
        raise NoResultsError(provider=self.provider_name, query=place_id)

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}/places:searchText",
                headers={"X-Goog-FieldMask": "places.id,places.displayName"},
                json={"textQuery": "test", "maxResultCount": 1},
            )
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _parse_business(self, place: dict[str, Any]) -> Business:
        display = place.get("displayName") or {}
        loc = place.get("location") or {}
        hours_data = place.get("currentOpeningHours") or {}
        opening_hours = hours_data.get("weekdayDescriptions", [])
        price_str: str | None = place.get("priceLevel")
        price_level = (
            _PRICE_LEVEL_TO_INT[price_str] if price_str in _PRICE_LEVEL_TO_INT else None
        )
        return Business(
            fsq_place_id=place.get("id", ""),
            name=display.get("text", ""),
            formatted_address=place.get("formattedAddress", ""),
            phone_number=place.get("nationalPhoneNumber"),
            website=place.get("websiteUri"),
            rating=place.get("rating"),
            categories=list(place.get("types") or []),
            latitude=loc.get("latitude"),
            longitude=loc.get("longitude"),
            opening_hours=opening_hours,
            price_level=price_level,
        )

    # ── HTTP helpers ──────────────────────────────────────────────

    async def _post_with_retries(
        self, client: httpx.AsyncClient, url: str,
        json: dict[str, Any], field_mask: str, context: str,
    ) -> dict[str, Any]:
        async def _request() -> dict[str, Any]:
            await self._limiter.acquire()
            response = await client.post(
                url, json=json,
                headers={"X-Goog-FieldMask": field_mask},
            )
            if response.status_code == 429 or response.status_code >= 500:
                self._limiter.record_throttled(
                    _retry_after_seconds(response) if response.status_code == 429 else None
                )
            response.raise_for_status()
            self._limiter.record_success()
            data: dict[str, Any] = response.json()
            return data

        return await retry_async(_request, context=context, jitter=0.3)

    async def _get_with_retries(
        self, client: httpx.AsyncClient, url: str,
        field_mask: str, context: str,
    ) -> dict[str, Any]:
        async def _request() -> dict[str, Any]:
            await self._limiter.acquire()
            response = await client.get(
                url,
                headers={"X-Goog-FieldMask": field_mask},
            )
            if response.status_code == 429 or response.status_code >= 500:
                self._limiter.record_throttled(
                    _retry_after_seconds(response) if response.status_code == 429 else None
                )
            response.raise_for_status()
            self._limiter.record_success()
            data: dict[str, Any] = response.json()
            return data

        return await retry_async(_request, context=context, jitter=0.3)

    def _handle_error_response(
        self, error: httpx.HTTPStatusError, context: str
    ) -> None:
        status_code = error.response.status_code
        api_error: dict[str, Any] = {}
        try:
            data: dict[str, Any] = error.response.json()
            api_error = data.get("error", {}) or {}
            error_msg = str(api_error.get("message", str(error)))
        except Exception:
            error_msg = str(error)

        if status_code in (401, 403):
            raise ProviderError(
                message=f"Invalid Google Places API key: {error_msg}",
                provider=self.provider_name, status_code=502,
            )
        if status_code == 429:
            retry_after = int(error.response.headers.get("Retry-After", 60))
            raise RateLimitError(self.provider_name, retry_after)
        if status_code == 404:
            raise NoResultsError(provider=self.provider_name, query=context)
        if status_code >= 500:
            raise ProviderError(
                message=f"Google Places API server error: {error_msg}",
                provider=self.provider_name, status_code=502,
            )
        raise ProviderError(
            message=f"Google Places API error ({status_code}): {error_msg}",
            provider=self.provider_name, status_code=502,
        )

    @staticmethod
    def _parse_coordinates(location: str | None) -> tuple[float, float] | None:
        if not location or "," not in location:
            return None
        parts = [part.strip() for part in location.split(",")]
        if len(parts) != 2:
            return None
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            return None