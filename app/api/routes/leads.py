from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import repository
from app.db.database import get_db
from app.db.models import Lead
from app.schemas.lead import (
    LeadBatchesResponse,
    LeadBatchRead,
    LeadBatchRequest,
    LeadBatchResult,
    LeadCreate,
    LeadDeleteResult,
    LeadListResponse,
    LeadRead,
    LeadStatsResponse,
    LeadUpdate,
)
from app.services.lead_persistence_service import lead_to_dict
from app.services.run_history_service import finish_run, start_run

router = APIRouter(prefix="/leads", tags=["leads"])

DbSession = Annotated[Session, Depends(get_db)]


def _raise_not_found(lead_id: int) -> None:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "NotFound", "message": f"Lead {lead_id} not found"},
    )


def _to_schema(lead: Lead) -> LeadRead:
    return LeadRead.model_validate(lead_to_dict(lead))


@router.post("", response_model=LeadRead, status_code=status.HTTP_201_CREATED)
def create_lead(payload: LeadCreate, db: DbSession) -> LeadRead:
    existing = repository.get_lead_by_fsq(db, payload.fsq_place_id)
    if existing is not None:
        return _to_schema(existing)
    return _to_schema(repository.create_lead(db, payload.model_dump()))


@router.post("/batch", response_model=LeadBatchResult)
def create_leads_batch(payload: LeadBatchRequest, db: DbSession) -> LeadBatchResult:
    """Save many leads as a new saved batch.

    Each Save All call creates its own batch (a ``save_all`` run-history
    record) so leads from different searches are never visually merged. The
    batch stores the search location, category, and total leads.
    """
    if not payload.items:
        return LeadBatchResult(created=0, updated=0, failed=0, run_id=None)

    run = start_run(
        db,
        "save_all",
        location=payload.location,
        business_category=payload.business_category,
        total_tasks=len(payload.items),
    )
    items = [item.model_dump() for item in payload.items]

    # Assign the run_id to incoming items so new leads are created with the
    # correct batch. Existing leads will be upserted as usual, but the
    # repository.upsert_lead implementation preserves an existing lead's
    # `run_id` if it is already set, preventing reassigning existing leads to
    # a different batch while still allowing other fields to be updated.
    for item in items:
        item["run_id"] = run.id

    result = repository.upsert_leads(db, items)
    total_results = result.get("created", 0) + result.get("updated", 0)
    finish_run(db, run, status="success", total_results=total_results)
    return LeadBatchResult(**result, run_id=run.id)


@router.get("/batches", response_model=LeadBatchesResponse)
def list_lead_batches(db: DbSession) -> LeadBatchesResponse:
    """List saved batches (Save All runs) grouped with their lead counts."""
    batches = repository.list_lead_batches(db)
    return LeadBatchesResponse(
        items=[
            LeadBatchRead(
                run_id=run.id,
                location=run.location,
                business_category=run.business_category,
                status=run.status,
                total_results=run.total_results,
                lead_count=lead_count,
                started_at=run.started_at,
                finished_at=run.finished_at,
            )
            for run, lead_count in batches
        ],
        total=len(batches),
        ungrouped_total=repository.count_leads(db, ungrouped=True),
    )


@router.get("", response_model=LeadListResponse)
def list_leads(
    db: DbSession,
    search: str | None = Query(default=None, max_length=200),
    review_status: str | None = Query(default=None, max_length=50),
    run_id: int | None = Query(default=None, ge=1),
    ungrouped: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> LeadListResponse:
    items = [
        _to_schema(lead)
        for lead in repository.list_leads(
            db,
            search=search,
            review_status=review_status,
            run_id=run_id,
            ungrouped=ungrouped,
            limit=limit,
            offset=offset,
        )
    ]
    total = repository.count_leads(
        db, search=search, review_status=review_status, run_id=run_id, ungrouped=ungrouped
    )
    return LeadListResponse(items=items, total=total, limit=limit, offset=offset)


@router.delete("", response_model=LeadDeleteResult)
def delete_leads(
    db: DbSession,
    search: str | None = Query(default=None, max_length=200),
    review_status: str | None = Query(default=None, max_length=50),
    run_id: int | None = Query(default=None, ge=1),
    ungrouped: bool = Query(default=False),
) -> LeadDeleteResult:
    """Delete all saved leads matching the selected filters."""
    deleted = repository.delete_leads(
        db,
        search=search,
        review_status=review_status,
        run_id=run_id,
        ungrouped=ungrouped,
    )
    return LeadDeleteResult(deleted=deleted)


@router.get("/stats", response_model=LeadStatsResponse)
def lead_stats(db: DbSession) -> LeadStatsResponse:
    return LeadStatsResponse(**repository.get_stats(db))


@router.get("/{lead_id}", response_model=LeadRead)
def get_lead(lead_id: int, db: DbSession) -> LeadRead:
    lead = repository.get_lead(db, lead_id)
    if lead is None:
        _raise_not_found(lead_id)
    return _to_schema(lead)


@router.put("/{lead_id}", response_model=LeadRead)
def update_lead(lead_id: int, payload: LeadUpdate, db: DbSession) -> LeadRead:
    lead = repository.get_lead(db, lead_id)
    if lead is None:
        _raise_not_found(lead_id)
    data = payload.model_dump(exclude_unset=True)
    return _to_schema(repository.update_lead(db, lead, data))


@router.delete("/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lead(lead_id: int, db: DbSession) -> None:
    lead = repository.get_lead(db, lead_id)
    if lead is None:
        _raise_not_found(lead_id)
    repository.delete_lead(db, lead)
