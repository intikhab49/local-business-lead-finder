from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agents.human_review import HumanReviewAgent, ReviewResponse
from app.db.database import get_db
from app.schemas.review import (
    ReviewActionRequest,
    ReviewEditRequest,
    ReviewEntryResponse,
    ReviewListResponse,
    ReviewResponseSchema,
)
from app.services.lead_persistence_service import save_reviewed_lead

router = APIRouter()

review_agent = HumanReviewAgent()

DbSession = Annotated[Session, Depends(get_db)]


def _raise_not_found(fsq_place_id: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error": "NotFound",
            "message": f"No review found for {fsq_place_id}",
            "status_code": 404,
        },
    )


def _to_schema(response: ReviewResponse) -> ReviewResponseSchema:
    return ReviewResponseSchema(
        fsq_place_id=response.fsq_place_id,
        status=response.status,
        reviewer=response.reviewer,
        timestamp=response.timestamp,
        reason=response.reason,
        updated_fields=response.updated_fields,
        history=[
            ReviewEntryResponse(**entry)
            for entry in response.history
        ],
        lead=response.lead,
    )


@router.get(
    "/review/pending",
    response_model=ReviewListResponse,
    status_code=status.HTTP_200_OK,
    tags=["review"],
    summary="List pending reviews",
    description="Return all leads currently waiting for human review.",
)
async def list_pending_reviews():
    reviews = review_agent.list_pending()
    return ReviewListResponse(reviews=reviews, total=len(reviews))


@router.get(
    "/review/{fsq_place_id}",
    response_model=ReviewResponseSchema,
    status_code=status.HTTP_200_OK,
    tags=["review"],
    summary="Get review status and history",
    description="Return the review status, history and lead snapshot for a business.",
)
async def get_review(fsq_place_id: str):
    response = review_agent.get_review(fsq_place_id)
    if not response.success:
        _raise_not_found(fsq_place_id)
    return _to_schema(response)


@router.post(
    "/review/{fsq_place_id}/approve",
    response_model=ReviewResponseSchema,
    status_code=status.HTTP_200_OK,
    tags=["review"],
    summary="Approve a lead review",
    description="Mark a pending review as approved by a human reviewer.",
)
async def approve_review(fsq_place_id: str, request: ReviewActionRequest, db: DbSession):
    response = await review_agent.approve(
        fsq_place_id=fsq_place_id,
        reviewer=request.reviewer,
        reason=request.reason,
    )
    if not response.success:
        _raise_not_found(fsq_place_id)
    if response.lead:
        save_reviewed_lead(
            db,
            fsq_place_id=fsq_place_id,
            lead_data=response.lead,
            review_status="approved",
            reviewer=request.reviewer,
        )
    return _to_schema(response)


@router.post(
    "/review/{fsq_place_id}/reject",
    response_model=ReviewResponseSchema,
    status_code=status.HTTP_200_OK,
    tags=["review"],
    summary="Reject a lead review",
    description="Reject a pending review with an optional reason.",
)
async def reject_review(fsq_place_id: str, request: ReviewActionRequest):
    response = await review_agent.reject(
        fsq_place_id=fsq_place_id,
        reviewer=request.reviewer,
        reason=request.reason,
    )
    if not response.success:
        _raise_not_found(fsq_place_id)
    return _to_schema(response)


@router.post(
    "/review/{fsq_place_id}/edit",
    response_model=ReviewResponseSchema,
    status_code=status.HTTP_200_OK,
    tags=["review"],
    summary="Edit a lead under review",
    description="Apply human-edited fields to a lead and record the edit in its review history.",
)
async def edit_review(fsq_place_id: str, request: ReviewEditRequest, db: DbSession):
    response = await review_agent.edit(
        fsq_place_id=fsq_place_id,
        reviewer=request.reviewer,
        updated_fields=request.updated_fields,
    )
    if not response.success:
        _raise_not_found(fsq_place_id)
    if response.lead:
        save_reviewed_lead(
            db,
            fsq_place_id=fsq_place_id,
            lead_data=response.lead,
            review_status="edited",
            reviewer=request.reviewer,
        )
    return _to_schema(response)
