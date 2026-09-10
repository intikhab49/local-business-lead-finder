import asyncio
import time

from app.agents.task_manager import TaskManager
from app.core.concurrency import DEFAULT_MAX_CONCURRENT_TASKS, get_max_concurrent_tasks


class TestConcurrencyConfig:
    def test_default_max_concurrent_tasks(self):
        assert DEFAULT_MAX_CONCURRENT_TASKS == 5

    def test_get_max_concurrent_tasks_returns_positive(self):
        assert get_max_concurrent_tasks() >= 1

    def test_task_manager_uses_setting_default(self, monkeypatch):
        class FakeSettings:
            max_concurrent_tasks = 7

        monkeypatch.setattr("app.core.concurrency.settings", FakeSettings())
        assert get_max_concurrent_tasks() == 7

    def test_task_manager_ignores_non_positive_config(self, monkeypatch):
        class FakeSettings:
            max_concurrent_tasks = 0

        monkeypatch.setattr("app.core.concurrency.settings", FakeSettings())
        assert get_max_concurrent_tasks() == DEFAULT_MAX_CONCURRENT_TASKS


class TestTaskManager:
    async def test_parallel_execution(self):
        active = 0
        max_active = 0
        lock = asyncio.Lock()

        async def task(item):
            nonlocal active, max_active
            async with lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            async with lock:
                active -= 1
            return item

        manager = TaskManager(concurrency=10)
        results = await manager.run(list(range(10)), task)

        assert max_active > 1
        assert results == list(range(10))

    async def test_result_ordering(self):
        async def task(item):
            await asyncio.sleep((10 - item) * 0.001)
            return item * 10

        manager = TaskManager(concurrency=10)
        items = list(range(5))
        results = await manager.run(items, task)

        assert results == [0, 10, 20, 30, 40]

    async def test_concurrency_limit(self):
        active = 0
        max_active = 0
        lock = asyncio.Lock()

        async def task(item):
            nonlocal active, max_active
            async with lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            async with lock:
                active -= 1
            return item

        manager = TaskManager(concurrency=3)
        results = await manager.run(list(range(20)), task)

        assert max_active <= 3
        assert max_active == 3
        assert len(results) == 20

    async def test_failure_isolation(self):
        calls = []

        async def task(item):
            calls.append(item)
            if item == 2:
                raise RuntimeError("boom")
            if item == 4:
                raise ValueError("kaboom")
            return item * 2

        manager = TaskManager(concurrency=5)
        results = await manager.run(list(range(5)), task)

        assert results == [0, 2, None, 6, None]
        assert calls == [0, 1, 2, 3, 4]

    async def test_empty_task_list(self):
        manager = TaskManager(concurrency=5)
        results = await manager.run([], lambda x: x)

        assert results == []

    async def test_performance_benchmark(self):
        async def task(item):
            await asyncio.sleep(0.02)
            return item

        items = list(range(8))

        sequential = TaskManager(concurrency=1)
        start = time.monotonic()
        seq_results = await sequential.run(items, task)
        seq_time = time.monotonic() - start

        parallel = TaskManager(concurrency=8)
        start = time.monotonic()
        par_results = await parallel.run(items, task)
        par_time = time.monotonic() - start

        assert seq_results == par_results == items
        assert par_time < seq_time

    async def test_logs_structured_messages(self, caplog):
        import logging

        caplog.set_level(logging.INFO)

        async def task(item):
            if item == "b":
                raise RuntimeError("fail")
            return item

        manager = TaskManager(concurrency=2)
        await manager.run(["a", "b", "c"], task)

        messages = caplog.messages
        assert any("Starting 3 tasks" in m for m in messages)
        completed = [m for m in messages if "Task completed" in m]
        failed = [m for m in messages if "Task failed" in m]
        assert len(completed) == 2
        assert len(failed) == 1
        assert any("Finished in" in m and " ms" in m for m in messages)

    async def test_timing_reported_in_logs(self, caplog):
        import logging

        caplog.set_level(logging.INFO)

        async def task(item):
            await asyncio.sleep(0.005)
            return item

        manager = TaskManager(concurrency=2)
        await manager.run(list(range(3)), task)

        finished = [m for m in caplog.messages if "Finished in" in m]
        assert len(finished) == 1
        import re

        match = re.search(r"Finished in (\d+) ms", finished[0])
        assert match is not None
        assert int(match.group(1)) >= 0

    async def test_custom_args_passed_to_task(self):
        async def task(item, factor, offset=0):
            return item * factor + offset

        manager = TaskManager(concurrency=3)
        results = await manager.run(list(range(3)), task, 10, offset=5)

        assert results == [5, 15, 25]
