"""Background discovery jobs.

A search no longer blocks one long HTTP request. `start()` returns a job id
immediately; the crawl runs as an asyncio task that:

* persists every finished cell right away (when auto-save is on),
* records the cell in the run's checkpoint so the job can be resumed,
* publishes live progress the UI polls,
* can be cancelled at any point without losing what it already found.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.db import repository
from app.db.database import get_session_local, init_db
from app.dependencies import resolve_providers
from app.providers.base import Business, SearchQuery
from app.services.discovery_engine import CellOutcome, DiscoveryEngine, DiscoveryReport
from app.services.enrichment_service import EnrichmentService
from app.services.lead_persistence_service import (
    clean_categories,
    parse_location_fields,
)
from app.services.run_history_service import finish_run, start_run

logger = logging.getLogger(__name__)

_ENRICHABLE_SOCIALS = ("linkedin", "facebook", "instagram", "twitter", "youtube")


@dataclass
class DiscoveryParams:
    location: str
    business_category: str
    radius: int = 5000
    keyword: str | None = None
    min_rating: float | None = None
    max_results: int = 500
    grid_size: int = 3
    exclude_saved: bool = True
    auto_save: bool = True
    auto_enrich: bool = True
    provider: str | None = None
    fallback_provider: str | None = None
    concurrency: int | None = None
    resume_run_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class JobState:
    id: str
    params: DiscoveryParams
    status: str = "queued"  # queued | running | completed | cancelled | failed
    run_id: int | None = None
    provider: str = ""
    fallback: str | None = None
    cells_total: int = 0
    cells_done: int = 0
    cells_skipped: int = 0
    cells_stopped: int = 0  # never searched: result cap reached, or cancelled
    phase: str = "starting"  # starting | searching | enriching | done
    found: int = 0
    saved: int = 0
    enriched: int = 0
    duplicates_skipped: int = 0
    message: str = "Queued"
    errors: list[str] = field(default_factory=list)
    leads: list[dict[str, Any]] = field(default_factory=list)
    cell_log: list[dict[str, Any]] = field(default_factory=list)
    limiter: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    resumed_from: int | None = None
    _cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "cancelled", "failed")

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def status_dict(self) -> dict[str, Any]:
        # Cells that were skipped (resume) or never needed (cap reached,
        # cancelled) still count as settled — otherwise the bar looks stuck.
        settled = self.cells_done + self.cells_skipped + self.cells_stopped
        progress = settled / self.cells_total if self.cells_total else 0.0
        return {
            "job_id": self.id,
            "status": self.status,
            "message": self.message,
            "run_id": self.run_id,
            "provider": self.provider,
            "fallback_provider": self.fallback,
            "cells_total": self.cells_total,
            "cells_done": self.cells_done,
            "cells_skipped": self.cells_skipped,
            "cells_stopped": self.cells_stopped,
            "phase": self.phase,
            "progress": round(min(1.0, progress), 4),
            "found": self.found,
            "saved": self.saved,
            "enriched": self.enriched,
            "duplicates_skipped": self.duplicates_skipped,
            "errors": self.errors[-20:],
            "cell_log": self.cell_log[-60:],
            "rate_limit": self.limiter,
            "elapsed": round(self.elapsed, 1),
            "resumed_from": self.resumed_from,
            "auto_save": self.params.auto_save,
            "params": self.params.to_dict(),
        }


class DiscoveryJobManager:
    """In-process registry of running/finished discovery jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self, params: DiscoveryParams) -> JobState:
        self._purge()
        job = JobState(id=uuid.uuid4().hex[:12], params=params)
        self._jobs[job.id] = job
        job._task = asyncio.create_task(self._run(job))
        return job

    def get(self, job_id: str) -> JobState | None:
        return self._jobs.get(job_id)

    def list(self) -> list[JobState]:
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.is_terminal:
            return False
        job._cancel.set()
        job.message = "Cancelling — finishing in-flight cells"
        return True

    def _purge(self) -> None:
        cutoff = time.time() - settings.discovery_job_ttl
        for job_id, job in list(self._jobs.items()):
            if job.is_terminal and (job.finished_at or 0) < cutoff:
                self._jobs.pop(job_id, None)

    # ── the job body ──────────────────────────────────────────────

    async def _run(self, job: JobState) -> None:
        params = job.params
        init_db()
        session_factory = get_session_local()
        db = session_factory()
        started = time.monotonic()
        enrichment = EnrichmentService() if params.auto_enrich else None

        try:
            primary, fallback = resolve_providers(
                params.provider, params.fallback_provider
            )
            job.provider = primary.provider_name
            job.fallback = fallback.provider_name if fallback else None

            skip_cells: set[int] = set()
            seen: set[str] = set()

            if params.resume_run_id:
                run = repository.get_run_history(db, params.resume_run_id)
                if run is None:
                    raise ValueError(f"Run #{params.resume_run_id} not found")
                checkpoint = repository.get_run_checkpoint(db, run.id)
                skip_cells = {int(i) for i in checkpoint.get("cells_done", [])}
                seen |= repository.get_lead_place_ids_for_run(db, run.id)
                job.resumed_from = run.id
                job.cells_skipped = len(skip_cells)
                logger.info(
                    "[Job %s] resuming run #%d — %d cells already done",
                    job.id, run.id, len(skip_cells),
                )
            else:
                run = start_run(
                    db, "discovery",
                    location=params.location,
                    business_category=params.business_category,
                )
            job.run_id = run.id

            if params.exclude_saved:
                seen |= repository.get_all_known_place_ids(db)

            job.status = "running"
            job.phase = "searching"
            job.message = f"Searching with {job.provider}"

            def on_cell(outcome: CellOutcome, businesses: list[Business]) -> None:
                self._handle_cell(job, db, outcome, businesses)

            def on_progress(report: DiscoveryReport) -> None:
                job.cells_total = report.cells_planned
                job.cells_done = sum(
                    1 for o in report.outcomes if o.status in ("ok", "empty", "error")
                )
                job.cells_stopped = sum(
                    1 for o in report.outcomes if o.status in ("capped", "cancelled")
                )
                job.limiter = report.limiter_stats or job.limiter

            engine = DiscoveryEngine(
                primary, fallback,
                concurrency=params.concurrency,
                seen=seen,
                on_cell=on_cell,
                on_progress=on_progress,
            )

            query = SearchQuery(
                query=params.business_category.strip(),
                location=params.location.strip(),
                radius=params.radius,
                type=params.business_category.strip().lower(),
                keyword=params.keyword.strip() if params.keyword else None,
                min_rating=params.min_rating,
                max_results=params.max_results,
            )

            cells = await engine.plan(query, params.grid_size)
            job.cells_total = len(cells)
            repository.save_run_checkpoint(db, run.id, {
                **repository.get_run_checkpoint(db, run.id),
                "cells_planned": len(cells),
                "grid_size": params.grid_size,
                "params": params.to_dict(),
            })

            report = await engine.discover(
                query,
                grid_size=params.grid_size,
                max_total=params.max_results,
                skip_cells=frozenset(skip_cells),
                cancel=job._cancel,
            )

            job.limiter = report.limiter_stats
            job.cells_done = sum(
                1 for o in report.outcomes if o.status in ("ok", "empty", "error")
            )
            job.cells_stopped = sum(
                1 for o in report.outcomes if o.status in ("capped", "cancelled")
            )

            if params.auto_enrich and enrichment is not None:
                await self._enrich_job_leads(job, enrichment, db)

            job.status = "cancelled" if job._cancel.is_set() else "completed"
            job.phase = "done"
            job.message = self._final_message(job)
            finish_run(
                db, run,
                status="success" if job.status == "completed" else "cancelled",
                total_results=job.found,
                processing_time=time.monotonic() - started,
                total_tasks=job.cells_total,
                error_message="; ".join(job.errors[:5]) or None,
            )
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.message = "Cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 — surface, never crash the server
            logger.exception("[Job %s] failed", job.id)
            job.status = "failed"
            job.message = f"{type(exc).__name__}: {exc}"
            job.errors.append(str(exc))
            if job.run_id:
                run = repository.get_run_history(db, job.run_id)
                if run is not None:
                    finish_run(
                        db, run, status="failed",
                        total_results=job.found,
                        error_message=str(exc),
                        processing_time=time.monotonic() - started,
                    )
        finally:
            job.finished_at = time.time()
            db.close()

    # ── per-cell persistence ──────────────────────────────────────

    def _handle_cell(
        self, job: JobState, db, outcome: CellOutcome, businesses: list[Business]
    ) -> None:
        job.cell_log.append(outcome.to_dict())
        job.duplicates_skipped += max(0, outcome.found - outcome.new)
        if outcome.error:
            job.errors.append(f"cell {outcome.index}: {outcome.error}")

        rows = [b.to_dict() for b in businesses]
        job.leads.extend(rows)
        job.found = len(job.leads)

        if job.params.auto_save and rows and job.run_id:
            try:
                result = repository.upsert_leads(
                    db, [lead_row(r, job.params.business_category, job.run_id) for r in rows]
                )
                job.saved += result.get("created", 0) + result.get("updated", 0)
            except Exception:  # noqa: BLE001 — keep crawling even if a write fails
                logger.exception("[Job %s] failed to persist cell %d", job.id, outcome.index)
                job.errors.append(f"cell {outcome.index}: save failed")

        # Only a cell that actually ran may be checkpointed — a capped or
        # cancelled cell must be retried when the run is resumed.
        if job.run_id and outcome.status in ("ok", "empty"):
            try:
                repository.mark_cell_done(
                    db, job.run_id, outcome.index,
                    extra={"found": job.found, "saved": job.saved},
                )
            except Exception:  # noqa: BLE001
                logger.exception("[Job %s] checkpoint write failed", job.id)

        job.message = (
            f"{job.found} leads from {job.cells_done + 1}/{job.cells_total or '?'} cells"
        )

    # ── enrichment ────────────────────────────────────────────────

    async def _enrich_job_leads(
        self, job: JobState, enrichment: EnrichmentService, db
    ) -> None:
        """Scrape sites for contact details, reporting each one as it lands.

        One big all-or-nothing batch left the UI reading "enriched 0" for
        minutes and ignored Stop. Sites now run under a shared concurrency
        limit and are counted as they complete, so a single slow site never
        holds up the count — or the rest of the queue.
        """
        pending = [
            row for row in job.leads
            if row.get("website") and not row.get("auto_enriched")
        ]
        if not pending:
            return

        job.phase = "enriching"
        total = len(pending)
        semaphore = asyncio.Semaphore(max(1, settings.enrichment_concurrency))
        done = 0
        unsaved: list[dict[str, Any]] = []

        async def enrich_one(row: dict[str, Any]) -> dict[str, Any] | None:
            async with semaphore:
                if job._cancel.is_set():
                    return None
                try:
                    found = await enrichment.enrich_with_discovery(
                        Business.from_dict(row)
                    )
                except Exception:  # noqa: BLE001 — one bad site, not the batch
                    logger.exception(
                        "[Job %s] enrichment failed for %s", job.id,
                        row.get("fsq_place_id"),
                    )
                    return None
                return row if self._merge_enrichment(job, row, found) else None

        tasks = [asyncio.create_task(enrich_one(row)) for row in pending]
        try:
            for completed in asyncio.as_completed(tasks):
                updated = await completed
                done += 1
                if updated is not None:
                    unsaved.append(updated)
                job.message = (
                    f"Scraping websites for contact details — {done}/{total} "
                    f"({job.enriched} with new details)"
                )
                # Flush periodically so a crash or Stop keeps what was found.
                if job.params.auto_save and len(unsaved) >= 20:
                    self._save_enriched(job, db, unsaved)
                    unsaved = []
                if job._cancel.is_set():
                    break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if unsaved and job.params.auto_save:
                self._save_enriched(job, db, unsaved)

        if job._cancel.is_set():
            job.message = f"Stopped scraping — {done} of {total} sites checked"
        else:
            job.message = f"Checked {total} websites — {job.enriched} enriched"

    def _save_enriched(self, job: JobState, db, rows: list[dict[str, Any]]) -> None:
        if not rows or not job.run_id:
            return
        try:
            repository.upsert_leads(
                db,
                [lead_row(row, job.params.business_category, job.run_id) for row in rows],
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Job %s] failed to persist enrichment", job.id)

    def _merge_enrichment(self, job: JobState, row: dict[str, Any], found) -> bool:
        """Copy discovered contact details onto a lead row. True if anything stuck."""
        if not found:
            return False
        if found.email:
            row["email"] = found.email
        for key in _ENRICHABLE_SOCIALS:
            value = getattr(found, key, None)
            if value:
                row[key] = value
        if getattr(found, "confidence_score", None) is not None:
            row["confidence_score"] = found.confidence_score
        if getattr(found, "source", None):
            row["source"] = found.source
        row["auto_enriched"] = True
        job.enriched += 1
        return True

    @staticmethod
    def _final_message(job: JobState) -> str:
        bits = [f"{job.found} leads"]
        if job.params.auto_save:
            bits.append(f"{job.saved} saved")
        else:
            bits.append("not saved (auto-save off)")
        if job.enriched:
            bits.append(f"{job.enriched} enriched")
        if job.errors:
            bits.append(f"{len(job.errors)} cell error(s)")
        return " · ".join(bits)


def lead_row(
    business: dict[str, Any], category: str, run_id: int | None = None
) -> dict[str, Any]:
    """Map a provider business dict onto a Lead row."""
    address = business.get("formatted_address", "")
    location = parse_location_fields(address, business)
    row: dict[str, Any] = {
        "fsq_place_id": business.get("fsq_place_id", ""),
        "business_name": business.get("name", ""),
        "address": address,
        "categories": clean_categories(", ".join(business.get("categories") or [])),
        "city": location.get("city"),
        "state": location.get("state"),
        "country": location.get("country"),
        "phone": business.get("phone_number"),
        "website": business.get("website"),
        "email": business.get("email"),
        "latitude": business.get("latitude"),
        "longitude": business.get("longitude"),
        "confidence_score": business.get("confidence_score") or 0.0,
        "source": business.get("source") or category or "",
        "review_status": "discovered",
    }
    for key in _ENRICHABLE_SOCIALS:
        if business.get(key):
            row[key] = business[key]
    if run_id is not None:
        row["run_id"] = run_id
    return row


job_manager = DiscoveryJobManager()

__all__ = ["DiscoveryJobManager", "DiscoveryParams", "JobState", "job_manager", "lead_row"]
