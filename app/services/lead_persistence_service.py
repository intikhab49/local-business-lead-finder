import logging
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Lead
from app.db.repository import create_lead, get_lead_by_fsq, update_lead
from app.monitoring.metrics import get_metrics

logger = logging.getLogger(__name__)

_GENERIC_CATEGORIES = frozenset({"point_of_interest", "establishment"})


def _is_generic_category(category: str) -> bool:
    return bool(category) and category.strip().lower() in _GENERIC_CATEGORIES


def clean_categories(categories: str | None) -> str:
    """Drop generic provider category labels from a comma-joined string.

    Used consistently by the API serialization and CSV/Excel exports so the
    Saved Leads UI, CSV, and Excel all show the same cleaned categories.
    """
    if not categories:
        return ""
    parts = [part.strip() for part in categories.split(",")]
    kept = [part for part in parts if part and not _is_generic_category(part)]
    return ", ".join(kept)


def clean_categories_list(categories: list[str] | None) -> list[str]:
    """Drop generic provider category labels from a list of category labels.

    Shared logic with :func:`clean_categories` so Search Results show the
    same cleaned categories as Saved Leads, CSV, and Excel.
    """
    if not categories:
        return []
    return [part.strip() for part in categories if not _is_generic_category(part)]


def parse_location_fields(address: str | None, data: dict[str, Any] | None = None) -> dict[str, str | None]:
    """Derive city/state/country heuristically from a formatted address.

    Explicit values in ``data`` take precedence; otherwise the trailing
    comma-separated fields of an address are used as a best-effort mapping.
    """
    address = address or ""
    data = data or {}
    if data.get("city") or data.get("state") or data.get("country"):
        return {
            "city": (data.get("city") or "").strip() or None,
            "state": (data.get("state") or "").strip() or None,
            "country": (data.get("country") or "").strip() or None,
        }

    parts = [part.strip() for part in address.split(",") if part.strip()]
    city = state = country = None
    if parts:
        country = parts[-1]
    if len(parts) >= 2:
        state = parts[-2]
    if len(parts) >= 3:
        city = parts[-3]
    if isinstance(city, str) and city.isdigit():
        city = parts[-4] if len(parts) >= 4 else None  # e.g. postal code in state slot
    return {"city": city, "state": state, "country": country}


def lead_to_dict(lead: Lead) -> dict[str, Any]:
    """Serialize an ORM Lead into a plain dict for API/export consumers."""
    return {
        "id": lead.id,
        "fsq_place_id": lead.fsq_place_id,
        "business_name": lead.business_name,
        "address": lead.address,
        "categories": clean_categories(lead.categories),
        "city": lead.city,
        "state": lead.state,
        "country": lead.country,
        "phone": lead.phone,
        "website": lead.website,
        "email": lead.email,
        "linkedin": lead.linkedin,
        "facebook": lead.facebook,
        "instagram": lead.instagram,
        "twitter": lead.twitter,
        "youtube": lead.youtube,
        "latitude": lead.latitude,
        "longitude": lead.longitude,
        "confidence_score": lead.confidence_score,
        "source": lead.source,
        "review_status": lead.review_status,
        "run_id": lead.run_id,
        "reviewer": lead.reviewer,
        "notes": lead.notes,
        "extra": lead.extra,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "updated_at": lead.updated_at.isoformat() if lead.updated_at else None,
    }


def save_reviewed_lead(
    db: Session,
    fsq_place_id: str,
    lead_data: dict[str, Any],
    review_status: str,
    reviewer: str = "",
    notes: str | None = None,
) -> Lead:
    """Persist (upsert) a lead that has been approved or edited by a human.

    Reuses the existing review workflow output: the lead snapshot recorded by
    the HumanReviewAgent is converted into a durable Lead record. Metrics are
    incremented for observability.
    """
    existing = get_lead_by_fsq(db, fsq_place_id)
    data = {
        "fsq_place_id": fsq_place_id,
        "business_name": lead_data.get("business_name") or "",
        "address": lead_data.get("address") or "",
        "categories": lead_data.get("categories"),
        "city": lead_data.get("city"),
        "state": lead_data.get("state"),
        "country": lead_data.get("country"),
        "phone": lead_data.get("phone"),
        "website": lead_data.get("website"),
        "email": lead_data.get("email"),
        "linkedin": lead_data.get("linkedin"),
        "facebook": lead_data.get("facebook"),
        "instagram": lead_data.get("instagram"),
        "twitter": lead_data.get("twitter"),
        "youtube": lead_data.get("youtube"),
        "latitude": lead_data.get("latitude"),
        "longitude": lead_data.get("longitude"),
        "confidence_score": lead_data.get("confidence_score") or 0.0,
        "source": lead_data.get("source") or "",
        "review_status": review_status,
        "reviewer": reviewer or None,
        "notes": notes or None,
    }
    if existing is not None:
        lead = update_lead(db, existing, data)
        get_metrics().increment("leads_updated")
    else:
        lead = create_lead(db, data)
        get_metrics().increment("leads_created")

    logger.info(
        "[LeadStore] Saved reviewed lead fsq=%s status=%s",
        fsq_place_id,
        review_status,
    )
    return lead
