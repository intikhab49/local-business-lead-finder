import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.concurrency import get_max_concurrent_tasks

logger = logging.getLogger(__name__)

TaskFn = Callable[[Any], Awaitable[Any]]


class TaskManager:
    def __init__(self, concurrency: int | None = None) -> None:
        self._concurrency = (
            concurrency if concurrency and concurrency > 0 else get_max_concurrent_tasks()
        )
        self._semaphore = asyncio.Semaphore(self._concurrency)

    @property
    def concurrency(self) -> int:
        return self._concurrency

    async def run(
        self,
        items: list[Any],
        task_fn: TaskFn,
        *args: Any,
        **kwargs: Any,
    ) -> list[Any]:
        start = time.monotonic()
        total = len(items)

        logger.info("[TaskManager] Starting %d tasks", total)

        if not items:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.info("[TaskManager] Finished in %d ms", elapsed_ms)
            return []

        async def _wrapped(item: Any, index: int) -> tuple[int, Any]:
            async with self._semaphore:
                try:
                    result = await task_fn(item, *args, **kwargs)
                    logger.info(
                        "[TaskManager] Task completed: index=%d item=%s",
                        index,
                        self._describe(item),
                    )
                    return index, result
                except Exception as e:
                    logger.warning(
                        "[TaskManager] Task failed: index=%d item=%s error=%s",
                        index,
                        self._describe(item),
                        e,
                    )
                    return index, None

        results: list[Any] = [None] * total
        tasks = [asyncio.create_task(_wrapped(item, idx)) for idx, item in enumerate(items)]

        for task in tasks:
            index, result = await task
            results[index] = result

        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("[TaskManager] Finished in %d ms", elapsed_ms)

        return results

    @staticmethod
    def _describe(item: Any) -> str:
        name = getattr(item, "name", None) or getattr(item, "fsq_place_id", None)
        if name:
            return str(name)
        return str(item)
