from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import DiscoveryExclusion, Lead, RunHistory

logger = logging.getLogger(__name__)

_FILTER_COLUMNS = (Lead.business_name, Lead.email, Lead.website, Lead.phone, Lead.address)


def _apply_filters(
    stmt,
    search: str | None,
    review_status: str | None,
    run_id: int | None = None,
    ungrouped: bool = False,
):
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(*(column.ilike(pattern) for column in _FILTER_COLUMNS)))
    if review_status:
        stmt = stmt.where(Lead.review_status == review_status)
    if ungrouped:
        orphaned = Lead.run_id.not_in(select(RunHistory.id))
        stmt = stmt.where(or_(Lead.run_id.is_(None), orphaned))
    elif run_id is not None:
        stmt = stmt.where(Lead.run_id == run_id)
    return stmt


def count_leads(
    db: Session,
    search: str | None = None,
    review_status: str | None = None,
    run_id: int | None = None,
    ungrouped: bool = False,
) -> int:
    stmt = _apply_filters(
        select(func.count()).select_from(Lead), search, review_status, run_id, ungrouped
    )
    return db.execute(stmt).scalar_one()


def list_leads(
    db: Session,
    search: str | None = None,
    review_status: str | None = None,
    limit: int | None = 50,
    offset: int = 0,
    run_id: int | None = None,
    ungrouped: bool = False,
) -> list[Lead]:
    stmt = _apply_filters(
        select(Lead).order_by(Lead.id.desc()), search, review_status, run_id, ungrouped
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.execute(stmt.offset(offset)).scalars().all())


def get_lead(db: Session, lead_id: int) -> Lead | None:
    return db.get(Lead, lead_id)


def get_lead_by_fsq(db: Session, fsq_place_id: str) -> Lead | None:
    stmt = select(Lead).where(Lead.fsq_place_id == fsq_place_id)
    return db.execute(stmt).scalar_one_or_none()


def _filter_fields(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if hasattr(Lead, key)}


def create_lead(db: Session, data: dict[str, Any]) -> Lead:
    lead = Lead(**_filter_fields(data))
    db.add(lead)
    db.commit()
    db.refresh(lead)
    logger.info("[LeadStore] Created lead id=%s fsq=%s", lead.id, lead.fsq_place_id)
    return lead


def upsert_lead(db: Session, data: dict[str, Any]) -> tuple[Lead, bool]:
    """Upsert a single lead by fsq_place_id.

    Returns the lead and whether it was newly created (True) or updated (False).
    """
    existing = get_lead_by_fsq(db, data.get("fsq_place_id", ""))
    fields = _filter_fields(data)
    if existing is None:
        lead = Lead(**fields)
        db.add(lead)
        db.flush()
        logger.info("[LeadStore] Created lead fsq=%s", lead.fsq_place_id)
        return lead, True

    if "run_id" in fields and existing.run_id is not None:
        fields.pop("run_id", None)

    for key, value in fields.items():
        setattr(existing, key, value)
    logger.info("[LeadStore] Upsert-updated lead id=%s fsq=%s", existing.id, existing.fsq_place_id)
    return existing, False


def upsert_leads(db: Session, items: list[dict[str, Any]]) -> dict[str, int]:
    """Persist (create or update) many leads in one transaction."""
    created = 0
    updated = 0
    failed = 0
    try:
        for data in items:
            if not data.get("fsq_place_id"):
                failed += 1
                continue
            _, was_created = upsert_lead(db, data)
            if was_created:
                created += 1
            else:
                updated += 1
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("[LeadStore] Batch upsert failed")
        raise
    logger.info(
        "[LeadStore] Batch upsert complete created=%s updated=%s failed=%s",
        created, updated, failed,
    )
    return {"created": created, "updated": updated, "failed": failed}


def update_lead(db: Session, lead: Lead, data: dict[str, Any]) -> Lead:
    for key, value in _filter_fields(data).items():
        setattr(lead, key, value)
    db.commit()
    db.refresh(lead)
    logger.info("[LeadStore] Updated lead id=%s", lead.id)
    return lead


def delete_lead(db: Session, lead: Lead) -> None:
    db.delete(lead)
    db.commit()
    logger.info("[LeadStore] Deleted lead id=%s", lead.id)


def delete_leads(
    db: Session,
    search: str | None = None,
    review_status: str | None = None,
    run_id: int | None = None,
    ungrouped: bool = False,
) -> int:
    stmt = _apply_filters(
        delete(Lead), search, review_status, run_id, ungrouped
    )
    result = db.execute(stmt)
    db.commit()
    deleted = result.rowcount or 0
    logger.info("[LeadStore] Deleted %s lead(s)", deleted)
    return deleted


def get_stats(db: Session) -> dict[str, Any]:
    total = db.execute(select(func.count()).select_from(Lead)).scalar_one()
    status_rows = db.execute(
        select(Lead.review_status, func.count()).group_by(Lead.review_status)
    ).all()
    by_status = {row[0] or "": int(row[1]) for row in status_rows}
    avg_confidence = db.execute(
        select(func.avg(Lead.confidence_score)).select_from(Lead)
    ).scalar()
    with_email = db.execute(
        select(func.count()).select_from(Lead).where(Lead.email.is_not(None))
    ).scalar_one()
    with_website = db.execute(
        select(func.count()).select_from(Lead).where(Lead.website.is_not(None))
    ).scalar_one()
    return {
        "total": total,
        "by_status": by_status,
        "avg_confidence": round(float(avg_confidence or 0.0), 4),
        "with_email": with_email,
        "with_website": with_website,
    }


# ── Deduplication helpers ──────────────────────────────────────────

def get_all_known_place_ids(db: Session) -> set[str]:
    """Return every fsq_place_id already in leads or exclusions — used to filter search results."""
    lead_ids = set(db.execute(select(Lead.fsq_place_id)).scalars().all())
    exclusion_ids = set(
        db.execute(select(DiscoveryExclusion.fsq_place_id)).scalars().all()
    )
    return lead_ids | exclusion_ids


def add_exclusion(db: Session, fsq_place_id: str, reason: str = "") -> None:
    existing = db.get(DiscoveryExclusion, fsq_place_id)
    if existing is not None:
        return
    db.add(DiscoveryExclusion(fsq_place_id=fsq_place_id, reason=reason))
    db.commit()
    logger.info("[Exclusion] Added %s: %s", fsq_place_id, reason)


def remove_exclusion(db: Session, fsq_place_id: str) -> bool:
    entry = db.get(DiscoveryExclusion, fsq_place_id)
    if entry is None:
        return False
    db.delete(entry)
    db.commit()
    logger.info("[Exclusion] Removed %s", fsq_place_id)
    return True


def list_exclusions(db: Session) -> list[DiscoveryExclusion]:
    return list(
        db.execute(
            select(DiscoveryExclusion).order_by(DiscoveryExclusion.created_at.desc())
        ).scalars().all()
    )


# ── Run History ─────────────────────────────────────────────────────

_RUN_HISTORY_SEARCH_COLUMNS = (
    RunHistory.location,
    RunHistory.business_category,
    RunHistory.run_type,
    RunHistory.status,
)


def _apply_run_history_filters(
    stmt,
    search: str | None,
    run_type: str | None,
    status: str | None,
):
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(*(column.ilike(pattern) for column in _RUN_HISTORY_SEARCH_COLUMNS))
        )
    if run_type:
        stmt = stmt.where(RunHistory.run_type == run_type)
    if status:
        stmt = stmt.where(RunHistory.status == status)
    return stmt


def count_run_history(
    db: Session,
    search: str | None = None,
    run_type: str | None = None,
    status: str | None = None,
) -> int:
    stmt = _apply_run_history_filters(
        select(func.count()).select_from(RunHistory), search, run_type, status
    )
    return db.execute(stmt).scalar_one()


def list_run_history(
    db: Session,
    search: str | None = None,
    run_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RunHistory]:
    stmt = _apply_run_history_filters(
        select(RunHistory).order_by(RunHistory.id.desc()), search, run_type, status
    )
    return list(db.execute(stmt.limit(limit).offset(offset)).scalars().all())


def get_run_history(db: Session, run_id: int) -> RunHistory | None:
    return db.get(RunHistory, run_id)


def count_leads_for_run(db: Session, run_id: int) -> int:
    return db.execute(
        select(func.count()).select_from(Lead).where(Lead.run_id == run_id)
    ).scalar_one()


def list_lead_batches(db: Session) -> list[tuple[RunHistory, int]]:
    runs = db.execute(
        select(RunHistory)
        .where(RunHistory.run_type.in_(["search", "save_all"]))
        .order_by(RunHistory.id.desc())
    ).scalars().all()
    batches = []
    for run in runs:
        lead_count = count_leads_for_run(db, run.id)
        if lead_count > 0:
            batches.append((run, lead_count))
    return batches


def _run_history_fields(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if hasattr(RunHistory, key)}


def create_run_history(db: Session, data: dict[str, Any]) -> RunHistory:
    run = RunHistory(**_run_history_fields(data))
    db.add(run)
    db.commit()
    db.refresh(run)
    logger.info("[RunHistory] Created run id=%s type=%s", run.id, run.run_type)
    return run


def finish_run_history(db: Session, run: RunHistory, **updates: Any) -> RunHistory:
    for key, value in updates.items():
        if hasattr(run, key):
            setattr(run, key, value)
    db.commit()
    db.refresh(run)
    logger.info("[RunHistory] Finished run id=%s status=%s", run.id, run.status)
    return run


def delete_run_history(db: Session, run: RunHistory) -> None:
    db.delete(run)
    db.commit()
    logger.info("[RunHistory] Deleted run id=%s", run.id)


def clear_run_history(db: Session) -> int:
    count = db.execute(select(func.count()).select_from(RunHistory)).scalar_one()
    db.execute(RunHistory.__table__.delete())
    db.commit()
    logger.info("[RunHistory] Cleared %s run(s)", count)
    return count


def get_run_history_stats(db: Session) -> dict[str, Any]:
    total = count_run_history(db)
    status_rows = db.execute(
        select(RunHistory.status, func.count()).group_by(RunHistory.status)
    ).all()
    by_status = {row[0] or "": int(row[1]) for row in status_rows}
    avg = db.execute(
        select(func.avg(RunHistory.processing_time)).select_from(RunHistory)
    ).scalar()
    return {
        "total_runs": total,
        "successful": int(by_status.get("success", 0)),
        "failed": int(by_status.get("failed", 0)),
        "avg_duration": round(float(avg or 0.0), 3),
        "by_status": by_status,
    }

# ── Discovery checkpoints (resume support) ─────────────────────────

def get_run_checkpoint(db: Session, run_id: int) -> dict[str, Any]:
    """Return the resume state stored on a run (empty dict when none)."""
    run = get_run_history(db, run_id)
    if run is None or not run.checkpoint:
        return {}
    checkpoint = run.checkpoint
    return dict(checkpoint) if isinstance(checkpoint, dict) else {}


def save_run_checkpoint(db: Session, run_id: int, data: dict[str, Any]) -> None:
    """Overwrite a run's resume state. Called after every finished cell."""
    run = get_run_history(db, run_id)
    if run is None:
        return
    run.checkpoint = data
    db.commit()


def mark_cell_done(
    db: Session, run_id: int, cell_index: int, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Record one finished grid cell, returning the updated checkpoint."""
    checkpoint = get_run_checkpoint(db, run_id)
    done = set(checkpoint.get("cells_done", []))
    done.add(int(cell_index))
    checkpoint["cells_done"] = sorted(done)
    if extra:
        checkpoint.update(extra)
    save_run_checkpoint(db, run_id, checkpoint)
    return checkpoint


def get_lead_place_ids_for_run(db: Session, run_id: int) -> set[str]:
    """Place IDs already persisted for a run — used when resuming."""
    return set(
        db.execute(
            select(Lead.fsq_place_id).where(Lead.run_id == run_id)
        ).scalars().all()
    )
