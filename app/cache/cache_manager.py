import copy
import logging
import threading
import time
from typing import Any

from app.core.config import settings
from app.monitoring.metrics import get_metrics

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 24 * 60 * 60


class CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


class CacheManager:
    def __init__(self, ttl_seconds: int | None = None) -> None:
        self._ttl_seconds = (
            ttl_seconds
            if ttl_seconds is not None and ttl_seconds > 0
            else settings.cache_ttl_seconds
        )
        self._store: dict[str, CacheEntry] = {}
        self._lock = threading.RLock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                get_metrics().increment("cache_misses")
                logger.info("[Cache] Miss: key=%s", key)
                return None

            now = time.monotonic()
            if entry.is_expired(now):
                del self._store[key]
                get_metrics().increment("cache_misses")
                logger.info("[Cache] Expired: key=%s", key)
                return None

            get_metrics().increment("cache_hits")
            logger.info("[Cache] Hit: key=%s", key)
            return copy.deepcopy(entry.value)

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = (
            ttl_seconds
            if ttl_seconds is not None and ttl_seconds > 0
            else self._ttl_seconds
        )
        expires_at = time.monotonic() + ttl

        with self._lock:
            self._store[key] = CacheEntry(
                value=copy.deepcopy(value),
                expires_at=expires_at,
            )

        logger.info("[Cache] Stored: key=%s ttl=%ds", key, ttl)

    def delete(self, key: str) -> bool:
        with self._lock:
            entry = self._store.pop(key, None)
        if entry is not None:
            logger.info("[Cache] Deleted: key=%s", key)
            return True
        logger.info("[Cache] Miss: key=%s", key)
        return False

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
        logger.info("[Cache] Cleared: store reset")

    def cleanup_expired(self) -> int:
        now = time.monotonic()
        expired_keys: list[str] = []

        with self._lock:
            for key, entry in list(self._store.items()):
                if entry.is_expired(now):
                    expired_keys.append(key)
                    del self._store[key]

        for key in expired_keys:
            logger.info("[Cache] Expired: key=%s", key)

        return len(expired_keys)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def __len__(self) -> int:
        return self.size
