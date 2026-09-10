"""OpenStreetMap / Overpass provider — free, no API key, no result cap.

Used as the open-data fallback for Google Places. Overpass returns every POI
matching the tag filter inside a radius (not a top-20 page), so it is the piece
that removes the per-search result ceiling; the trade-off is that coverage and
contact data depend on what mappers have entered.

Both Overpass and Nominatim are volunteer-run: this provider paces itself with
an AdaptiveRateLimiter, sends a descriptive User-Agent, and rotates mirrors when
one endpoint is busy.
"""
from __future__ import annotations

import logging
import re

import httpx

from app.core.config import settings
from app.core.exceptions import (
    NoResultsError,
    ProviderError,
    RateLimitError,
)
from app.core.rate_limiter import AdaptiveRateLimiter
from app.core.retry import retry_async
from app.providers.base import BaseProvider, Business, SearchQuery, SearchResult

logger = logging.getLogger(__name__)

# Common lead categories mapped to the OSM tags mappers actually use.
_CATEGORY_TAGS: dict[str, list[tuple[str, str]]] = {
    "restaurant": [("amenity", "restaurant")],
    "cafe": [("amenity", "cafe")],
    "coffee": [("amenity", "cafe")],
    "bar": [("amenity", "bar"), ("amenity", "pub")],
    "pub": [("amenity", "pub")],
    "fast food": [("amenity", "fast_food")],
    "bakery": [("shop", "bakery")],
    "hotel": [("tourism", "hotel"), ("tourism", "motel"), ("tourism", "guest_house")],
    "gym": [("leisure", "fitness_centre"), ("leisure", "sports_centre")],
    "fitness": [("leisure", "fitness_centre")],
    "dentist": [("amenity", "dentist"), ("healthcare", "dentist")],
    "doctor": [("amenity", "doctors"), ("healthcare", "doctor")],
    "clinic": [("amenity", "clinic"), ("healthcare", "clinic")],
    "pharmacy": [("amenity", "pharmacy")],
    "veterinary": [("amenity", "veterinary")],
    "hospital": [("amenity", "hospital")],
    "lawyer": [("office", "lawyer")],
    "attorney": [("office", "lawyer")],
    "accountant": [("office", "accountant")],
    "insurance": [("office", "insurance")],
    "real estate": [("office", "estate_agent")],
    "estate agent": [("office", "estate_agent")],
    "bank": [("amenity", "bank")],
    "salon": [("shop", "hairdresser"), ("shop", "beauty")],
    "hair": [("shop", "hairdresser")],
    "barber": [("shop", "hairdresser")],
    "spa": [("leisure", "spa"), ("shop", "beauty")],
    "car repair": [("shop", "car_repair")],
    "auto repair": [("shop", "car_repair")],
    "mechanic": [("shop", "car_repair")],
    "car dealer": [("shop", "car")],
    "plumber": [("craft", "plumber")],
    "electrician": [("craft", "electrician")],
    "carpenter": [("craft", "carpenter")],
    "roofer": [("craft", "roofer")],
    "hvac": [("craft", "hvac")],
    "builder": [("craft", "builder")],
    "contractor": [("craft", "builder"), ("office", "construction_company")],
    "cleaning": [("shop", "laundry"), ("craft", "cleaning")],
    "laundry": [("shop", "laundry"), ("shop", "dry_cleaning")],
    "florist": [("shop", "florist")],
    "furniture": [("shop", "furniture")],
    "supermarket": [("shop", "supermarket")],
    "grocery": [("shop", "supermarket"), ("shop", "convenience")],
    "clothing": [("shop", "clothes")],
    "jewelry": [("shop", "jewelry")],
    "optician": [("shop", "optician")],
    "pet": [("shop", "pet")],
    "hardware": [("shop", "hardware"), ("shop", "doityourself")],
    "school": [("amenity", "school")],
    "childcare": [("amenity", "kindergarten"), ("amenity", "childcare")],
    "gas station": [("amenity", "fuel")],
    "car wash": [("amenity", "car_wash")],
    "marketing": [("office", "advertising_agency")],
    "advertising": [("office", "advertising_agency")],
    "it": [("office", "it")],
    "software": [("office", "it")],
    "consulting": [("office", "consulting")],
    "architect": [("office", "architect")],
    "travel agency": [("shop", "travel_agency")],
    "tattoo": [("shop", "tattoo")],
    "nail": [("shop", "beauty")],
    "photographer": [("craft", "photographer"), ("shop", "photo")],
}

# Keys searched by value regex when a category is not in the table above.
_GENERIC_KEYS = "amenity|shop|office|craft|leisure|healthcare|tourism|club"

_PHONE_KEYS = ("phone", "contact:phone", "contact:mobile", "telephone")
_WEBSITE_KEYS = ("website", "contact:website", "url", "contact:url")
_EMAIL_KEYS = ("email", "contact:email")
_TYPE_PREFIX = {"node": "n", "way": "w", "relation": "r"}


def normalize_category(category: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (category or "").lower()).strip()


def build_overpass_filters(category: str) -> list[str]:
    """Return Overpass tag filters for a free-text category."""
    normalized = normalize_category(category)
    if not normalized:
        return [f'nwr[~"^({_GENERIC_KEYS})$"~"."]']

    tags = _CATEGORY_TAGS.get(normalized)
    if tags is None:
        # Try the longest matching known category contained in the input,
        # e.g. "italian restaurant" -> restaurant.
        matches = [
            key for key in _CATEGORY_TAGS
            if re.search(rf"\b{re.escape(key)}\b", normalized)
        ]
        if matches:
            tags = _CATEGORY_TAGS[max(matches, key=len)]

    if tags:
        return [f'nwr["{key}"="{value}"]' for key, value in tags]

    # Unknown category: match the value of any business-ish key, plus names.
    escaped = re.escape(normalized).replace("\\ ", "[ _-]?")
    return [
        f'nwr[~"^({_GENERIC_KEYS})$"~"{escaped}",i]',
        f'nwr["name"~"{escaped}",i][~"^({_GENERIC_KEYS})$"~"."]',
    ]


class OSMProvider(BaseProvider):
    """Overpass-backed provider. Free, keyless, uncapped per query."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        max_results: int | None = None,
        limiter: AdaptiveRateLimiter | None = None,
    ) -> None:
        endpoints = list(settings.osm_overpass_urls)
        super().__init__(
            api_key=api_key or "",
            base_url=base_url or endpoints[0],
            timeout=timeout or settings.osm_timeout,
            max_results=max_results or settings.osm_max_results,
        )
        self._endpoints = [base_url] if base_url else endpoints
        self._endpoint_idx = 0
        self._client: httpx.AsyncClient | None = None
        self._limiter = limiter or AdaptiveRateLimiter(
            min_interval=settings.osm_min_interval,
            name="overpass",
        )
        self._geocode_cache: dict[str, tuple[float, float] | None] = {}

    @property
    def provider_name(self) -> str:
        return "osm"

    @property
    def limiter(self) -> AdaptiveRateLimiter:
        return self._limiter

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "User-Agent": settings.osm_user_agent,
                    "Accept": "application/json",
                },
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                follow_redirects=True,
            )
        return self._client

    # ── search ────────────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> SearchResult:
        effective_max = min(query.max_results or self.max_results, self.max_results)
        category = query.keyword or query.type or query.query or ""

        coords = parse_coordinates(query.location)
        if coords is None and query.location:
            coords = await self.geocode(query.location)
        if coords is None:
            raise ProviderError(
                message=(
                    "OpenStreetMap search needs a location it can place on the map. "
                    f"Could not geocode {query.location!r}."
                ),
                provider=self.provider_name,
            )

        lat, lng = coords
        overpass_ql = self._build_query(category, lat, lng, query.radius, effective_max)
        data = await self._post_overpass(overpass_ql, context=f"osm search '{category}'")

        businesses: list[Business] = []
        seen: set[str] = set()
        for element in data.get("elements", []):
            business = self._parse_element(element)
            if business is None or business.fsq_place_id in seen:
                continue
            if query.min_rating is not None and (business.rating or 0) < query.min_rating:
                continue
            seen.add(business.fsq_place_id)
            businesses.append(business)
            if len(businesses) >= effective_max:
                break

        if not businesses:
            raise NoResultsError(provider=self.provider_name, query=category)

        logger.info(
            "[OSM] %s near %.4f,%.4f r=%dm -> %d businesses",
            category, lat, lng, query.radius, len(businesses),
        )
        return SearchResult(
            businesses=businesses,
            total_results=len(businesses),
            next_page_token=None,
        )

    def _build_query(
        self, category: str, lat: float, lng: float, radius: int, limit: int
    ) -> str:
        filters = build_overpass_filters(category)
        radius = max(50, min(int(radius), 50_000))
        body = "\n  ".join(
            f'{f}(around:{radius},{lat:.6f},{lng:.6f});' for f in filters
        )
        timeout = max(25, min(self.timeout, 180))
        return (
            f"[out:json][timeout:{timeout}];\n"
            f"(\n  {body}\n);\n"
            f"out center tags {limit};"
        )

    async def get_details(self, place_id: str) -> Business:
        parsed = parse_osm_id(place_id)
        if parsed is None:
            raise NoResultsError(provider=self.provider_name, query=place_id)
        osm_type, osm_id = parsed
        overpass_ql = (
            f"[out:json][timeout:25];\n{osm_type}({osm_id});\nout center tags 1;"
        )
        data = await self._post_overpass(overpass_ql, context=f"osm details {place_id}")
        for element in data.get("elements", []):
            business = self._parse_element(element)
            if business is not None:
                return business
        raise NoResultsError(provider=self.provider_name, query=place_id)

    async def geocode(self, location: str) -> tuple[float, float] | None:
        """Resolve a text location with Nominatim (cached, rate-limited)."""
        key = location.strip().lower()
        if not key:
            return None
        if key in self._geocode_cache:
            return self._geocode_cache[key]

        client = await self._get_client()

        async def _request() -> httpx.Response:
            await self._limiter.acquire()
            response = await client.get(
                settings.osm_nominatim_url,
                params={"q": location, "format": "json", "limit": 1},
            )
            response.raise_for_status()
            return response

        try:
            response = await retry_async(
                _request, context=f"nominatim geocode {location!r}", jitter=0.3
            )
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 — geocoding is best-effort
            logger.warning("[OSM] geocoding failed for %r: %s", location, exc)
            return None

        coords: tuple[float, float] | None = None
        if isinstance(payload, list) and payload:
            try:
                coords = (float(payload[0]["lat"]), float(payload[0]["lon"]))
            except (KeyError, TypeError, ValueError):
                coords = None
        self._geocode_cache[key] = coords
        self._limiter.record_success()
        return coords

    async def health_check(self) -> bool:
        try:
            data = await self._post_overpass(
                "[out:json][timeout:10];node(1);out ids 1;",
                context="osm health",
            )
            return "elements" in data
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── HTTP ──────────────────────────────────────────────────────

    async def _post_overpass(self, overpass_ql: str, context: str) -> dict:
        client = await self._get_client()
        attempts = max(1, len(self._endpoints))
        last_error: Exception | None = None

        for _ in range(attempts):
            endpoint = self._endpoints[self._endpoint_idx % len(self._endpoints)]

            async def _request(url: str = endpoint) -> dict:
                await self._limiter.acquire()
                response = await client.post(url, content=overpass_ql.encode("utf-8"))
                if response.status_code in (429, 504):
                    retry_after = _retry_after_seconds(response)
                    self._limiter.record_throttled(retry_after)
                    raise RateLimitError(self.provider_name, int(retry_after or 30))
                response.raise_for_status()
                return response.json()

            try:
                data = await retry_async(_request, context=context, jitter=0.3)
                self._limiter.record_success()
                return data
            except RateLimitError as exc:
                last_error = exc
                self._endpoint_idx += 1  # try the next mirror
                logger.warning("[OSM] %s throttled, rotating mirror", endpoint)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                self._limiter.record_error()
                self._endpoint_idx += 1
                logger.warning(
                    "[OSM] %s returned %s, rotating mirror",
                    endpoint, exc.response.status_code,
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                last_error = exc
                self._limiter.record_error()
                self._endpoint_idx += 1
                logger.warning("[OSM] %s network error: %s", endpoint, exc)
            except ValueError as exc:  # malformed JSON — usually an HTML error page
                last_error = exc
                self._limiter.record_error()
                self._endpoint_idx += 1

        if isinstance(last_error, RateLimitError):
            raise last_error
        raise ProviderError(
            message=f"All Overpass endpoints failed: {last_error}",
            provider=self.provider_name,
            status_code=502,
        )

    # ── parsing ───────────────────────────────────────────────────

    def _parse_element(self, element: dict) -> Business | None:
        tags = element.get("tags") or {}
        name = (tags.get("name") or tags.get("operator") or "").strip()
        if not name:
            return None

        osm_type = element.get("type", "node")
        osm_id = element.get("id")
        if osm_id is None:
            return None
        place_id = f"osm:{_TYPE_PREFIX.get(osm_type, 'n')}{osm_id}"

        center = element.get("center") or {}
        lat = element.get("lat", center.get("lat"))
        lon = element.get("lon", center.get("lon"))

        categories = [
            f"{key}:{tags[key]}"
            for key in ("amenity", "shop", "office", "craft", "leisure", "healthcare", "tourism")
            if tags.get(key)
        ]

        business = Business(
            fsq_place_id=place_id,
            name=name,
            formatted_address=_format_address(tags),
            phone_number=_first_tag(tags, _PHONE_KEYS),
            website=_first_tag(tags, _WEBSITE_KEYS),
            rating=None,  # OSM has no ratings
            categories=categories,
            latitude=float(lat) if lat is not None else None,
            longitude=float(lon) if lon is not None else None,
            opening_hours=[tags["opening_hours"]] if tags.get("opening_hours") else [],
            price_level=None,
        )
        email = _first_tag(tags, _EMAIL_KEYS)
        if email:
            # OSM sometimes has a contact e-mail; scraping fills in the rest.
            business.email = email
        return business


def _first_tag(tags: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = tags.get(key)
        if value:
            return str(value).split(";")[0].strip()
    return None


def _format_address(tags: dict) -> str:
    house = tags.get("addr:housenumber", "")
    street = tags.get("addr:street", "")
    line1 = " ".join(part for part in (house, street) if part).strip()
    rest = [tags.get("addr:city", ""), tags.get("addr:state", ""), tags.get("addr:postcode", "")]
    parts = [line1] + [part for part in rest if part]
    return ", ".join(part for part in parts if part)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        return None


def parse_coordinates(location: str | None) -> tuple[float, float] | None:
    if not location or "," not in location:
        return None
    parts = [part.strip() for part in location.split(",")]
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def parse_osm_id(place_id: str) -> tuple[str, int] | None:
    """'osm:n12345' -> ('node', 12345)."""
    match = re.fullmatch(r"osm:([nwr])(\d+)", (place_id or "").strip())
    if not match:
        return None
    kind = {"n": "node", "w": "way", "r": "relation"}[match.group(1)]
    return kind, int(match.group(2))


__all__ = [
    "OSMProvider",
    "build_overpass_filters",
    "normalize_category",
    "parse_coordinates",
    "parse_osm_id",
]
