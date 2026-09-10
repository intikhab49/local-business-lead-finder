import asyncio
import logging
import threading

import pytest

from app.monitoring.metrics import (
    MetricsCollector,
    get_metrics,
    reset_metrics,
    timer,
)


@pytest.fixture(autouse=True)
def _clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


class TestMetricsCollectorSingleton:
    def test_is_singleton(self):
        assert get_metrics() is get_metrics()
        assert isinstance(get_metrics(), MetricsCollector)

    def test_reset_creates_fresh_instance(self):
        first = get_metrics()
        reset_metrics()
        second = get_metrics()
        assert first is not second

    def test_singleton_across_threads(self):
        instances = set()
        lock = threading.Lock()

        def _get():
            inst = get_metrics()
            with lock:
                instances.add(inst)

        threads = [threading.Thread(target=_get) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(instances) == 1


class TestCounters:
    def test_increment_and_get(self):
        metrics = get_metrics()
        metrics.increment("searches_total")
        metrics.increment("searches_total")
        metrics.increment("scraper_calls", 3)
        assert metrics.get_counter("searches_total") == 2
        assert metrics.get_counter("scraper_calls") == 3
        assert metrics.get_counter("missing") == 0

    def test_increment_from_zero(self):
        metrics = get_metrics()
        assert metrics.get_counter("new_counter") == 0
        metrics.increment("new_counter")
        assert metrics.get_counter("new_counter") == 1


class TestAverages:
    def test_record_duration_and_average(self):
        metrics = get_metrics()
        metrics.record_duration("search", 0.5)
        metrics.record_duration("search", 1.5)
        assert metrics.get_average("search") == pytest.approx(1.0)

    def test_average_zero_when_no_samples(self):
        metrics = get_metrics()
        assert metrics.get_average("search") == 0.0

    def test_record_tool_duration(self):
        metrics = get_metrics()
        metrics.record_tool_duration("search_tool", 0.2)
        metrics.record_tool_duration("search_tool", 0.4)
        assert metrics.get_tool_count("search_tool") == 2
        assert metrics.get_tool_average("search_tool") == pytest.approx(0.3)
        assert metrics.get_tool_count("missing") == 0
        assert metrics.get_tool_average("missing") == 0.0


class TestRates:
    def test_cache_hit_rate(self):
        metrics = get_metrics()
        assert metrics.cache_hit_rate() == 0.0
        metrics.increment("cache_hits", 3)
        metrics.increment("cache_misses", 1)
        assert metrics.cache_hit_rate() == pytest.approx(0.75)

    def test_success_rate(self):
        metrics = get_metrics()
        assert metrics.success_rate("api") == 0.0
        metrics.increment("api_calls", 4)
        metrics.increment("api_failures", 1)
        assert metrics.success_rate("api") == pytest.approx(0.75)


class TestTimer:
    @pytest.mark.asyncio
    async def test_timer_records_duration(self):
        metrics = get_metrics()
        with timer("search"):
            await asyncio.sleep(0.01)
        assert metrics.get_counter("search_count") == 1
        assert metrics.get_average("search") > 0.0

    def test_timer_records_on_exception(self):
        metrics = get_metrics()
        with pytest.raises(ValueError):
            with timer("enrichment"):
                raise ValueError("boom")
        assert metrics.get_counter("enrichment_count") == 1
        assert metrics.get_average("enrichment") > 0.0


class TestSnapshot:
    def test_snapshot_structure(self):
        metrics = get_metrics()
        metrics.increment("searches_total", 2)
        metrics.increment("businesses_processed", 5)
        metrics.increment("cache_hits", 3)
        metrics.increment("cache_misses", 1)
        metrics.increment("api_calls", 4)
        metrics.increment("api_failures", 1)
        metrics.increment("retry_attempts", 7)
        metrics.increment("retry_successes", 6)
        metrics.increment("retry_failures", 1)
        metrics.record_duration("search", 0.5)
        metrics.record_duration("llm", 0.2)
        metrics.record_tool_duration("search", 0.1)

        snap = metrics.snapshot()

        assert snap["counters"]["searches_total"] == 2
        assert snap["counters"]["businesses_processed"] == 5
        assert snap["counters"]["cache_hits"] == 3
        assert snap["counters"]["cache_misses"] == 1
        assert snap["counters"]["retry_attempts"] == 7
        assert snap["counters"]["retry_successes"] == 6
        assert snap["counters"]["retry_failures"] == 1
        assert snap["averages"]["search"] == pytest.approx(0.5)
        assert snap["averages"]["llm"] == pytest.approx(0.2)
        assert snap["tools"]["search"]["calls"] == 1
        assert snap["tools"]["search"]["average_time_ms"] > 0
        assert snap["cache_hit_rate"] == pytest.approx(0.75)
        assert snap["success_rates"]["api"] == pytest.approx(0.75)
        assert snap["uptime_seconds"] >= 0

    def test_snapshot_empty(self):
        snap = get_metrics().snapshot()
        assert snap["counters"] == {}
        assert snap["averages"] == {}
        assert snap["tools"] == {}
        assert snap["cache_hit_rate"] == 0.0
        assert snap["success_rates"] == {}
        assert snap["uptime_seconds"] >= 0


class TestConcurrentUpdates:
    @pytest.mark.asyncio
    async def test_concurrent_increments_are_consistent(self):
        metrics = get_metrics()
        target = 500
        await asyncio.gather(
            *[
                asyncio.to_thread(metrics.increment, "concurrent_counter")
                for _ in range(target)
            ]
        )
        assert metrics.get_counter("concurrent_counter") == target

    @pytest.mark.asyncio
    async def test_concurrent_mixed_updates(self):
        metrics = get_metrics()

        async def _worker(n: int):
            for _ in range(100):
                metrics.increment("searches_total")
                metrics.record_duration("search", 0.001)
                metrics.record_tool_duration("tool_a", 0.001)

        await asyncio.gather(*[_worker(i) for i in range(10)])

        assert metrics.get_counter("searches_total") == 1000
        assert metrics.get_counter("search_count") == 1000
        assert metrics.get_tool_count("tool_a") == 1000
        assert metrics.get_average("search") == pytest.approx(0.001)
        assert metrics.get_tool_average("tool_a") == pytest.approx(0.001)

    @pytest.mark.asyncio
    async def test_thread_concurrent_snapshot(self):
        metrics = get_metrics()

        def _worker():
            for _ in range(200):
                metrics.increment("api_calls")

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert metrics.get_counter("api_calls") == 1600
        assert metrics.snapshot()["counters"]["api_calls"] == 1600


class TestMetricsLogging:
    def test_logs_summary(self, caplog):
        caplog.set_level(logging.INFO)
        metrics = get_metrics()
        metrics.increment("searches_total", 1)
        metrics.log_summary()
        assert any("[Metrics] Summary:" in msg for msg in caplog.messages)

    def test_logs_counter_update(self, caplog):
        caplog.set_level(logging.DEBUG)
        get_metrics().increment("searches_total")
        assert any("[Metrics] Incremented counter 'searches_total'" in msg for msg in caplog.messages)


class TestIntegrationPoints:
    @pytest.mark.asyncio
    async def test_cache_increments_hits_and_misses(self):
        from app.cache.cache_manager import CacheManager

        metrics = get_metrics()
        cache = CacheManager(ttl_seconds=60)

        cache.get("missing")
        cache.set("k1", "v1")
        cache.get("k1")

        assert metrics.get_counter("cache_misses") == 1
        assert metrics.get_counter("cache_hits") == 1
        assert metrics.cache_hit_rate() == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_scraper_records_calls(self, monkeypatch):
        from app.services.website_scraper import WebsiteScraperService

        metrics = get_metrics()

        async def _fake_download(self, url):
            return "<html><body>hello</body></html>"

        monkeypatch.setattr(WebsiteScraperService, "_download_page", _fake_download)
        scraper = WebsiteScraperService()
        await scraper.scrape("https://example.com")

        assert metrics.get_counter("scraper_calls") == 1
        assert metrics.get_counter("scraper_count") == 1
        assert metrics.get_counter("scraper_failures") == 0
        assert metrics.get_average("scraper") > 0.0
        assert metrics.success_rate("scraper") == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_scraper_records_failure(self, monkeypatch):
        from app.services.website_scraper import WebsiteScraperService

        metrics = get_metrics()

        async def _fake_download(self, url):
            return None

        monkeypatch.setattr(WebsiteScraperService, "_download_page", _fake_download)
        scraper = WebsiteScraperService()
        result = await scraper.scrape("https://example.com")

        assert result["emails"] == []
        assert metrics.get_counter("scraper_calls") == 1
        assert metrics.get_counter("scraper_failures") == 1

    @pytest.mark.asyncio
    async def test_search_records_searches(self, monkeypatch):
        from app.services.business_search_service import BusinessSearchService

        metrics = get_metrics()

        class FakeResult:
            businesses = []

        class FakeProvider:
            provider_name = "foursquare"

            async def search(self, query):
                return FakeResult()

            async def get_details(self, place_id):
                raise NotImplementedError

            async def close(self):
                pass

        service = BusinessSearchService(provider=FakeProvider())
        result = await service.search(
            location="New York",
            business_category="restaurant",
        )

        assert result == []
        assert metrics.get_counter("searches_total") == 1
        assert metrics.get_counter("search_count") == 1
        assert metrics.get_average("search") > 0.0

    @pytest.mark.asyncio
    async def test_tool_records_call_count(self):
        from app.tools.search_tool import SearchTool

        metrics = get_metrics()

        class FakeProvider:
            provider_name = "foursquare"

        class FakeSearchService:
            provider = FakeProvider()

            async def search(self, location="", business_category="", radius=5000, max_results=20):
                return [
                    {"fsq_place_id": "id1", "name": "Pizza", "formatted_address": "1 Main St"},
                ]

        tool = SearchTool(search_service=FakeSearchService())
        result = await tool.execute("search", None, location="NY", business_category="pizza")
        assert result.success is True
        assert metrics.get_tool_count("search") == 1
        assert metrics.get_tool_average("search") > 0.0

    @pytest.mark.asyncio
    async def test_gemini_records_calls_and_failures(self, monkeypatch):
        import httpx

        from app.llm.gemini_client import GeminiClient

        metrics = get_metrics()

        class FakeResponse:
            def __init__(self, text="", status_code=200):
                self.text = text
                self.status_code = status_code

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {self.status_code}",
                        request=httpx.Request("POST", "http://test"),
                        response=httpx.Response(self.status_code),
                    )

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                self.response = FakeResponse(
                    '{"candidates": [{"content": {"parts": [{"text": "{\\"actions\\": []}"}]}}]}'
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, *args, **kwargs):
                return self.response

        client = GeminiClient(api_key="k", model="m", base_url="https://t", timeout=5, max_retries=1)
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeAsyncClient())
        result = await client.generate("hi")
        assert result == {"actions": []}
        assert metrics.get_counter("gemini_calls") == 1
        assert metrics.get_counter("gemini_failures") == 0
        assert metrics.get_counter("llm_count") == 1
        assert metrics.get_average("llm") > 0.0
        assert metrics.success_rate("gemini") == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_retry_records_attempts(self):
        from app.core.retry import retry_async

        metrics = get_metrics()

        async def _op():
            raise ConnectionError("boom")

        with pytest.raises(ConnectionError):
            await retry_async(_op, max_retries=1, initial_delay=0, context="flaky")

        assert metrics.get_counter("retry_attempts") == 2
        assert metrics.get_counter("retry_successes") == 0
        assert metrics.get_counter("retry_failures") == 1

    @pytest.mark.asyncio
    async def test_retry_records_success(self):
        from app.core.retry import retry_async

        metrics = get_metrics()

        async def _op():
            return "ok"

        await retry_async(_op, max_retries=1, context="good")
        assert metrics.get_counter("retry_attempts") == 1
        assert metrics.get_counter("retry_successes") == 1
        assert metrics.get_counter("retry_failures") == 0


class TestMetricsEndpoint:
    def test_get_metrics_endpoint(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.routes.business import router

        app = FastAPI()
        app.include_router(router)

        metrics = get_metrics()
        metrics.increment("searches_total", 3)

        with TestClient(app) as client:
            response = client.get("/metrics")

        assert response.status_code == 200
        data = response.json()
        assert data["counters"]["searches_total"] == 3
        assert "uptime_seconds" in data
        assert "cache_hit_rate" in data
        assert "success_rates" in data
        assert "tools" in data
        assert "averages" in data
