"""Minimal metrics stub — no-op collector that satisfies existing imports."""
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any


class MetricsCollector:
    _instance: "MetricsCollector | None" = None

    def __new__(cls) -> "MetricsCollector":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._counters: defaultdict[str, int] = defaultdict(int)
            cls._instance._timers: defaultdict[str, list[float]] = defaultdict(list)
        return cls._instance

    def increment(self, name: str, amount: int = 1) -> None:
        self._counters[name] += amount

    def record_timing(self, name: str, elapsed: float) -> None:
        self._timers[name].append(elapsed)

    def snapshot(self) -> dict[str, Any]:
        counters = dict(self._counters)
        timers = {
            f"{k}_avg_ms": int(sum(v) / len(v) * 1000) if v else 0
            for k, v in self._timers.items()
        }
        return {"counters": counters, "timers": timers}

    def log_summary(self) -> None:
        pass


_mc: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    global _mc
    if _mc is None:
        _mc = MetricsCollector()
    return _mc


@contextmanager
def timer(name: str):
    start = time.monotonic()
    try:
        yield
    finally:
        get_metrics().record_timing(name, time.monotonic() - start)