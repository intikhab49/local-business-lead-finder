"""API client for backend calls (v2)."""
import os
from typing import Any

import requests

BACKEND_URL = os.environ.get("BLF_BACKEND_URL", "http://localhost:8000")
API_PREFIX = "/api/v1"

_session = requests.Session()


def _url(path: str) -> str:
    return f"{BACKEND_URL}{API_PREFIX}{path}"


def health_check() -> dict[str, Any] | None:
    try:
        resp = _session.get(f"{BACKEND_URL}/health", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def _backend_error_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
        detail = data.get("detail", {}) if isinstance(data, dict) else {}
        message = detail.get("message") if isinstance(detail, dict) else None
        status_code = detail.get("status_code") if isinstance(detail, dict) else None
    except ValueError:
        return f"{resp.status_code} {resp.reason}"
    if message:
        if status_code == 429:
            retry_after = (detail.get("details") or {}).get("retry_after")
            if retry_after:
                return f"Rate-limited. Wait ~{int(retry_after)}s and try again."
        return f"Error {status_code}: {message}"
    return f"{resp.status_code} {resp.reason}"


def _request_json(resp: requests.Response) -> dict[str, Any]:
    if not resp.ok:
        raise RuntimeError(_backend_error_message(resp))
    return resp.json()


# ── Search ────────────────────────────────────────────────────────

def search_businesses(
    location: str,
    business_category: str,
    radius: int = 5000,
    max_results: int | None = 500,
    exclude_saved: bool = True,
    mode: str = "normal",
    grid_size: int = 3,
    pause_seconds: int = 60,
) -> dict[str, Any]:
    payload = {
        "location": location,
        "business_category": business_category,
        "radius": radius,
        "max_results": max_results,
        "exclude_saved": exclude_saved,
        "search_mode": mode,
        "grid_size": grid_size,
        "pause_seconds": pause_seconds,
    }
    resp = _session.post(
        _url("/search"),
        json=payload,
        timeout=1800,
    )
    return _request_json(resp)


# ── Settings (data source + API keys) ─────────────────────────────

def get_settings() -> dict[str, Any]:
    resp = _session.get(_url("/settings"), timeout=10)
    return _request_json(resp)


def update_settings(**fields: Any) -> dict[str, Any]:
    resp = _session.put(_url("/settings"), json=fields, timeout=10)
    return _request_json(resp)


def test_api_key(provider: str, api_key: str | None = None) -> dict[str, Any]:
    resp = _session.post(
        _url("/settings/test"),
        json={"provider": provider, "api_key": api_key},
        timeout=40,
    )
    return _request_json(resp)


# ── Discovery jobs (non-blocking crawl) ───────────────────────────

def list_providers() -> dict[str, Any]:
    resp = _session.get(_url("/discovery/providers"), timeout=30)
    return _request_json(resp)


def start_discovery(
    location: str,
    business_category: str,
    radius: int = 5000,
    max_results: int = 500,
    grid_size: int = 3,
    exclude_saved: bool = True,
    auto_save: bool = True,
    auto_enrich: bool = True,
    provider: str | None = None,
    fallback_provider: str | None = None,
    concurrency: int | None = None,
    keyword: str | None = None,
    min_rating: float | None = None,
    resume_run_id: int | None = None,
) -> dict[str, Any]:
    payload = {
        "location": location,
        "business_category": business_category,
        "radius": radius,
        "max_results": max_results,
        "grid_size": grid_size,
        "exclude_saved": exclude_saved,
        "auto_save": auto_save,
        "auto_enrich": auto_enrich,
        "provider": provider,
        "fallback_provider": fallback_provider,
        "concurrency": concurrency,
        "keyword": keyword or None,
        "min_rating": min_rating,
        "resume_run_id": resume_run_id,
    }
    resp = _session.post(_url("/discovery/start"), json=payload, timeout=60)
    return _request_json(resp)


def get_discovery_job(job_id: str) -> dict[str, Any]:
    resp = _session.get(_url(f"/discovery/{job_id}"), timeout=30)
    return _request_json(resp)


def get_discovery_results(
    job_id: str, offset: int = 0, limit: int = 1000
) -> dict[str, Any]:
    resp = _session.get(
        _url(f"/discovery/{job_id}/results"),
        params={"offset": offset, "limit": limit},
        timeout=60,
    )
    return _request_json(resp)


def cancel_discovery(job_id: str) -> dict[str, Any]:
    resp = _session.post(_url(f"/discovery/{job_id}/cancel"), timeout=30)
    return _request_json(resp)


def save_discovery_leads(job_id: str, place_ids: list[str] | None = None) -> dict[str, Any]:
    resp = _session.post(
        _url(f"/discovery/{job_id}/save"),
        json={"place_ids": place_ids or []},
        timeout=120,
    )
    return _request_json(resp)


def list_discovery_jobs() -> dict[str, Any]:
    resp = _session.get(_url("/discovery/jobs"), timeout=30)
    return _request_json(resp)


def list_resumable_runs() -> dict[str, Any]:
    resp = _session.get(_url("/discovery/runs/resumable"), timeout=30)
    return _request_json(resp)


def discover_business(fsq_place_id: str) -> dict[str, Any]:
    resp = _session.post(
        _url("/enrich/discover"),
        json={"fsq_place_id": fsq_place_id},
        timeout=300,
    )
    return _request_json(resp)


def save_search_results(
    businesses: list[dict[str, Any]],
    location: str = "",
    business_category: str = "",
) -> dict[str, Any]:
    resp = _session.post(
        _url("/search/save"),
        json=businesses,
        params={"location": location, "business_category": business_category},
        timeout=120,
    )
    return _request_json(resp)


# ── Exclusions ────────────────────────────────────────────────────

def exclude_business(fsq_place_id: str, reason: str = "") -> dict[str, Any]:
    resp = _session.post(
        _url(f"/exclusions/{fsq_place_id}"),
        params={"reason": reason},
        timeout=30,
    )
    return _request_json(resp)


def unexclude_business(fsq_place_id: str) -> dict[str, Any]:
    resp = _session.delete(_url(f"/exclusions/{fsq_place_id}"), timeout=30)
    return _request_json(resp)


def list_exclusions() -> dict[str, Any]:
    resp = _session.get(_url("/exclusions"), timeout=30)
    return _request_json(resp)


# ── Leads / Saved ─────────────────────────────────────────────────

def list_leads(
    search: str = "",
    review_status: str = "",
    run_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if search:
        params["search"] = search
    if review_status:
        params["review_status"] = review_status
    if run_id is not None:
        params["run_id"] = run_id
    resp = _session.get(_url("/leads"), params=params, timeout=30)
    return _request_json(resp)


def get_lead_stats() -> dict[str, Any]:
    resp = _session.get(_url("/leads/stats"), timeout=30)
    return _request_json(resp)


def list_lead_batches() -> dict[str, Any]:
    resp = _session.get(_url("/leads/batches"), timeout=30)
    return _request_json(resp)


def save_leads_batch(
    items: list[dict[str, Any]],
    location: str = "",
    business_category: str = "",
) -> dict[str, Any]:
    resp = _session.post(
        _url("/leads/batch"),
        json={"items": items, "location": location, "business_category": business_category},
        timeout=60,
    )
    return _request_json(resp)


def update_lead(lead_id: int, data: dict[str, Any]) -> dict[str, Any]:
    resp = _session.put(_url(f"/leads/{lead_id}"), json=data, timeout=30)
    return _request_json(resp)


def delete_lead(lead_id: int) -> None:
    resp = _session.delete(_url(f"/leads/{lead_id}"), timeout=30)
    resp.raise_for_status()


def delete_leads(
    search: str = "",
    review_status: str = "",
    run_id: int | None = None,
) -> int:
    params: dict[str, Any] = {}
    if search:
        params["search"] = search
    if review_status:
        params["review_status"] = review_status
    if run_id is not None:
        params["run_id"] = run_id
    resp = _session.delete(_url("/leads"), params=params, timeout=60)
    return _request_json(resp).get("deleted", 0)


# ── Export ─────────────────────────────────────────────────────────

def export_csv(
    search: str = "",
    review_status: str = "",
    run_id: int | None = None,
) -> tuple[str, str]:
    params: dict[str, Any] = {}
    if search:
        params["search"] = search
    if review_status:
        params["review_status"] = review_status
    if run_id is not None:
        params["run_id"] = run_id
    resp = _session.get(_url("/export/csv"), params=params, timeout=60)
    resp.raise_for_status()
    filename = "leads.csv"
    disposition = resp.headers.get("Content-Disposition", "")
    if "filename=" in disposition:
        filename = disposition.split("filename=")[-1].strip('"')
    return filename, resp.text


def export_excel(
    search: str = "",
    review_status: str = "",
    run_id: int | None = None,
) -> tuple[str, bytes]:
    params: dict[str, Any] = {}
    if search:
        params["search"] = search
    if review_status:
        params["review_status"] = review_status
    if run_id is not None:
        params["run_id"] = run_id
    resp = _session.get(_url("/export/excel"), params=params, timeout=60)
    resp.raise_for_status()
    filename = "leads.xlsx"
    disposition = resp.headers.get("Content-Disposition", "")
    if "filename=" in disposition:
        filename = disposition.split("filename=")[-1].strip('"')
    return filename, resp.content


# ── Review ─────────────────────────────────────────────────────────

def get_pending_reviews() -> dict[str, Any]:
    resp = _session.get(_url("/review/pending"), timeout=30)
    return _request_json(resp)


def approve_review(fsq_place_id: str, reviewer: str = "") -> dict[str, Any]:
    resp = _session.post(
        _url(f"/review/{fsq_place_id}/approve"),
        json={"reviewer": reviewer, "reason": ""},
        timeout=30,
    )
    return _request_json(resp)


def reject_review(fsq_place_id: str, reviewer: str = "", reason: str = "") -> dict[str, Any]:
    resp = _session.post(
        _url(f"/review/{fsq_place_id}/reject"),
        json={"reviewer": reviewer, "reason": reason},
        timeout=30,
    )
    return _request_json(resp)


# ── History ────────────────────────────────────────────────────────

def list_run_history(
    search: str = "",
    run_type: str = "",
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if search:
        params["search"] = search
    if run_type:
        params["run_type"] = run_type
    if status:
        params["status"] = status
    resp = _session.get(_url("/history"), params=params, timeout=30)
    return _request_json(resp)


def get_run_history(run_id: int) -> dict[str, Any]:
    resp = _session.get(_url(f"/history/{run_id}"), timeout=30)
    return _request_json(resp)


def get_run_history_stats() -> dict[str, Any]:
    resp = _session.get(_url("/history/stats"), timeout=30)
    return _request_json(resp)


def delete_run_history(run_id: int) -> None:
    resp = _session.delete(_url(f"/history/{run_id}"), timeout=30)
    resp.raise_for_status()


def _history_export(kind: str, fallback_name: str, **filters: Any) -> tuple[str, bytes]:
    params = {key: value for key, value in filters.items() if value}
    resp = _session.get(_url(f"/history/export/{kind}"), params=params, timeout=60)
    resp.raise_for_status()
    filename = fallback_name
    disposition = resp.headers.get("Content-Disposition", "")
    if "filename=" in disposition:
        filename = disposition.split("filename=")[-1].strip('"')
    return filename, resp.content


def export_history_csv(**filters: Any) -> tuple[str, bytes]:
    return _history_export("csv", "run_history.csv", **filters)


def export_history_excel(**filters: Any) -> tuple[str, bytes]:
    return _history_export("excel", "run_history.xlsx", **filters)


def clear_run_history() -> dict[str, Any]:
    resp = _session.delete(_url("/history"), timeout=30)
    return _request_json(resp)


# ── Metrics ────────────────────────────────────────────────────────

def get_metrics_snapshot() -> dict[str, Any]:
    resp = _session.get(_url("/metrics"), timeout=30)
    return _request_json(resp)