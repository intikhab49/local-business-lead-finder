import asyncio
import time

import pytest

from app.core.rate_limiter import AdaptiveRateLimiter


def make_limiter(**kwargs) -> AdaptiveRateLimiter:
    defaults = dict(
        min_interval=0.01,
        max_interval=1.0,
        backoff_factor=2.0,
        recovery_factor=0.5,
        recovery_streak=2,
        jitter=0.0,
        name="test",
    )
    defaults.update(kwargs)
    return AdaptiveRateLimiter(**defaults)


class TestPacing:
    async def test_first_call_does_not_wait(self):
        limiter = make_limiter(min_interval=0.05)
        waited = await limiter.acquire()
        assert waited == 0.0

    async def test_subsequent_calls_are_spaced_by_the_interval(self):
        limiter = make_limiter(min_interval=0.05)
        started = time.monotonic()
        for _ in range(3):
            await limiter.acquire()
        elapsed = time.monotonic() - started
        # Calls 2 and 3 each wait one interval.
        assert elapsed >= 0.09

    async def test_concurrent_callers_are_serialized(self):
        limiter = make_limiter(min_interval=0.02)
        started = time.monotonic()
        await asyncio.gather(*(limiter.acquire() for _ in range(4)))
        # 4 calls, 3 gaps of 0.02s (clock granularity on Windows is coarse).
        assert time.monotonic() - started >= 0.04


class TestAdaptation:
    async def test_throttling_widens_the_interval(self):
        limiter = make_limiter(min_interval=0.1)
        assert limiter.interval == pytest.approx(0.1)
        limiter.record_throttled()
        assert limiter.interval == pytest.approx(0.2)
        limiter.record_throttled()
        assert limiter.interval == pytest.approx(0.4)

    async def test_retry_after_wins_when_larger(self):
        limiter = make_limiter(min_interval=0.1, max_interval=60)
        limiter.record_throttled(retry_after=30)
        assert limiter.interval == pytest.approx(30)

    async def test_interval_is_capped(self):
        limiter = make_limiter(min_interval=0.1, max_interval=0.3)
        for _ in range(10):
            limiter.record_throttled()
        assert limiter.interval == pytest.approx(0.3)

    async def test_recovery_shrinks_after_a_clean_streak(self):
        limiter = make_limiter(min_interval=0.1)
        limiter.record_throttled()  # 0.2
        limiter.record_success()
        assert limiter.interval == pytest.approx(0.2), "one success is not a streak"
        limiter.record_success()
        assert limiter.interval == pytest.approx(0.1)

    async def test_recovery_never_goes_below_min_interval(self):
        limiter = make_limiter(min_interval=0.1)
        for _ in range(20):
            limiter.record_success()
        assert limiter.interval == pytest.approx(0.1)

    async def test_error_resets_the_recovery_streak(self):
        limiter = make_limiter(min_interval=0.1)
        limiter.record_throttled()
        limiter.record_success()
        limiter.record_error()
        limiter.record_success()
        assert limiter.interval == pytest.approx(0.2), "streak restarted after error"

    async def test_throttle_pushes_the_next_slot_out(self):
        limiter = make_limiter(min_interval=0.05)
        await limiter.acquire()
        limiter.record_throttled(retry_after=0.2)
        started = time.monotonic()
        await limiter.acquire()
        assert time.monotonic() - started >= 0.15


class TestStats:
    async def test_stats_report_counts(self):
        limiter = make_limiter(min_interval=0.0)
        await limiter.acquire()
        await limiter.acquire()
        limiter.record_success()
        limiter.record_throttled()
        stats = limiter.stats().to_dict()
        assert stats["requests"] == 2
        assert stats["throttled"] == 1
        assert "interval" in stats
