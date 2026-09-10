from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

TRANSIENT_STATUS_CODES = frozenset({429, 500, 501, 502, 503, 504})

TRANSIENT_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.TransportError,
    httpx.RequestError,
    httpx.ConnectError,
    TimeoutError,
    asyncio.TimeoutError,
    ConnectionError,
)

T = TypeVar("T")


def _retry_after_from(exc: BaseException) -> float | None:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        if response.status_code == 429:
            header = response.headers.get("Retry-After")
            if header:
                try:
                    return max(0.0, float(header))
                except ValueError:
                    return None
    return None


@dataclass
class RetryPolicy:
    max_retries: int = settings.retry_max_retries
    initial_delay: float = settings.retry_initial_delay
    backoff_multiplier: float = settings.retry_backoff_multiplier
    max_delay: float = settings.retry_max_delay
    transient_status_codes: frozenset[int] = frozenset(TRANSIENT_STATUS_CODES)
    retryable_exceptions: tuple[type[BaseException], ...] = TRANSIENT_EXCEPTIONS
    # Fraction of the computed delay added at random, to stop concurrent
    # workers from retrying in lockstep after a shared 429.
    jitter: float = 0.0

    def is_transient(self, exc: BaseException) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in self.transient_status_codes
        return isinstance(exc, self.retryable_exceptions)


def is_transient_error(exc: BaseException) -> bool:
    return RetryPolicy().is_transient(exc)


def compute_delay(
    attempt: int,
    initial_delay: float = settings.retry_initial_delay,
    backoff_multiplier: float = settings.retry_backoff_multiplier,
    max_delay: float = settings.retry_max_delay,
) -> float:
    delay = initial_delay * (backoff_multiplier ** attempt)
    return min(delay, max_delay)


def apply_jitter(delay: float, jitter: float) -> float:
    """Spread a retry delay by up to `jitter` (fraction of the delay)."""
    if jitter <= 0 or delay <= 0:
        return delay
    return delay + random.uniform(0.0, delay * jitter)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    max_retries: int | None = None,
    initial_delay: float | None = None,
    backoff_multiplier: float | None = None,
    max_delay: float | None = None,
    retryable_exceptions: tuple[type[BaseException], ...] | None = None,
    transient_status_codes: frozenset[int] | None = None,
    jitter: float | None = None,
    context: str = "",
) -> T:
    policy = RetryPolicy(
        max_retries=max_retries if max_retries is not None else settings.retry_max_retries,
        initial_delay=initial_delay if initial_delay is not None else settings.retry_initial_delay,
        backoff_multiplier=(
            backoff_multiplier
            if backoff_multiplier is not None
            else settings.retry_backoff_multiplier
        ),
        max_delay=max_delay if max_delay is not None else settings.retry_max_delay,
        transient_status_codes=(
            transient_status_codes
            if transient_status_codes is not None
            else frozenset(TRANSIENT_STATUS_CODES)
        ),
        retryable_exceptions=(
            retryable_exceptions
            if retryable_exceptions is not None
            else TRANSIENT_EXCEPTIONS
        ),
        jitter=jitter if jitter is not None else 0.0,
    )
    return await retry_async_with_policy(operation, policy=policy, context=context)


async def retry_async_with_policy(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    context: str = "",
) -> T:
    last_error: BaseException | None = None
    total_attempts = policy.max_retries + 1

    for attempt in range(1, total_attempts + 1):
        logger.info("[Retry] Attempt %d: %s", attempt, context or "unknown")
        try:
            result = await operation()
            logger.info("[Retry] Success: %s", context or "unknown")
            return result
        except Exception as exc:
            last_error = exc
            if not policy.is_transient(exc):
                logger.warning(
                    "[Retry] Failed (non-retryable): %s: %s",
                    context or "unknown", exc,
                )
                raise
            logger.warning("[Retry] Failed: %s: %s", context or "unknown", exc)
            if attempt < total_attempts:
                retry_after = _retry_after_from(exc)
                if retry_after is not None:
                    delay = retry_after
                else:
                    delay = apply_jitter(
                        compute_delay(
                            attempt - 1,
                            policy.initial_delay,
                            policy.backoff_multiplier,
                            policy.max_delay,
                        ),
                        policy.jitter,
                    )
                logger.info(
                    "[Retry] Retrying in %.2fs (attempt %d/%d)%s",
                    delay, attempt, total_attempts,
                    " [retry-after]" if retry_after is not None else "",
                )
                await asyncio.sleep(delay)

    assert last_error is not None
    logger.error("[Retry] Exhausted: %s: %s", context or "unknown", last_error)
    raise last_error


def with_retry(
    *,
    max_retries: int | None = None,
    initial_delay: float | None = None,
    backoff_multiplier: float | None = None,
    max_delay: float | None = None,
    retryable_exceptions: tuple[type[BaseException], ...] | None = None,
    transient_status_codes: frozenset[int] | None = None,
    jitter: float | None = None,
    context: str = "",
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await retry_async(
                lambda: func(*args, **kwargs),
                max_retries=max_retries,
                initial_delay=initial_delay,
                backoff_multiplier=backoff_multiplier,
                max_delay=max_delay,
                retryable_exceptions=retryable_exceptions,
                transient_status_codes=transient_status_codes,
                jitter=jitter,
                context=context or func.__name__,
            )
        return wrapper
    return decorator