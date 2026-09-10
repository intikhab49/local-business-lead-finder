"""Non-blocking discovery API: start a crawl, poll it, cancel it, resume it."""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import repository
from app.db.database import get_db
from app.dependencies import available_providers, resolve_providers
from app.schemas.requests import (
    DiscoveryJobResponse,
    DiscoveryStartRequest,
    SaveLeadsRequest,
)
from app.services.discovery_jobs import DiscoveryParams, job_manager, lead_row

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discovery", tags=["discovery"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("/providers", summary="Which providers are usable right now")
async def list_providers() -> dict[str, Any]:
    primary, fallback = resolve_providers()
    return {
        "available": available_providers(),
        "primary": primary.provider_name,
        "fallback": fallback.provider_name if fallback else None,
        "free": ["osm"],
        "concurrency": settings.discovery_concurrency,
    }


@router.post(
    "/start",
    response_model=DiscoveryJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a grid discovery job (returns immediately)",
)
async def start_discovery(request: DiscoveryStartRequest) -> DiscoveryJobResponse:
    params = DiscoveryParams(
        location=request.location.strip(),
        business_category=request.business_category.strip(),
        radius=request.radius,
        keyword=request.keyword,
        min_rating=request.min_rating,
        max_results=request.max_results,
        grid_size=request.grid_size,
        exclude_saved=request.exclude_saved,
        auto_save=request.auto_save,
        auto_enrich=request.auto_enrich,
        provider=request.provider,
        fallback_provider=request.fallback_provider,
        concurrency=request.concurrency,
        resume_run_id=request.resume_run_id,
    )
    job = job_manager.start(params)
    return DiscoveryJobResponse(**job.status_dict())


@router.get(
    "/jobs",
    summary="List discovery jobs known to this server process",
)
async def list_jobs() -> dict[str, Any]:
    return {"items": [job.status_dict() for job in job_manager.list()]}


@router.get(
    "/{job_id}",
    response_model=DiscoveryJobResponse,
    summary="Poll a discovery job",
)
async def get_job(job_id: str) -> DiscoveryJobResponse:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return DiscoveryJobResponse(**job.status_dict())


@router.get(
    "/{job_id}/results",
    summary="Read the leads a job has collected so far",
)
async def get_job_results(
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    window = job.leads[offset:offset + limit]
    return {
        "job_id": job.id,
        "status": job.status,
        "total": len(job.leads),
        "offset": offset,
        "items": window,
    }


@router.post("/{job_id}/cancel", summary="Cancel a running job (keeps its results)")
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    cancelled = job_manager.cancel(job_id)
    return {"job_id": job_id, "cancelling": cancelled, "status": job.status}


@router.post(
    "/{job_id}/save",
    summary="Save a job's leads to the database (used when auto-save is off)",
)
async def save_job_leads(
    job_id: str,
    db: DbSession,
    request: SaveLeadsRequest | None = None,
) -> dict[str, Any]:
    job = job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired")

    wanted = set(request.place_ids) if request and request.place_ids else None
    rows = [
        lead_row(lead, job.params.business_category, job.run_id)
        for lead in job.leads
        if wanted is None or lead.get("fsq_place_id") in wanted
    ]
    if not rows:
        return {"saved": 0, "created": 0, "updated": 0}

    result = repository.upsert_leads(db, rows)
    job.saved += result.get("created", 0) + result.get("updated", 0)
    return {
        "saved": result.get("created", 0) + result.get("updated", 0),
        **result,
    }


@router.get(
    "/runs/resumable",
    summary="Runs that stopped with cells left to crawl",
)
async def list_resumable_runs(db: DbSession) -> dict[str, Any]:
    runs = repository.list_run_history(db, limit=50, offset=0)
    items = []
    for run in runs:
        checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
        planned = int(checkpoint.get("cells_planned") or 0)
        done = len(checkpoint.get("cells_done") or [])
        if planned and done < planned:
            items.append({
                "run_id": run.id,
                "location": run.location,
                "business_category": run.business_category,
                "status": run.status,
                "cells_planned": planned,
                "cells_done": done,
                "found": checkpoint.get("found", 0),
                "params": checkpoint.get("params") or {},
                "started_at": run.started_at,
            })
    return {"items": items}
