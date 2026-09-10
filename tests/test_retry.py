import logging

import httpx
import pytest

from app.core.retry import (
    RetryPolicy,
    compute_delay,
    is_transient_error,
    retry_async,
    retry_async_with_policy,
    with_retry,
)


async def _no_sleep(delay: float) -> None:
    pass


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=httpx.Request("GET", "http://test"),
        response=httpx.Response(status_code),
    )


def _always_fail(exc: BaseException) -> BaseException:
    async def _op() -> None:
        raise exc

    return _op


class TestRetryAsync:
    async def test_success_no_retry(self):
        calls = []

        async def _op() -> str:
            calls.append(1)
            return "ok"

        result = await retry_async(_op, max_retries=3)
        assert result == "ok"
        assert len(calls) == 1

    async def test_transient_failure_then_success(self):
        calls = []

        async def _op() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("boom")
            return "ok"

        result = await retry_async(_op, max_retries=3, initial_delay=0)
        assert result == "ok"
        assert len(calls) == 2

    async def test_exponential_backoff(self, monkeypatch):
        recorded = []

        async def _fake_sleep(delay):
            recorded.append(delay)

        monkeypatch.setattr("app.core.retry.asyncio.sleep", _fake_sleep)
        calls = []

        async def _op() -> str:
            calls.append(1)
            raise ConnectionError("boom")

        with pytest.raises(ConnectionError):
            await retry_async(_op, max_retries=3, initial_delay=0.1, backoff_multiplier=2.0)

        assert recorded == [0.1, 0.2, 0.4]

    async def test_max_delay_cap(self, monkeypatch):
        recorded = []

        async def _fake_sleep(delay):
            recorded.append(delay)

        monkeypatch.setattr("app.core.retry.asyncio.sleep", _fake_sleep)

        async def _op() -> None:
            raise ConnectionError("boom")

        with pytest.raises(ConnectionError):
            await retry_async(
                _op,
                max_retries=5,
                initial_delay=10,
                backoff_multiplier=2.0,
                max_delay=15,
            )

        assert recorded == [10, 15, 15, 15, 15]

    async def test_non_retryable_error_raises_immediately(self):
        calls = []

        async def _op() -> None:
            calls.append(1)
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            await retry_async(_op, max_retries=3, initial_delay=0)

        assert len(calls) == 1

    async def test_exhaustion_raises_last_error(self, monkeypatch):
        monkeypatch.setattr("app.core.retry.asyncio.sleep", _no_sleep)
        exc = ConnectionError("final")

        with pytest.raises(ConnectionError) as excinfo:
            await retry_async(_always_fail(exc), max_retries=2, initial_delay=0)

        assert excinfo.value is exc


class TestHttpStatusRetries:
    async def test_http_429_retried_then_success(self):
        calls = []

        async def _op() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise _http_status_error(429)
            return "ok"

        result = await retry_async(_op, max_retries=3, initial_delay=0)
        assert result == "ok"
        assert len(calls) == 2

    async def test_http_429_honors_retry_after(self, monkeypatch):
        recorded = []

        async def _fake_sleep(delay):
            recorded.append(delay)

        monkeypatch.setattr("app.core.retry.asyncio.sleep", _fake_sleep)

        def _err_with_retry_after():
            request = httpx.Request("GET", "http://test")
            response = httpx.Response(
                429,
                headers={"Retry-After": "5"},
                request=request,
            )
            return httpx.HTTPStatusError("HTTP 429", request=request, response=response)

        calls = []

        async def _op() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise _err_with_retry_after()
            return "ok"

        result = await retry_async(_op, max_retries=3, initial_delay=1.0, max_delay=2.0)
        assert result == "ok"
        assert len(calls) == 2
        assert recorded == [5.0]

    async def test_http_503_retried(self, monkeypatch):
        monkeypatch.setattr("app.core.retry.asyncio.sleep", _no_sleep)
        calls = []

        async def _op() -> None:
            calls.append(1)
            raise _http_status_error(503)

        with pytest.raises(httpx.HTTPStatusError):
            await retry_async(_op, max_retries=2, initial_delay=0)

        assert len(calls) == 3

    async def test_http_400_not_retried(self):
        calls = []

        async def _op() -> None:
            calls.append(1)
            raise _http_status_error(400)

        with pytest.raises(httpx.HTTPStatusError):
            await retry_async(_op, max_retries=3, initial_delay=0)

        assert len(calls) == 1

    async def test_timeout_retried(self):
        calls = []

        async def _op() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.TimeoutException("timed out")
            return "ok"

        result = await retry_async(_op, max_retries=3, initial_delay=0)
        assert result == "ok"
        assert len(calls) == 2


class TestRetryLogging:
    async def test_logs_attempt_and_success(self, caplog):
        caplog.set_level(logging.INFO)

        async def _op() -> str:
            return "ok"

        await retry_async(_op, max_retries=2, context="test-op")

        assert any("[Retry] Attempt 1: test-op" in msg for msg in caplog.messages)
        assert any("[Retry] Success: test-op" in msg for msg in caplog.messages)

    async def test_logs_failed_and_exhausted(self, caplog, monkeypatch):
        caplog.set_level(logging.WARNING)
        monkeypatch.setattr("app.core.retry.asyncio.sleep", _no_sleep)

        async def _op() -> None:
            raise ConnectionError("boom")

        with pytest.raises(ConnectionError):
            await retry_async(_op, max_retries=1, initial_delay=0, context="flaky-op")

        messages = caplog.messages
        assert any("[Retry] Failed: flaky-op" in msg for msg in messages)
        assert any("[Retry] Exhausted: flaky-op" in msg for msg in messages)


class TestRetryHelpers:
    def test_compute_delay_exponential(self):
        assert compute_delay(0, 0.5, 2.0, 10.0) == 0.5
        assert compute_delay(1, 0.5, 2.0, 10.0) == 1.0
        assert compute_delay(2, 0.5, 2.0, 10.0) == 2.0
        assert compute_delay(3, 0.5, 2.0, 10.0) == 4.0

    def test_compute_delay_caps_at_max(self):
        assert compute_delay(10, 0.5, 2.0, 10.0) == 10.0

    def test_is_transient_error(self):
        assert is_transient_error(ConnectionError("x"))
        assert is_transient_error(httpx.TimeoutException("x"))
        assert is_transient_error(_http_status_error(429))
        assert is_transient_error(_http_status_error(503))
        assert not is_transient_error(_http_status_error(400))
        assert not is_transient_error(ValueError("x"))

    def test_retry_policy_defaults(self):
        policy = RetryPolicy()
        assert policy.max_retries >= 0
        assert policy.initial_delay >= 0
        assert policy.backoff_multiplier >= 1
        assert policy.max_delay >= 0

    def test_retry_policy_is_transient(self):
        policy = RetryPolicy(max_retries=1)
        assert policy.is_transient(httpx.ConnectError("x"))
        assert not policy.is_transient(ValueError("x"))

    async def test_retry_async_with_policy(self):
        policy = RetryPolicy(max_retries=1, initial_delay=0)
        calls = []

        async def _op() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("boom")
            return "ok"

        result = await retry_async_with_policy(_op, policy=policy, context="policy-op")
        assert result == "ok"
        assert len(calls) == 2


class TestWithRetryDecorator:
    async def test_decorator_retries(self, monkeypatch):
        monkeypatch.setattr("app.core.retry.asyncio.sleep", _no_sleep)
        calls = []

        @with_retry(max_retries=2, initial_delay=0, context="decorated")
        async def _flaky() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.TimeoutException("x")
            return "done"

        result = await _flaky()
        assert result == "done"
        assert len(calls) == 2

    async def test_decorator_non_retryable(self):
        calls = []

        @with_retry(max_retries=3, initial_delay=0)
        async def _bad_input() -> None:
            calls.append(1)
            raise ValueError("bad")

        with pytest.raises(ValueError):
            await _bad_input()

        assert len(calls) == 1

    async def test_decorator_preserves_function_name(self):
        @with_retry(max_retries=1)
        async def _named_op() -> str:
            return "ok"

        assert _named_op.__name__ == "_named_op"
        result = await _named_op()
        assert result == "ok"
