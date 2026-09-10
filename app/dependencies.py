from functools import lru_cache

from app.core.config import settings
from app.providers.base import BaseProvider
from app.providers.foursquare_provider import FoursquareProvider
from app.providers.geoapify_provider import GeoapifyProvider
from app.providers.google_places_provider import GooglePlacesProvider
from app.providers.osm_provider import OSMProvider
from app.services.business_search_service import BusinessSearchService

_PROVIDERS: dict[str, type[BaseProvider]] = {
    "foursquare": FoursquareProvider,
    "geoapify": GeoapifyProvider,
    "google_places": GooglePlacesProvider,
    "osm": OSMProvider,
}

# Providers that need no API key and can always stand in as a fallback.
FREE_PROVIDERS = ("osm",)


def available_providers() -> dict[str, bool]:
    """Map provider name -> whether it is configured and usable right now."""
    return {
        "google_places": bool(settings.google_places_api_key),
        "foursquare": bool(settings.foursquare_api_key),
        "geoapify": bool(settings.geoapify_api_key),
        "osm": True,  # free, keyless
    }


@lru_cache
def build_provider(name: str) -> BaseProvider:
    """Return the (cached) provider instance for a name."""
    provider_cls = _PROVIDERS.get(name.lower(), GooglePlacesProvider)
    return provider_cls()


@lru_cache
def get_provider() -> BaseProvider:
    """The primary provider, falling back to OSM when it is not configured."""
    name = settings.provider_name.lower()
    if not available_providers().get(name, False):
        return build_provider("osm")
    return build_provider(name)


@lru_cache
def get_fallback_provider() -> BaseProvider | None:
    """The free/open-data provider used when the primary fails or is throttled."""
    name = (settings.fallback_provider_name or "").lower()
    if name in ("", "none", "off", "disabled"):
        return None
    if not available_providers().get(name, False):
        return None
    primary = get_provider()
    if primary.provider_name == name:
        return None
    return build_provider(name)


def resolve_providers(
    primary_name: str | None = None,
    fallback_name: str | None = None,
) -> tuple[BaseProvider, BaseProvider | None]:
    """Resolve an explicit (primary, fallback) pair, honouring config defaults.

    Unconfigured names silently degrade to what is actually usable, so a search
    never dies just because a key is missing.
    """
    available = available_providers()

    if primary_name and available.get(primary_name.lower()):
        primary = build_provider(primary_name)
    else:
        primary = get_provider()

    if fallback_name is None:
        fallback = get_fallback_provider()
    elif fallback_name.lower() in ("none", "off", "", "disabled"):
        fallback = None
    elif available.get(fallback_name.lower()):
        fallback = build_provider(fallback_name)
    else:
        fallback = None

    if fallback is not None and fallback.provider_name == primary.provider_name:
        fallback = None
    return primary, fallback


_business_search_service: BusinessSearchService | None = None


def get_business_search_service() -> BusinessSearchService:
    global _business_search_service
    if _business_search_service is None:
        _business_search_service = BusinessSearchService(get_provider())
    return _business_search_service
