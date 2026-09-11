from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.db.models import RunHistory, utcnow
from app.db.repository import create_run_history, finish_run_history

logger = logging.getLogger(__name__)

_STATUS_RUNNING = "running"
_STATUS_SUCCESS = "success"
_STATUS_FAILED = "failed"
_STATUS_INTERRUPTED = "interrupted"


def start_run(
    db: Session,
    run_type: str,
    location: str = "",
    business_category: str = "",
    total_tasks: int = 1,
) -> RunHistory:
    """Create a new run-history record in the 'running' state."""
    return create_run_history(
        db,
        {
            "run_type": run_type,
            "location": location or "",
            "business_category": business_category or "",
            "status": _STATUS_RUNNING,
            "total_tasks": total_tasks,
            "total_results": 0,
            "processing_time": 0.0,
            "started_at": utcnow(),
            "finished_at": None,
            "error_message": None,
        },
    )


def finish_run(
    db: Session,
    run: RunHistory,
    *,
    status: str,
    total_results: int = 0,
    error_message: str | None = None,
    processing_time: float | None = None,
    total_tasks: int | None = None,
) -> RunHistory:
    """Finalize a run-history record with outcome, timing, and error details."""
    updates: dict = {
        "status": status,
        "total_results": total_results,
        "finished_at": utcnow(),
    }
    if error_message is not None:
        updates["error_message"] = str(error_message)[:2000]
    if processing_time is not None:
        updates["processing_time"] = processing_time
    if total_tasks is not None:
        updates["total_tasks"] = total_tasks
    return finish_run_history(db, run, **updates)


def mark_interrupted_runs(db: Session) -> int:
    """Close out runs left 'running' by a previous process.

    Discovery jobs live in memory, so a run still marked running at startup
    died with the old process. The checkpoint is left intact so the run can
    still be resumed.
    """
    runs = (
        db.query(RunHistory).filter(RunHistory.status == _STATUS_RUNNING).all()
    )
    for run in runs:
        run.status = _STATUS_INTERRUPTED
        run.finished_at = utcnow()
        run.error_message = run.error_message or "Server stopped before the run finished"
    if runs:
        db.commit()
        logger.info("[RunHistory] Marked %d orphaned run(s) interrupted", len(runs))
    return len(runs)


__all__ = [
    "_STATUS_RUNNING",
    "_STATUS_SUCCESS",
    "_STATUS_FAILED",
    "_STATUS_INTERRUPTED",
    "start_run",
    "finish_run",
    "mark_interrupted_runs",
]
