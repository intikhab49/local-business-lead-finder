"""Adaptive rate limiting for provider calls.

Replaces fixed sleeps between search batches. The limiter paces requests with a
single interval that adapts to what the provider actually tells us:

* a 429 / 5xx (or an explicit ``Retry-After``) multiplies the interval,
* a streak of clean responses shrinks it back toward ``min_interval``.

That is additive-increase/multiplicative-decrease pacing: fast while the
provider is happy, automatically slow when it starts pushing back.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class LimiterStats:
    interval: float
    min_interval: float
    max_interval: float
    requests: int
    throttled: int
    consecutive_ok: int
    total_wait: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "interval": round(self.interval, 3),
            "min_interval": round(self.min_interval, 3),
            "max_interval": round(self.max_interval, 3),
            "requests": self.requests,
            "throttled": self.throttled,
            "consecutive_ok": self.consecutive_ok,
            "total_wait": round(self.total_wait, 2),
        }


@dataclass
class AdaptiveRateLimiter:
    """Paces calls with an interval that reacts to throttling."""

    min_interval: float = field(default_factory=lambda: settings.discovery_min_interval)
    max_interval: float = field(default_factory=lambda: settings.discovery_max_interval)
    backoff_factor: float = field(
        default_factory=lambda: settings.discovery_backoff_factor
    )
    recovery_factor: float = field(
        default_factory=lambda: settings.discovery_recovery_factor
    )
    recovery_streak: int = field(
        default_factory=lambda: settings.discovery_recovery_streak
    )
    jitter: float = 0.1
    name: str = "provider"

    def __post_init__(self) -> None:
        self.min_interval = max(0.0, float(self.min_interval))
        self.max_interval = max(self.min_interval, float(self.max_interval))
        self._interval = self.min_interval
        self._next_at = 0.0
        self._lock = asyncio.Lock()
        self._requests = 0
        self._throttled = 0
        self._consecutive_ok = 0
        self._total_wait = 0.0

    # ── pacing ────────────────────────────────────────────────────

    @property
    def interval(self) -> float:
        return self._interval

    async def acquire(self) -> float:
        """Block until the next call is allowed. Returns the seconds waited."""
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_at - now)
            slot = max(now, self._next_at)
            spread = self._interval * self.jitter
            self._next_at = slot + self._interval + random.uniform(0.0, spread)
            self._requests += 1
        if wait > 0:
            self._total_wait += wait
            await asyncio.sleep(wait)
        return wait

    # ── feedback ──────────────────────────────────────────────────

    def record_success(self) -> None:
        self._consecutive_ok += 1
        if (
            self._consecutive_ok >= self.recovery_streak
            and self._interval > self.min_interval
        ):
            self._consecutive_ok = 0
            previous = self._interval
            self._interval = max(self.min_interval, self._interval * self.recovery_factor)
            logger.info(
                "[RateLimit:%s] recovering %.2fs -> %.2fs",
                self.name, previous, self._interval,
            )

    def record_throttled(self, retry_after: float | None = None) -> float:
        """Widen the interval after a 429/5xx. Returns the new interval."""
        self._throttled += 1
        self._consecutive_ok = 0
        previous = self._interval
        widened = max(self._interval * self.backoff_factor, self.min_interval or 0.5)
        if retry_after:
            widened = max(widened, float(retry_after))
        self._interval = min(self.max_interval, widened)
        # Push the next slot out too, so in-flight callers feel it immediately.
        self._next_at = max(self._next_at, time.monotonic() + self._interval)
        logger.warning(
            "[RateLimit:%s] throttled — interval %.2fs -> %.2fs%s",
            self.name, previous, self._interval,
            f" (retry-after {retry_after}s)" if retry_after else "",
        )
        return self._interval

    def record_error(self) -> None:
        """A non-throttle failure: reset the recovery streak, keep the interval."""
        self._consecutive_ok = 0

    def stats(self) -> LimiterStats:
        return LimiterStats(
            interval=self._interval,
            min_interval=self.min_interval,
            max_interval=self.max_interval,
            requests=self._requests,
            throttled=self._throttled,
            consecutive_ok=self._consecutive_ok,
            total_wait=self._total_wait,
        )
