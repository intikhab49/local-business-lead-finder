"""Data-source settings: which provider searches, and the API keys it uses.

Keys are written to .env so they survive a restart, and applied to the running
process at once, so nobody has to edit files or restart the server.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import dependencies
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

ENV_PATH = Path(".env")

# provider -> (Settings attribute, .env variable)
_KEY_FIELDS: dict[str, tuple[str, str]] = {
    "google_places": ("google_places_api_key", "GOOGLE_PLACES_API_KEY"),
    "geoapify": ("geoapify_api_key", "GEOAPIFY_API_KEY"),
}

_GOOGLE_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_GEOAPIFY_GEOCODE_URL = "https://api.geoapify.com/v1/geocode/search"


class SettingsUpdate(BaseModel):
    provider_name: Literal["google_places", "geoapify", "osm"] | None = None
    google_places_api_key: str | None = None
    geoapify_api_key: str | None = None


class KeyTestRequest(BaseModel):
    provider: Literal["google_places", "geoapify"]
    # Omit to test the key that is already saved.
    api_key: str | None = None


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"message": message, "status_code": 400})


def _mask(key: str | None) -> str | None:
    return f"…{key[-4:]}" if key else None


def _current() -> dict[str, Any]:
    return {
        # The provider searches really use: a source without its key falls back to osm.
        "provider_name": dependencies.get_provider().provider_name,
        "keys": {
            provider: {"set": bool(getattr(settings, attr)), "hint": _mask(getattr(settings, attr))}
            for provider, (attr, _) in _KEY_FIELDS.items()
        },
    }


def _write_env(updates: dict[str, str]) -> None:
    """Replace or append NAME=value lines, leaving every other line untouched."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0].strip()
        if name in remaining:
            lines[i] = f"{name}={remaining.pop(name)}"
    lines.extend(f"{name}={value}" for name, value in remaining.items())
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _reset_providers() -> None:
    # Providers read their key when built, so drop the cached instances.
    # Jobs already running keep the instances they hold.
    dependencies.build_provider.cache_clear()
    dependencies.get_provider.cache_clear()
    dependencies.get_fallback_provider.cache_clear()
    dependencies._business_search_service = None


@router.get("", summary="Current data source and which keys are saved")
async def get_settings() -> dict[str, Any]:
    return _current()


@router.put("", summary="Choose the data source and save API keys")
async def update_settings(body: SettingsUpdate) -> dict[str, Any]:
    env_updates: dict[str, str] = {}
    for attr, env_name in _KEY_FIELDS.values():
        value = getattr(body, attr)
        if value is not None:
            value = value.strip()
            setattr(settings, attr, value or None)
            env_updates[env_name] = value

    if body.provider_name:
        needs_key = _KEY_FIELDS.get(body.provider_name)
        if needs_key and not getattr(settings, needs_key[0]):
            raise _bad_request("Add an API key for this data source before selecting it.")
        settings.provider_name = body.provider_name
        env_updates["PROVIDER_NAME"] = body.provider_name

    if env_updates:
        _write_env(env_updates)
        _reset_providers()
        logger.info("Settings updated: %s", ", ".join(sorted(env_updates)))
    return _current()


@router.post("/test", summary="Check an API key with one real request")
async def test_key(body: KeyTestRequest) -> dict[str, Any]:
    key = (body.api_key or "").strip() or getattr(settings, _KEY_FIELDS[body.provider][0])
    if not key:
        raise _bad_request("No key entered and none saved yet.")
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            if body.provider == "google_places":
                ok, message = await _test_google(client, key)
            else:
                ok, message = await _test_geoapify(client, key)
        except httpx.HTTPError as exc:
            ok, message = False, f"Could not reach the provider: {exc.__class__.__name__}. Check the internet connection."
    return {"provider": body.provider, "ok": ok, "message": message}


async def _test_google(client: httpx.AsyncClient, key: str) -> tuple[bool, str]:
    # An ID-only field mask keeps the check on Google's free tier.
    resp = await client.post(
        _GOOGLE_SEARCH_URL,
        headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "places.id"},
        json={"textQuery": "coffee in London", "pageSize": 1},
    )
    if resp.status_code == 200:
        return True, "Google key works."

    error = _json(resp).get("error") or {}
    message = error.get("message") or f"HTTP {resp.status_code}"
    lowered = message.lower()
    if "api key not valid" in lowered:
        return False, "This key is not valid. Copy it again from Google Cloud → Credentials."
    if "has not been used" in lowered or "is disabled" in lowered:
        return False, "Places API (New) is not enabled for this key's project. Enable it in Google Cloud → APIs & Services → Library."
    if resp.status_code == 403:
        # Places (New) only says "caller does not have permission"; the
        # Geocoding API names the real cause, which is usually billing.
        geo = _json(await client.get(_GOOGLE_GEOCODE_URL, params={"address": "London", "key": key}))
        if "billing" in (geo.get("error_message") or "").lower():
            return False, "Billing is not enabled on this key's Google Cloud project. Link a billing account at console.cloud.google.com/billing, then test again."
    return False, f"Google refused the key: {message}"


async def _test_geoapify(client: httpx.AsyncClient, key: str) -> tuple[bool, str]:
    resp = await client.get(_GEOAPIFY_GEOCODE_URL, params={"text": "London", "limit": 1, "apiKey": key})
    if resp.status_code == 200:
        return True, "Geoapify key works."
    if resp.status_code in (401, 403):
        return False, "This key is not valid. Copy it again from myprojects.geoapify.com."
    return False, f"Geoapify refused the key: {_json(resp).get('message') or f'HTTP {resp.status_code}'}"


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
