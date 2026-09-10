import logging
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
from app.core.retry import retry_async
from app.monitoring.metrics import get_metrics
from app.providers.base import BaseProvider, Business, SearchQuery, SearchResult

logger = logging.getLogger(__name__)


class FoursquareProvider(BaseProvider):
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        max_results: int | None = None,
    ):
        api_key = api_key or settings.foursquare_api_key
        if not api_key:
            raise ProviderConfigurationError(
                provider="foursquare",
                message="Foursquare API key is required",
            )

        super().__init__(
            api_key=api_key,
            base_url=base_url or settings.foursquare_base_url,
            timeout=timeout or settings.foursquare_timeout,
            max_results=max_results or settings.foursquare_max_results,
        )
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_name(self) -> str:
        return "foursquare"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                    "X-Places-Api-Version": "2025-06-17",
                },
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    async def search(self, query: SearchQuery) -> SearchResult:
        params: dict[str, Any] = {
            "query": query.query or query.type or "",
            "limit": min(query.max_results, self.max_results),
        }
        if query.keyword:
            params["query"] = query.keyword
        if query.location:
            if "," in query.location and all(
                part.strip().replace("-", "").replace(".", "").isdigit()
                for part in query.location.split(",")
            ):
                params["ll"] = query.location
                if query.radius:
                    params["radius"] = min(query.radius, 100000)
            else:
                params["near"] = query.location

        url = f"{self.base_url}/search"

        return await self._execute_search(url, params, query)

    async def get_details(self, place_id: str) -> Business:
        client = await self._get_client()
        url = f"{self.base_url}/{place_id}"

        try:
            logger.info("Fetching details for place: %s", place_id)
            data = await self._get_json_with_retries(
                client,
                url,
                params=None,
                context=f"Foursquare details {place_id}",
            )
            return self._parse_business(data)
        except httpx.TimeoutException as e:
            logger.error("Details request timed out for place: %s", place_id)
            raise ProviderTimeoutError(self.provider_name, self.timeout) from e
        except httpx.HTTPStatusError as e:
            self._handle_error_response(e)
        except httpx.RequestError as e:
            logger.error("Network error fetching details for %s: %s", place_id, e)
            raise ProviderError(
                message=f"Network error fetching details: {e}",
                provider=self.provider_name,
            ) from e

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            response = await client.get(
                f"{self.base_url}/search",
                params={"query": "test", "limit": 1},
            )
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _execute_search(
        self, url: str, params: dict[str, Any], query: SearchQuery
    ) -> SearchResult:
        client = await self._get_client()

        try:
            logger.info(
                "Searching Foursquare: query=%s location=%s limit=%d",
                params.get("query", ""),
                params.get("near", ""),
                params.get("limit", self.max_results),
            )
            data = await self._get_json_with_retries(
                client,
                url,
                params=params,
                context=f"Foursquare search {params.get('query', '')} near {params.get('near', '')}",
            )
        except httpx.TimeoutException:
            logger.error("Foursquare search request timed out")
            raise ProviderTimeoutError(self.provider_name, self.timeout) from None
        except httpx.HTTPStatusError as e:
            self._handle_error_response(e)
        except httpx.RequestError as e:
            logger.error("Network error calling Foursquare API: %s", e)
            raise ProviderError(
                message=f"Network error: {e}",
                provider=self.provider_name,
            ) from e

        results = data.get("results", [])
        if not results:
            search_desc = f"{params.get('query', '')} near {params.get('near', '')}"
            logger.warning("No results found: %s", search_desc)
            raise NoResultsError(provider=self.provider_name, query=search_desc)

        businesses = [self._parse_business(place) for place in results]
        logger.info("Foursquare search returned %d results", len(businesses))
        return SearchResult(businesses=businesses, total_results=len(businesses))

    async def _get_json_with_retries(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, Any] | None,
        context: str,
    ) -> dict[str, Any]:
        metrics = get_metrics()

        async def _request() -> dict[str, Any]:
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
            except Exception:
                metrics.increment("api_failures")
                raise
            metrics.increment("api_calls")
            return response.json()

        return await retry_async(_request, context=context)

    def _parse_business(self, place: dict) -> Business:
        loc = place.get("location", {})
        categories = [
            c.get("name", "")
            for c in place.get("categories", [])
            if c.get("name")
        ]
        hours_data = place.get("hours", {})
        opening_hours = hours_data.get("display", []) if hours_data else []

        return Business(
            fsq_place_id=place.get("fsq_place_id", ""),
            name=place.get("name", ""),
            formatted_address=loc.get("formatted_address", ""),
            phone_number=place.get("tel"),
            website=place.get("website"),
            rating=place.get("rating"),
            categories=categories,
            latitude=place.get("latitude"),
            longitude=place.get("longitude"),
            opening_hours=opening_hours,
            price_level=place.get("price"),
        )

    def _handle_error_response(self, error: httpx.HTTPStatusError) -> None:
        status_code = error.response.status_code
        try:
            data = error.response.json()
            error_msg = data.get("message", str(error))
        except Exception:
            error_msg = str(error)

        if status_code == 401:
            logger.error("Invalid Foursquare API key (401)")
            raise ProviderError(
                message="Invalid Foursquare API key",
                provider=self.provider_name,
                status_code=502,
            )
        if status_code == 429:
            retry_after = int(error.response.headers.get("Retry-After", 60))
            raise RateLimitError(self.provider_name, retry_after)
        if status_code == 404:
            logger.warning("Place not found (404) for requested resource")
            raise NoResultsError(
                provider=self.provider_name,
                query=error.request.url.path.split("/")[-1],
            )
        if status_code >= 500:
            raise ProviderError(
                message=f"Foursquare API server error: {error_msg}",
                provider=self.provider_name,
                status_code=502,
            )

        raise ProviderError(
            message=f"Foursquare API error ({status_code}): {error_msg}",
            provider=self.provider_name,
            status_code=502,
        )
