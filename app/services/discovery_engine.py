"""Grid discovery engine — concurrent, adaptive, checkpointed.

Replaces the old fixed-pause staggered crawler:

* the area is split into a grid of cells, each searched independently,
* cells run in small concurrent batches instead of strictly one at a time,
* pacing comes from an AdaptiveRateLimiter (fast until the provider pushes
  back) rather than a fixed sleep between cells,
* every finished cell is handed to `on_cell` immediately, so results are saved
  as they arrive and an interrupted run can resume from the last cell,
* a cell that fails on the primary provider is retried on the free fallback.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import NoResultsError, ProviderError, RateLimitError
from app.core.rate_limiter import AdaptiveRateLimiter
from app.providers.base import BaseProvider, Business, SearchQuery

logger = logging.getLogger(__name__)

_METERS_PER_DEG_LAT = 111_320.0


@dataclass(frozen=True)
class CellPlan:
    """One grid cell to search."""

    index: int
    lat: float | None
    lng: float | None
    radius: int
    location_text: str | None = None

    @property
    def location(self) -> str | None:
        if self.lat is None or self.lng is None:
            return self.location_text
        return f"{self.lat:.6f},{self.lng:.6f}"


@dataclass
class CellOutcome:
    index: int
    status: str  # ok | empty | error | skipped | cancelled
    provider: str = ""
    found: int = 0
    new: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "status": self.status,
            "provider": self.provider,
            "found": self.found,
            "new": self.new,
            "error": self.error,
        }


@dataclass
class DiscoveryReport:
    businesses: list[Business] = field(default_factory=list)
    outcomes: list[CellOutcome] = field(default_factory=list)
    cells_planned: int = 0
    cancelled: bool = False
    limiter_stats: dict[str, Any] = field(default_factory=dict)

    @property
    def cells_done(self) -> int:
        return sum(1 for o in self.outcomes if o.status in ("ok", "empty", "error"))

    @property
    def errors(self) -> list[str]:
        return [o.error for o in self.outcomes if o.error]


def plan_grid(
    lat: float, lng: float, radius: int, grid_size: int
) -> list[tuple[float, float, int]]:
    """Tile a circle of `radius` with grid_size x grid_size overlapping cells."""
    grid_size = max(1, int(grid_size))
    if grid_size == 1:
        return [(lat, lng, radius)]

    # Cell centres are spaced so their circles overlap slightly and leave no gap.
    span = 2.0 * radius
    step = span / grid_size
    cell_radius = max(100, int(step * 0.75))
    lat_step = step / _METERS_PER_DEG_LAT
    lng_step = step / (_METERS_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.15))

    half = (grid_size - 1) / 2.0
    cells: list[tuple[float, float, int]] = []
    for row in range(grid_size):
        for col in range(grid_size):
            cells.append((
                lat + (row - half) * lat_step,
                lng + (col - half) * lng_step,
                cell_radius,
            ))
    return cells


class DiscoveryEngine:
    def __init__(
        self,
        primary: BaseProvider,
        fallback: BaseProvider | None = None,
        *,
        limiter: AdaptiveRateLimiter | None = None,
        concurrency: int | None = None,
        seen: set[str] | None = None,
        on_cell: Callable[[CellOutcome, list[Business]], None] | None = None,
        on_progress: Callable[[DiscoveryReport], None] | None = None,
        geocoder: Callable[[str], Any] | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback if fallback is not primary else None
        self.limiter = (
            limiter
            or getattr(primary, "limiter", None)
            or AdaptiveRateLimiter(name=primary.provider_name)
        )
        # A fallback provider paces itself on its own limiter (different host,
        # different budget); the engine drives whichever one it is calling.
        self._limiters = {primary.provider_name: self.limiter}
        if self.fallback is not None:
            self._limiters[self.fallback.provider_name] = (
                getattr(self.fallback, "limiter", None)
                or AdaptiveRateLimiter(name=self.fallback.provider_name)
            )
        self.concurrency = max(1, concurrency or settings.discovery_concurrency)
        self.seen = seen if seen is not None else set()
        self._on_cell = on_cell
        self._on_progress = on_progress
        self._geocoder = geocoder
        self._lock = asyncio.Lock()

    def limiter_for(self, provider: BaseProvider) -> AdaptiveRateLimiter:
        return self._limiters.get(provider.provider_name, self.limiter)

    # ── planning ──────────────────────────────────────────────────

    async def resolve_center(self, location: str | None) -> tuple[float, float] | None:
        coords = _parse_coordinates(location)
        if coords or not location:
            return coords
        for candidate in (self._geocoder, getattr(self.primary, "geocode", None),
                          getattr(self.fallback, "geocode", None)):
            if candidate is None:
                continue
            try:
                resolved = await candidate(location)
            except Exception:  # noqa: BLE001 — geocoding is best-effort
                logger.warning("[Discovery] geocoder failed for %r", location, exc_info=True)
                continue
            if resolved:
                return resolved
        return None

    async def plan(
        self, query: SearchQuery, grid_size: int
    ) -> list[CellPlan]:
        if grid_size <= 1:
            return [CellPlan(0, None, None, query.radius, query.location)]

        center = await self.resolve_center(query.location)
        if center is None:
            logger.warning(
                "[Discovery] could not resolve %r to coordinates — single-cell search",
                query.location,
            )
            return [CellPlan(0, None, None, query.radius, query.location)]

        lat, lng = center
        return [
            CellPlan(idx, c_lat, c_lng, c_radius)
            for idx, (c_lat, c_lng, c_radius) in enumerate(
                plan_grid(lat, lng, query.radius, grid_size)
            )
        ]

    # ── execution ─────────────────────────────────────────────────

    async def discover(
        self,
        query: SearchQuery,
        grid_size: int = 3,
        max_total: int = 500,
        skip_cells: frozenset[int] | set[int] = frozenset(),
        cancel: asyncio.Event | None = None,
        per_cell_max: int = 200,
    ) -> DiscoveryReport:
        cells = await self.plan(query, grid_size)
        report = DiscoveryReport(cells_planned=len(cells))
        semaphore = asyncio.Semaphore(self.concurrency)
        cancel = cancel or asyncio.Event()

        for cell in cells:
            if cell.index in skip_cells:
                report.outcomes.append(CellOutcome(cell.index, "skipped"))

        pending = [cell for cell in cells if cell.index not in skip_cells]

        def _stop_reason() -> str | None:
            if cancel.is_set():
                return "cancelled"
            if len(report.businesses) >= max_total:
                return "capped"
            return None

        async def run_cell(cell: CellPlan) -> None:
            reason = _stop_reason()
            if reason:
                await self._record(report, CellOutcome(cell.index, reason), [])
                return
            async with semaphore:
                reason = _stop_reason()
                if reason:
                    await self._record(report, CellOutcome(cell.index, reason), [])
                    return
                outcome, businesses = await self._search_cell(cell, query, per_cell_max)
                await self._record(report, outcome, businesses, limit=max_total)

        await asyncio.gather(*(run_cell(cell) for cell in pending))

        report.cancelled = cancel.is_set()
        report.outcomes.sort(key=lambda o: o.index)
        report.limiter_stats = self.limiter.stats().to_dict()
        del report.businesses[max_total:]
        return report

    async def _record(
        self,
        report: DiscoveryReport,
        outcome: CellOutcome,
        businesses: list[Business],
        limit: int | None = None,
    ) -> None:
        """Deduplicate, append, and flush one cell's results — under a lock.

        ``limit`` caps the run's total. It is applied here, before the results
        are handed off, because the callback saves them straight away.
        """
        async with self._lock:
            fresh: list[Business] = []
            for business in businesses:
                if limit is not None and len(report.businesses) + len(fresh) >= limit:
                    break
                place_id = business.fsq_place_id
                if not place_id or place_id in self.seen:
                    continue
                self.seen.add(place_id)
                fresh.append(business)
            outcome.new = len(fresh)
            report.businesses.extend(fresh)
            report.outcomes.append(outcome)

        # Hand results off immediately so they are persisted before the next cell.
        if self._on_cell is not None:
            try:
                self._on_cell(outcome, fresh)
            except Exception:  # noqa: BLE001 — a checkpoint failure must not kill the run
                logger.exception("[Discovery] on_cell callback failed (cell %d)", outcome.index)
        if self._on_progress is not None:
            try:
                self._on_progress(report)
            except Exception:  # noqa: BLE001
                logger.exception("[Discovery] on_progress callback failed")

    async def _search_cell(
        self, cell: CellPlan, query: SearchQuery, per_cell_max: int
    ) -> tuple[CellOutcome, list[Business]]:
        cell_query = SearchQuery(
            query=query.query,
            location=cell.location,
            radius=cell.radius,
            type=query.type,
            keyword=query.keyword,
            min_rating=query.min_rating,
            max_results=per_cell_max,
        )

        providers = [self.primary] + ([self.fallback] if self.fallback else [])
        last_error: str | None = None

        for attempt, provider in enumerate(providers):
            is_fallback = attempt > 0
            limiter = self.limiter_for(provider)
            try:
                await limiter.acquire()
                result = await provider.search(cell_query)
                limiter.record_success()
                businesses = list(result.businesses)
                if not businesses:
                    return CellOutcome(cell.index, "empty", provider.provider_name), []
                return (
                    CellOutcome(
                        cell.index, "ok", provider.provider_name, found=len(businesses)
                    ),
                    businesses,
                )
            except NoResultsError:
                if not is_fallback and self.fallback is not None:
                    continue  # the free provider may still know this area
                return CellOutcome(cell.index, "empty", provider.provider_name), []
            except RateLimitError as exc:
                retry_after = (exc.details or {}).get("retry_after")
                limiter.record_throttled(retry_after)
                last_error = f"{provider.provider_name}: rate limited"
                logger.warning("[Discovery] cell %d rate limited on %s",
                               cell.index, provider.provider_name)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (429, 500, 502, 503, 504):
                    limiter.record_throttled(None)
                else:
                    limiter.record_error()
                last_error = f"{provider.provider_name}: HTTP {status}"
            except (ProviderError, httpx.HTTPError) as exc:
                limiter.record_error()
                last_error = f"{provider.provider_name}: {exc}"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad cell must not kill the run
                limiter.record_error()
                last_error = f"{provider.provider_name}: {type(exc).__name__}: {exc}"
                logger.exception("[Discovery] cell %d unexpected error", cell.index)

        return CellOutcome(cell.index, "error", error=last_error), []


def _parse_coordinates(location: str | None) -> tuple[float, float] | None:
    if not location or "," not in location:
        return None
    parts = [part.strip() for part in location.split(",")]
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


__all__ = [
    "CellOutcome",
    "CellPlan",
    "DiscoveryEngine",
    "DiscoveryReport",
    "plan_grid",
]
