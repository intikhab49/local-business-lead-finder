import asyncio

import httpx
import pytest

from app.core.exceptions import NoResultsError, ProviderError, RateLimitError
from app.core.rate_limiter import AdaptiveRateLimiter
from app.providers.base import BaseProvider, Business, SearchQuery, SearchResult
from app.services.discovery_engine import DiscoveryEngine, plan_grid

CHICAGO = "41.8781,-87.6298"


def business(place_id: str) -> Business:
    return Business(
        fsq_place_id=place_id,
        name=f"Business {place_id}",
        formatted_address="1 Main Street",
    )


class FakeProvider(BaseProvider):
    """Provider whose per-cell behaviour is scripted by the test."""

    def __init__(self, name="fake", results=None, errors=None, limiter=None):
        super().__init__(api_key="k", base_url="http://x", timeout=1, max_results=100)
        self._name = name
        self._results = results or {}
        self._errors = errors or {}
        self.calls: list[SearchQuery] = []
        self.limiter = limiter or AdaptiveRateLimiter(
            min_interval=0.0, max_interval=0.1, name=name
        )

    @property
    def provider_name(self) -> str:
        return self._name

    async def search(self, query: SearchQuery) -> SearchResult:
        self.calls.append(query)
        key = len(self.calls) - 1
        location = query.location or ""
        error = self._errors.get(location) or self._errors.get(key)
        if error is not None:
            raise error
        ids = self._results.get(location, self._results.get(key, ["a", "b"]))
        businesses = [business(f"{self._name}-{i}") for i in ids]
        return SearchResult(businesses=businesses, total_results=len(businesses))

    async def get_details(self, place_id: str) -> Business:
        return business(place_id)

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def query(location=CHICAGO, radius=4000) -> SearchQuery:
    return SearchQuery(query="dentist", location=location, radius=radius, type="dentist")


class TestPlanGrid:
    def test_single_cell_keeps_the_full_radius(self):
        assert plan_grid(41.0, -87.0, 5000, 1) == [(41.0, -87.0, 5000)]

    def test_grid_size_produces_n_squared_cells(self):
        assert len(plan_grid(41.0, -87.0, 5000, 3)) == 9
        assert len(plan_grid(41.0, -87.0, 5000, 4)) == 16

    def test_cells_are_distinct_and_smaller_than_the_area(self):
        cells = plan_grid(41.0, -87.0, 6000, 3)
        centers = {(round(lat, 6), round(lng, 6)) for lat, lng, _ in cells}
        assert len(centers) == 9
        assert all(radius < 6000 for _, _, radius in cells)


class TestDiscovery:
    async def test_every_cell_is_searched(self):
        provider = FakeProvider()
        engine = DiscoveryEngine(provider, concurrency=3)
        report = await engine.discover(query(), grid_size=3, max_total=500)
        assert report.cells_planned == 9
        assert len(provider.calls) == 9
        assert report.cells_done == 9

    async def test_results_are_deduplicated_across_cells(self):
        provider = FakeProvider(results={i: ["dup", f"uniq{i}"] for i in range(9)})
        engine = DiscoveryEngine(provider, concurrency=2)
        report = await engine.discover(query(), grid_size=3, max_total=500)
        ids = [b.fsq_place_id for b in report.businesses]
        assert len(ids) == len(set(ids))
        assert ids.count("fake-dup") == 1

    async def test_already_seen_places_are_filtered(self):
        provider = FakeProvider(results={i: ["known"] for i in range(9)})
        engine = DiscoveryEngine(provider, concurrency=2, seen={"fake-known"})
        report = await engine.discover(query(), grid_size=2, max_total=500)
        assert report.businesses == []

    async def test_cells_run_concurrently(self):
        class SlowProvider(FakeProvider):
            async def search(self, q):
                await asyncio.sleep(0.05)
                return await super().search(q)

        provider = SlowProvider()
        engine = DiscoveryEngine(provider, concurrency=4)
        started = asyncio.get_event_loop().time()
        await engine.discover(query(), grid_size=2, max_total=500)
        elapsed = asyncio.get_event_loop().time() - started
        assert elapsed < 0.19, "4 cells should overlap, not run one after another"

    async def test_max_total_caps_collection(self):
        provider = FakeProvider(
            results={i: [f"c{i}-{n}" for n in range(10)] for i in range(9)}
        )
        engine = DiscoveryEngine(provider, concurrency=1)
        report = await engine.discover(query(), grid_size=3, max_total=12)
        assert len(report.businesses) <= 12
        assert any(o.status == "capped" for o in report.outcomes)

    async def test_max_total_caps_what_on_cell_hands_off(self):
        # on_cell saves leads immediately, so the cap must hold before the hand-off.
        provider = FakeProvider(
            results={i: [f"c{i}-{n}" for n in range(10)] for i in range(9)}
        )
        flushed: list[int] = []
        engine = DiscoveryEngine(
            provider, concurrency=4,
            on_cell=lambda outcome, businesses: flushed.append(len(businesses)),
        )
        report = await engine.discover(query(), grid_size=3, max_total=12)
        assert sum(flushed) == 12
        assert len(report.businesses) == 12

    async def test_skip_cells_supports_resume(self):
        provider = FakeProvider()
        engine = DiscoveryEngine(provider, concurrency=2)
        report = await engine.discover(
            query(), grid_size=3, max_total=500, skip_cells={0, 1, 2, 3}
        )
        assert len(provider.calls) == 5
        assert sum(1 for o in report.outcomes if o.status == "skipped") == 4

    async def test_on_cell_receives_results_as_they_finish(self):
        provider = FakeProvider(results={i: [i] for i in range(4)})
        flushed: list[tuple[str, int]] = []

        def on_cell(outcome, businesses):
            flushed.append((outcome.status, len(businesses)))

        engine = DiscoveryEngine(provider, concurrency=1, on_cell=on_cell)
        await engine.discover(query(), grid_size=2, max_total=500)
        assert len(flushed) == 4
        assert all(status == "ok" for status, _ in flushed)

    async def test_cancellation_stops_remaining_cells(self):
        cancel = asyncio.Event()

        class CancellingProvider(FakeProvider):
            async def search(self, q):
                cancel.set()
                return await super().search(q)

        provider = CancellingProvider()
        engine = DiscoveryEngine(provider, concurrency=1)
        report = await engine.discover(
            query(), grid_size=3, max_total=500, cancel=cancel
        )
        assert report.cancelled is True
        assert len(provider.calls) == 1
        assert any(o.status == "cancelled" for o in report.outcomes)


class TestResilience:
    async def test_provider_error_is_isolated_to_one_cell(self):
        errors = {0: ProviderError(message="boom", provider="fake")}
        provider = FakeProvider(errors=errors)
        engine = DiscoveryEngine(provider, concurrency=1)
        report = await engine.discover(query(), grid_size=2, max_total=500)
        assert sum(1 for o in report.outcomes if o.status == "error") == 1
        assert sum(1 for o in report.outcomes if o.status == "ok") == 3

    async def test_fallback_provider_covers_a_failed_cell(self):
        primary = FakeProvider(
            name="primary",
            errors={i: ProviderError(message="down", provider="primary") for i in range(4)},
        )
        fallback = FakeProvider(name="free")
        engine = DiscoveryEngine(primary, fallback, concurrency=1)
        report = await engine.discover(query(), grid_size=2, max_total=500)
        assert len(fallback.calls) == 4
        assert all(b.fsq_place_id.startswith("free-") for b in report.businesses)
        assert all(o.provider == "free" for o in report.outcomes if o.status == "ok")

    async def test_fallback_used_when_primary_has_no_results(self):
        primary = FakeProvider(
            name="primary",
            errors={i: NoResultsError(provider="primary", query="x") for i in range(4)},
        )
        fallback = FakeProvider(name="free")
        engine = DiscoveryEngine(primary, fallback, concurrency=1)
        report = await engine.discover(query(), grid_size=2, max_total=500)
        assert len(report.businesses) > 0

    async def test_rate_limit_widens_the_interval(self):
        primary = FakeProvider(
            name="primary",
            errors={i: RateLimitError("primary", retry_after=1) for i in range(4)},
        )
        engine = DiscoveryEngine(primary, concurrency=1)
        before = engine.limiter.interval
        await engine.discover(query(), grid_size=2, max_total=500)
        assert engine.limiter.interval > before
        assert engine.limiter.stats().throttled == 4

    async def test_http_5xx_is_treated_as_throttling(self):
        response = httpx.Response(503, request=httpx.Request("POST", "http://x"))
        error = httpx.HTTPStatusError("busy", request=response.request, response=response)
        provider = FakeProvider(errors={0: error})
        engine = DiscoveryEngine(provider, concurrency=1)
        report = await engine.discover(query(), grid_size=2, max_total=500)
        assert engine.limiter.stats().throttled == 1
        assert any(o.status == "error" and "503" in (o.error or "") for o in report.outcomes)

    async def test_unexpected_exception_does_not_kill_the_run(self):
        provider = FakeProvider(errors={0: ValueError("weird")})
        engine = DiscoveryEngine(provider, concurrency=1)
        report = await engine.discover(query(), grid_size=2, max_total=500)
        assert report.cells_done == 4
        assert any("ValueError" in (o.error or "") for o in report.outcomes)

    async def test_on_cell_failure_does_not_stop_the_crawl(self):
        def broken(outcome, businesses):
            raise RuntimeError("db down")

        provider = FakeProvider()
        engine = DiscoveryEngine(provider, concurrency=1, on_cell=broken)
        report = await engine.discover(query(), grid_size=2, max_total=500)
        assert report.cells_done == 4


class TestPlanning:
    async def test_text_location_falls_back_to_a_single_cell(self):
        provider = FakeProvider()
        engine = DiscoveryEngine(provider, concurrency=1)
        report = await engine.discover(
            query(location="Nowhere Town"), grid_size=3, max_total=500
        )
        assert report.cells_planned == 1
        assert provider.calls[0].location == "Nowhere Town"

    async def test_geocoder_enables_the_grid_for_text_locations(self):
        async def geocode(location: str):
            return (41.0, -87.0)

        provider = FakeProvider()
        engine = DiscoveryEngine(provider, concurrency=2, geocoder=geocode)
        report = await engine.discover(
            query(location="Chicago"), grid_size=3, max_total=500
        )
        assert report.cells_planned == 9

    async def test_provider_limiter_is_reused(self):
        limiter = AdaptiveRateLimiter(min_interval=0.0, name="shared")
        provider = FakeProvider(limiter=limiter)
        engine = DiscoveryEngine(provider, concurrency=1)
        assert engine.limiter is limiter
