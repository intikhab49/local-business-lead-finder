import json

import httpx
import pytest

from app.llm.gemini_client import (
    GeminiClient,
    GeminiError,
    GeminiInvalidResponseError,
    GeminiTimeoutError,
)


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
        self.response = FakeResponse()
        self.post_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self.response


def make_client(**overrides):
    defaults = {
        "api_key": "test-key",
        "model": "gemini-2.0-flash",
        "base_url": "https://test.example.com/v1beta",
        "timeout": 5,
        "max_retries": 1,
    }
    defaults.update(overrides)
    return GeminiClient(**defaults)


def gemini_payload(actions, reasoning="test"):
    return json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": json.dumps({"actions": actions, "reasoning": reasoning})}]
                    }
                }
            ]
        }
    )


class TestGeminiClientGenerate:
    @pytest.mark.asyncio
    async def test_success_returns_parsed_dict(self):
        client = make_client()
        fake = FakeAsyncClient()
        fake.response = FakeResponse(gemini_payload(["scrape_website", "extract_email"]))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
            result = await client.generate("do something", system_prompt="be an agent")

        assert result == {"actions": ["scrape_website", "extract_email"], "reasoning": "test"}

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self):
        client = make_client(api_key=None)
        with pytest.raises(GeminiError):
            await client.generate("do something")

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self):
        client = make_client(max_retries=2)

        class FlakyClient(FakeAsyncClient):
            def __init__(self):
                super().__init__()
                self.count = 0

            async def post(self, *args, **kwargs):
                self.count += 1
                if self.count == 1:
                    self.response = FakeResponse("", status_code=500)
                else:
                    self.response = FakeResponse(gemini_payload(["discover_social"]))
                return self.response

        flaky = FlakyClient()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(httpx, "AsyncClient", lambda *a, **k: flaky)
            result = await client.generate("do something")

        assert result["actions"] == ["discover_social"]
        assert flaky.count >= 2

    @pytest.mark.asyncio
    async def test_timeout_exhausts_retries(self):
        client = make_client(max_retries=2)

        class TimeoutClient(FakeAsyncClient):
            async def post(self, *args, **kwargs):
                raise httpx.TimeoutException("timed out")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(httpx, "AsyncClient", lambda *a, **k: TimeoutClient())
            with pytest.raises(GeminiTimeoutError):
                await client.generate("do something")

    @pytest.mark.asyncio
    async def test_http_4xx_no_retry(self):
        client = make_client(max_retries=3)

        class BadKeyClient(FakeAsyncClient):
            def __init__(self):
                super().__init__()
                self.call_count = 0

            async def post(self, *args, **kwargs):
                self.call_count += 1
                self.response = FakeResponse("", status_code=400)
                return self.response

        bad = BadKeyClient()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(httpx, "AsyncClient", lambda *a, **k: bad)
            with pytest.raises(GeminiError):
                await client.generate("do something")

        assert bad.call_count == 1


class TestGeminiClientParsing:
    @pytest.mark.asyncio
    async def test_strips_code_fences(self):
        client = make_client()
        fake = FakeAsyncClient()
        raw = "```json\n" + json.dumps({"actions": ["scrape_website"]}) + "\n```"
        fake.response = FakeResponse(
            json.dumps({"candidates": [{"content": {"parts": [{"text": raw}]}}]})
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
            result = await client.generate("do something")

        assert result["actions"] == ["scrape_website"]

    @pytest.mark.asyncio
    async def test_invalid_json_in_response_raises(self):
        client = make_client()
        fake = FakeAsyncClient()
        fake.response = FakeResponse("this is not json")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
            with pytest.raises(GeminiInvalidResponseError):
                await client.generate("do something")

    @pytest.mark.asyncio
    async def test_no_candidates_raises(self):
        client = make_client()
        fake = FakeAsyncClient()
        fake.response = FakeResponse(json.dumps({"candidates": []}))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
            with pytest.raises(GeminiInvalidResponseError):
                await client.generate("do something")

    @pytest.mark.asyncio
    async def test_empty_text_raises(self):
        client = make_client()
        fake = FakeAsyncClient()
        fake.response = FakeResponse(json.dumps({"candidates": [{"content": {"parts": []}}]}))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
            with pytest.raises(GeminiInvalidResponseError):
                await client.generate("do something")

    @pytest.mark.asyncio
    async def test_non_object_json_raises(self):
        client = make_client()
        fake = FakeAsyncClient()
        fake.response = FakeResponse(
            json.dumps({"candidates": [{"content": {"parts": [{"text": "[1, 2, 3]"}]}}]})
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
            with pytest.raises(GeminiInvalidResponseError):
                await client.generate("do something")
