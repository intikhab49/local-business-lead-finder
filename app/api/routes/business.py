import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.exceptions import (
    NoResultsError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    ValidationError,
)
from app.db import repository
from app.db.database import get_db
from app.dependencies import get_business_search_service, resolve_providers
from app.providers.base import Business, SearchQuery
from app.schemas.requests import (
    AgentSearchRequest,
    AgentSearchResponse,
    BusinessDetailResponse,
    BusinessResponse,
    BusinessSearchQueryRequest,
    BusinessSearchRequest,
    EnrichedBusinessResponse,
    EnrichmentRequest,
    ErrorResponse,
    HealthResponse,
    SearchResponse,
)
from app.services.business_search_service import BusinessSearchService
from app.services.enrichment_service import EnrichmentService
from app.services.lead_persistence_service import clean_categories_list, save_reviewed_lead
from app.services.run_history_service import finish_run, start_run
from app.services.discovery_engine import DiscoveryEngine

logger = logging.getLogger(__name__)

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]


def _business_to_response(b: dict) -> BusinessResponse:
    return BusinessResponse(
        fsq_place_id=b.get("fsq_place_id", ""),
        name=b.get("name", ""),
        formatted_address=b.get("formatted_address", ""),
        phone_number=b.get("phone_number"),
        website=b.get("website"),
        email=b.get("email") or None,
        linkedin=b.get("linkedin"),
        facebook=b.get("facebook"),
        instagram=b.get("instagram"),
        twitter=b.get("twitter"),
        youtube=b.get("youtube"),
        rating=b.get("rating"),
        categories=clean_categories_list(b.get("categories", [])),
        latitude=b.get("latitude"),
        longitude=b.get("longitude"),
        opening_hours=b.get("opening_hours", []),
        price_level=b.get("price_level"),
        confidence_score=b.get("confidence_score"),
        source=b.get("source"),
        auto_enriched=bool(b.get("auto_enriched", False)),
    )


async def _auto_enrich(results: list[dict]) -> list[dict]:
    """Auto-enrich search results — scrape, discover emails, socials. Never fails the search."""
    if not results:
        return results

    enrichment = EnrichmentService()
    pending = [
        (idx, r)
        for idx, r in enumerate(results)
        if r.get("fsq_place_id") and r.get("website")
    ]
    if not pending:
        return results

    businesses = [Business.from_dict(r) for _, r in pending]
    try:
        enriched_map = await enrichment.enrich_many(businesses)
    except Exception:
        logger.exception("Automatic enrichment failed — returning results as-is")
        return results

    for _, result in pending:
        found = enriched_map.get(result.get("fsq_place_id", ""))
        if not found:
            continue
        if found.email:
            result["email"] = found.email
        if found.website:
            result["website"] = found.website
        for field in ("linkedin", "facebook", "instagram", "twitter", "youtube"):
            value = getattr(found, field)
            if value:
                result[field] = value
        try:
            if getattr(found, "confidence_score", None) is not None:
                result["confidence_score"] = found.confidence_score
        except Exception:
            pass
        try:
            if getattr(found, "source", None):
                result["source"] = found.source
        except Exception:
            pass
        result["auto_enriched"] = True
    return results


# ── Health / Metrics ──────────────────────────────────────────────

@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["health"],
    summary="Health check",
)
async def health_check(
    service: BusinessSearchService = Depends(get_business_search_service),
):
    is_healthy = await service.health_check()
    return HealthResponse(
        status="healthy" if is_healthy else "unhealthy",
        service="Business Lead Finder",
        version="2.0.0",
        provider=service.provider.provider_name,
    )


@router.get(
    "/metrics",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    tags=["metrics"],
)
async def get_metrics_endpoint():
    from app.db import repository as repo
    from app.db.database import get_db
    db = next(get_db())
    try:
        stats = repo.get_stats(db)
        return {
            "leads": stats,
            "total_known": stats["total"],
        }
    finally:
        db.close()


# ── Search (with dedup) ───────────────────────────────────────────

@router.post(
    "/search",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    tags=["business"],
    summary="Search businesses by location and category",
)
async def search_businesses(
    request: BusinessSearchRequest,
    db: DbSession,
    service: BusinessSearchService = Depends(get_business_search_service),
):
    run = start_run(
        db, "search",
        location=request.location,
        business_category=request.business_category,
    )
    started = time.monotonic()
    try:
        if request.search_mode == "staggered":
            # Grid crawl: concurrent cells, adaptive pacing, free-provider fallback.
            # Prefer POST /discovery/start — it streams progress and can resume.
            primary, fallback = resolve_providers()
            known = repository.get_all_known_place_ids(db) if request.exclude_saved else set()
            engine = DiscoveryEngine(primary, fallback, seen=known)
            query = SearchQuery(
                query=request.business_category.strip(),
                location=request.location.strip(),
                radius=request.radius,
                type=request.business_category.strip().lower(),
                keyword=request.keyword.strip() if request.keyword else None,
                min_rating=request.min_rating,
                max_results=request.max_results or 500,
            )
            report = await engine.discover(
                query,
                grid_size=request.grid_size,
                max_total=request.max_results or 500,
            )
            businesses = report.businesses
            result = {
                "businesses": [b.to_dict() for b in businesses],
                "total_results": len(businesses),
                "new_results": len(businesses),
                "skipped": 0,
                "total_from_api": len(businesses),
                "search_location": request.location,
                "search_category": request.business_category,
                "search_radius": request.radius,
                "next_page_token": None,
                "message": (
                    f"Grid crawl across {report.cells_planned} cells found "
                    f"{len(businesses)} leads"
                ),
            }
        else:
            result = await service.search(
                location=request.location,
                business_category=request.business_category,
                radius=request.radius,
                keyword=request.keyword,
                min_rating=request.min_rating,
                max_results=request.max_results,
                exclude_saved=request.exclude_saved,
                db=db,
            )
    except ValidationError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=400, detail=ErrorResponse(
            error="ValidationError", message=str(e.message), status_code=400).model_dump()) from e
    except ProviderTimeoutError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=504, detail=ErrorResponse(
            error="Timeout", message=str(e.message), status_code=504).model_dump()) from e
    except RateLimitError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=429, detail=ErrorResponse(
            error="RateLimit", message=str(e.message), status_code=429,
            details=e.details).model_dump()) from e
    except ProviderError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error="ProviderError", message=str(e.message), status_code=502).model_dump()) from e
    except Exception as e:
        finish_run(db, run, status="failed", error_message="An unexpected error occurred",
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=500, detail=ErrorResponse(
            error="InternalServerError", message="An unexpected error occurred",
            status_code=500).model_dump()) from e

    finish_run(db, run, status="success",
               total_results=result["total_results"],
               processing_time=time.monotonic() - started)

    businesses = result["businesses"]

    # Auto-persist new leads to DB so dedup works on next search
    new_leads_data = []
    for b in businesses:
        new_leads_data.append({
            "fsq_place_id": b.get("fsq_place_id", ""),
            "business_name": b.get("name", ""),
            "address": b.get("formatted_address", ""),
            "phone": b.get("phone_number"),
            "website": b.get("website"),
            "rating": b.get("rating"),
            "categories": ", ".join(b.get("categories", [])),
            "latitude": b.get("latitude"),
            "longitude": b.get("longitude"),
            "source": result["search_category"],
            "review_status": "discovered",
            "run_id": run.id,
        })
    if new_leads_data:
        repository.upsert_leads(db, new_leads_data)

    businesses = await _auto_enrich(businesses)

    return SearchResponse(
        businesses=[_business_to_response(r) for r in businesses],
        total_results=result["total_results"],
        new_results=result.get("new_results", result["total_results"]),
        skipped=result.get("skipped", 0),
        total_from_api=result.get("total_from_api", result["total_results"]),
        search_location=result["search_location"],
        search_category=result["search_category"],
        search_radius=result["search_radius"],
        next_page_token=result.get("next_page_token"),
        message=result.get("message"),
    )


@router.post(
    "/search/query",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    tags=["business"],
    summary="Search businesses by text query",
)
async def search_businesses_by_query(
    request: BusinessSearchQueryRequest,
    db: DbSession,
    service: BusinessSearchService = Depends(get_business_search_service),
):
    run = start_run(
        db, "search",
        location=request.location or "",
        business_category=request.business_category or request.query,
    )
    started = time.monotonic()
    try:
        result = await service.search_by_query(
            query_text=request.query,
            location=request.location,
            radius=request.radius,
            business_category=request.business_category,
            min_rating=request.min_rating,
            max_results=request.max_results,
            exclude_saved=request.exclude_saved,
            db=db,
        )
        businesses = result["businesses"]

        # Auto-persist
        new_leads_data = []
        for b in businesses:
            new_leads_data.append({
                "fsq_place_id": b.get("fsq_place_id", ""),
                "business_name": b.get("name", ""),
                "address": b.get("formatted_address", ""),
                "phone": b.get("phone_number"),
                "website": b.get("website"),
                "categories": ", ".join(b.get("categories", [])),
                "latitude": b.get("latitude"),
                "longitude": b.get("longitude"),
                "source": result["search_category"],
                "review_status": "discovered",
                "run_id": run.id,
            })
        if new_leads_data:
            repository.upsert_leads(db, new_leads_data)

        finish_run(db, run, status="success",
                   total_results=result["total_results"],
                   processing_time=time.monotonic() - started)

        businesses = await _auto_enrich(businesses)
        return SearchResponse(
            businesses=[_business_to_response(r) for r in businesses],
            total_results=result["total_results"],
            new_results=result.get("new_results", result["total_results"]),
            skipped=result.get("skipped", 0),
            total_from_api=result.get("total_from_api", result["total_results"]),
            search_location=result["search_location"],
            search_category=result["search_category"],
            search_radius=result["search_radius"],
            next_page_token=result.get("next_page_token"),
            message=result.get("message"),
        )
    except ValidationError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=400, detail=ErrorResponse(
            error="ValidationError", message=str(e.message), status_code=400).model_dump()) from e
    except ProviderTimeoutError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=504, detail=ErrorResponse(
            error="Timeout", message=str(e.message), status_code=504).model_dump()) from e
    except RateLimitError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=429, detail=ErrorResponse(
            error="RateLimit", message=str(e.message), status_code=429,
            details=e.details).model_dump()) from e
    except ProviderError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error="ProviderError", message=str(e.message), status_code=502).model_dump()) from e
    except Exception as e:
        finish_run(db, run, status="failed", error_message="An unexpected error occurred",
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=500, detail=ErrorResponse(
            error="InternalServerError", message="An unexpected error occurred",
            status_code=500).model_dump()) from e


# ── Business Details ──────────────────────────────────────────────

@router.get(
    "/businesses/{fsq_place_id}",
    response_model=BusinessDetailResponse,
    status_code=status.HTTP_200_OK,
    tags=["business"],
)
async def get_business_details(
    fsq_place_id: str,
    service: BusinessSearchService = Depends(get_business_search_service),
):
    try:
        business = await service.get_business_details(fsq_place_id)
        return BusinessDetailResponse(business=_business_to_response(business))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=ErrorResponse(
            error="ValidationError", message=str(e.message), status_code=400).model_dump()) from e
    except NoResultsError as e:
        raise HTTPException(status_code=404, detail=ErrorResponse(
            error="NotFound", message=str(e.message), status_code=404).model_dump()) from e
    except RateLimitError as e:
        raise HTTPException(status_code=429, detail=ErrorResponse(
            error="RateLimit", message=str(e.message), status_code=429,
            details=e.details).model_dump()) from e
    except ProviderError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error="ProviderError", message=str(e.message), status_code=502).model_dump()) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=ErrorResponse(
            error="InternalServerError", message="An unexpected error occurred",
            status_code=500).model_dump()) from e


# ── Enrich / Discover ─────────────────────────────────────────────

_enrichment_service = EnrichmentService()


@router.post(
    "/enrich/discover",
    response_model=EnrichedBusinessResponse,
    status_code=status.HTTP_200_OK,
    tags=["business"],
    summary="AI-powered lead discovery",
)
async def discover_business(
    request: EnrichmentRequest,
    db: DbSession,
    service: BusinessSearchService = Depends(get_business_search_service),
):
    run = start_run(db, "discover_more",
                    location=request.fsq_place_id,
                    business_category="")
    started = time.monotonic()
    try:
        business_dict = await service.get_business_details(request.fsq_place_id)
        business = Business.from_dict(business_dict)
        enriched = await _enrichment_service.enrich_with_discovery(business)
    except ValidationError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=400, detail=ErrorResponse(
            error="ValidationError", message=str(e.message), status_code=400).model_dump()) from e
    except NoResultsError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=404, detail=ErrorResponse(
            error="NotFound", message=str(e.message), status_code=404).model_dump()) from e
    except ProviderTimeoutError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=504, detail=ErrorResponse(
            error="Timeout", message=str(e.message), status_code=504).model_dump()) from e
    except RateLimitError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=429, detail=ErrorResponse(
            error="RateLimit", message=str(e.message), status_code=429,
            details=e.details).model_dump()) from e
    except ProviderError as e:
        finish_run(db, run, status="failed", error_message=str(e.message),
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error="ProviderError", message=str(e.message), status_code=502).model_dump()) from e
    except Exception as e:
        finish_run(db, run, status="failed", error_message="An unexpected error occurred",
                   processing_time=time.monotonic() - started)
        raise HTTPException(status_code=500, detail=ErrorResponse(
            error="InternalServerError", message="An unexpected error occurred",
            status_code=500).model_dump()) from e

    finish_run(db, run, status="success", total_results=1,
               processing_time=time.monotonic() - started)
    return EnrichedBusinessResponse(
        business_name=enriched.business_name,
        address=enriched.address,
        phone=enriched.phone,
        website=enriched.website,
        email=enriched.email,
        linkedin=enriched.linkedin,
        facebook=enriched.facebook,
        instagram=enriched.instagram,
        twitter=enriched.twitter,
        youtube=enriched.youtube,
        latitude=enriched.latitude,
        longitude=enriched.longitude,
        confidence_score=enriched.confidence_score,
        source=enriched.source,
    )


@router.post(
    "/enrich",
    response_model=EnrichedBusinessResponse,
    status_code=status.HTTP_200_OK,
    tags=["business"],
    summary="Enrich a business lead",
)
async def enrich_business(
    request: EnrichmentRequest,
    service: BusinessSearchService = Depends(get_business_search_service),
):
    try:
        business_dict = await service.get_business_details(request.fsq_place_id)
        business = Business.from_dict(business_dict)
        enriched = await _enrichment_service.enrich_with_scraper(business)
        return EnrichedBusinessResponse(
            business_name=enriched.business_name,
            address=enriched.address,
            phone=enriched.phone,
            website=enriched.website,
            email=enriched.email,
            linkedin=enriched.linkedin,
            facebook=enriched.facebook,
            instagram=enriched.instagram,
            twitter=enriched.twitter,
            youtube=enriched.youtube,
            latitude=enriched.latitude,
            longitude=enriched.longitude,
            confidence_score=enriched.confidence_score,
            source=enriched.source,
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=ErrorResponse(
            error="ValidationError", message=str(e.message), status_code=400).model_dump()) from e
    except NoResultsError as e:
        raise HTTPException(status_code=404, detail=ErrorResponse(
            error="NotFound", message=str(e.message), status_code=404).model_dump()) from e
    except RateLimitError as e:
        raise HTTPException(status_code=429, detail=ErrorResponse(
            error="RateLimit", message=str(e.message), status_code=429,
            details=e.details).model_dump()) from e
    except ProviderError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error="ProviderError", message=str(e.message), status_code=502).model_dump()) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=ErrorResponse(
            error="InternalServerError", message="An unexpected error occurred",
            status_code=500).model_dump()) from e


# ── Bulk Save ─────────────────────────────────────────────────────

@router.post(
    "/search/save",
    status_code=status.HTTP_200_OK,
    tags=["business"],
    summary="Save search results as leads",
)
async def save_search_results(
    businesses: list[dict],
    db: DbSession,
    location: str = "",
    business_category: str = "",
):
    """Upsert businesses into the leads table. Place IDs already saved will be updated."""
    run = start_run(db, "save_all",
                    location=location,
                    business_category=business_category,
                    total_tasks=len(businesses))
    result = repository.upsert_leads(
        db,
        [{**b, "run_id": run.id, "review_status": b.get("review_status", "approved")}
         for b in businesses],
    )
    finish_run(db, run, status="success",
               total_results=result["created"] + result["updated"])
    return {"saved": result, "run_id": run.id}


# ── Exclusions ────────────────────────────────────────────────────

@router.post(
    "/exclusions/{fsq_place_id}",
    status_code=status.HTTP_201_CREATED,
    tags=["business"],
    summary="Exclude a place from future searches",
)
async def exclude_business(fsq_place_id: str, reason: str = Query(default=""), db: Session = Depends(get_db)):
    repository.add_exclusion(db, fsq_place_id, reason)
    return {"status": "excluded", "fsq_place_id": fsq_place_id}


@router.delete(
    "/exclusions/{fsq_place_id}",
    status_code=status.HTTP_200_OK,
    tags=["business"],
    summary="Remove exclusion for a place",
)
async def unexclude_business(fsq_place_id: str, db: Session = Depends(get_db)):
    removed = repository.remove_exclusion(db, fsq_place_id)
    if not removed:
        raise HTTPException(status_code=404, detail={"error": "Not found"})
    return {"status": "unexcluded", "fsq_place_id": fsq_place_id}


@router.get(
    "/exclusions",
    status_code=status.HTTP_200_OK,
    tags=["business"],
    summary="List all excluded places",
)
async def list_exclusions(db: Session = Depends(get_db)):
    return {"exclusions": [e.fsq_place_id for e in repository.list_exclusions(db)]}