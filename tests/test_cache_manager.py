import threading
import time

from app.cache.cache_manager import DEFAULT_TTL_SECONDS, CacheManager
from app.models.enriched_business import EnrichedBusiness


def make_lead(**overrides) -> EnrichedBusiness:
    kwargs = {
        "business_name": "Test Biz",
        "address": "123 Main St",
        "phone": "555-0100",
        "website": "https://test.example.com",
        "email": "info@test.example.com",
        "confidence_score": 0.8,
        "source": "foursquare",
    }
    kwargs.update(overrides)
    return EnrichedBusiness(**kwargs)


class TestCacheManagerDefaults:
    def test_default_ttl_is_24_hours(self):
        assert DEFAULT_TTL_SECONDS == 24 * 60 * 60

    def test_settings_ttl_used_by_default(self, monkeypatch):
        class FakeSettings:
            cache_ttl_seconds = 3600

        monkeypatch.setattr("app.cache.cache_manager.settings", FakeSettings())
        cache = CacheManager()
        assert cache.ttl_seconds == 3600

    def test_explicit_ttl_overrides_settings(self, monkeypatch):
        class FakeSettings:
            cache_ttl_seconds = 3600

        monkeypatch.setattr("app.cache.cache_manager.settings", FakeSettings())
        cache = CacheManager(ttl_seconds=60)
        assert cache.ttl_seconds == 60

    def test_non_positive_ttl_falls_back(self, monkeypatch):
        class FakeSettings:
            cache_ttl_seconds = 3600

        monkeypatch.setattr("app.cache.cache_manager.settings", FakeSettings())
        cache = CacheManager(ttl_seconds=0)
        assert cache.ttl_seconds == 3600


class TestCacheManagerOperations:
    def test_set_and_get(self):
        cache = CacheManager(ttl_seconds=60)
        lead = make_lead()
        cache.set("id1", lead)

        result = cache.get("id1")
        assert result is not None
        assert result.business_name == "Test Biz"
        assert result.email == "info@test.example.com"

    def test_miss_returns_none(self):
        cache = CacheManager(ttl_seconds=60)
        assert cache.get("missing") is None

    def test_get_returns_copy_not_reference(self):
        cache = CacheManager(ttl_seconds=60)
        lead = make_lead()
        cache.set("id1", lead)

        result = cache.get("id1")
        assert result is not lead
        result.email = "mutated@example.com"
        assert cache.get("id1").email == "info@test.example.com"

    def test_delete_returns_true_and_removes(self):
        cache = CacheManager(ttl_seconds=60)
        cache.set("id1", make_lead())
        assert cache.delete("id1") is True
        assert cache.get("id1") is None
        assert cache.delete("id1") is False

    def test_clear_empties_store(self):
        cache = CacheManager(ttl_seconds=60)
        cache.set("id1", make_lead())
        cache.set("id2", make_lead(business_name="Other"))
        cache.clear()
        assert cache.size == 0
        assert cache.get("id1") is None
        assert cache.get("id2") is None

    def test_size_tracks_entries(self):
        cache = CacheManager(ttl_seconds=60)
        assert cache.size == 0
        cache.set("id1", make_lead())
        cache.set("id2", make_lead(business_name="Other"))
        assert cache.size == 2


class TestCacheManagerExpiration:
    def test_get_expired_entry_returns_none(self):
        cache = CacheManager(ttl_seconds=60)
        cache.set("id1", make_lead(), ttl_seconds=1)
        time.sleep(1.05)

        assert cache.get("id1") is None
        assert cache.size == 0

    def test_get_before_expiry_returns_value(self):
        cache = CacheManager(ttl_seconds=60)
        cache.set("id1", make_lead(), ttl_seconds=30)

        assert cache.get("id1") is not None

    def test_cleanup_expired_removes_only_expired(self):
        cache = CacheManager(ttl_seconds=60)
        cache.set("expired", make_lead(), ttl_seconds=1)
        cache.set("fresh", make_lead(business_name="Fresh"), ttl_seconds=60)
        time.sleep(1.05)

        removed = cache.cleanup_expired()

        assert removed == 1
        assert cache.size == 1
        assert cache.get("expired") is None
        assert cache.get("fresh") is not None

    def test_cleanup_expired_returns_zero_when_none(self):
        cache = CacheManager(ttl_seconds=60)
        cache.set("fresh", make_lead())
        assert cache.cleanup_expired() == 0


class TestCacheManagerLogging:
    def test_logs_hit(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        cache = CacheManager(ttl_seconds=60)
        cache.set("id1", make_lead())
        cache.get("id1")

        assert any("[Cache] Hit: key=id1" in msg for msg in caplog.messages)

    def test_logs_miss(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        cache = CacheManager(ttl_seconds=60)
        cache.get("missing")

        assert any("[Cache] Miss: key=missing" in msg for msg in caplog.messages)

    def test_logs_stored(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        cache = CacheManager(ttl_seconds=60)
        cache.set("id1", make_lead())

        assert any("[Cache] Stored: key=id1" in msg for msg in caplog.messages)

    def test_logs_expired(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        cache = CacheManager(ttl_seconds=60)
        cache.set("id1", make_lead(), ttl_seconds=1)
        time.sleep(1.05)
        cache.get("id1")

        assert any("[Cache] Expired: key=id1" in msg for msg in caplog.messages)

    def test_logs_deleted(self, caplog):
        import logging

        caplog.set_level(logging.INFO)
        cache = CacheManager(ttl_seconds=60)
        cache.set("id1", make_lead())
        cache.delete("id1")

        assert any("[Cache] Deleted: key=id1" in msg for msg in caplog.messages)


class TestCacheManagerConcurrency:
    def test_concurrent_set_and_get(self):
        cache = CacheManager(ttl_seconds=60)
        errors: list[Exception] = []
        barrier = threading.Barrier(8)

        def worker(n: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(50):
                    key = f"key_{n}_{i}"
                    cache.set(key, make_lead(business_name=f"Biz {n}-{i}"))
                    result = cache.get(key)
                    assert result is not None
                    assert result.business_name == f"Biz {n}-{i}"
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert cache.size == 8 * 50

    def test_concurrent_delete_and_get(self):
        cache = CacheManager(ttl_seconds=60)
        for i in range(50):
            cache.set(f"key_{i}", make_lead(business_name=f"Biz {i}"))

        errors: list[Exception] = []
        barrier = threading.Barrier(5)

        def worker(n: int) -> None:
            try:
                barrier.wait(timeout=5)
                start = n * 10
                for i in range(start, start + 10):
                    cache.delete(f"key_{i}")
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert cache.size == 0

    def test_concurrent_cleanup_and_access(self):
        cache = CacheManager(ttl_seconds=60)
        for i in range(30):
            cache.set(f"exp_{i}", make_lead(), ttl_seconds=1)
        time.sleep(1.05)
        for i in range(30, 60):
            cache.set(f"fresh_{i}", make_lead())

        errors: list[Exception] = []

        def cleaner() -> None:
            try:
                cache.cleanup_expired()
            except Exception as e:  # pragma: no cover
                errors.append(e)

        def reader() -> None:
            try:
                for i in range(60):
                    cache.get(f"key_{i}")
                    cache.get(f"fresh_{i}")
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=cleaner) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
