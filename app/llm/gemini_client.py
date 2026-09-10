import json
import logging
import re
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.retry import retry_async
from app.monitoring.metrics import get_metrics, timer

logger = logging.getLogger(__name__)


class GeminiError(Exception):
    pass


class GeminiTimeoutError(GeminiError):
    pass


class GeminiInvalidResponseError(GeminiError):
    pass


class GeminiClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model
        self.base_url = base_url or settings.gemini_base_url
        self.timeout = timeout or settings.gemini_timeout
        self.max_retries = max_retries if max_retries is not None else settings.gemini_max_retries

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 512,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise GeminiError("Gemini API key is not configured")

        url = f"{self.base_url}/models/{self.model}:generateContent"
        headers = {"Content-Type": "application/json"}
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json",
            },
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        logger.info("[LLM] Prompt sent: model=%s", self.model)

        metrics = get_metrics()
        metrics.increment("gemini_calls")
        llm_start = time.monotonic()
        try:
            with timer("llm"):
                text = await self._post_with_retries(url, headers, payload)
        except Exception:
            metrics.increment("gemini_failures")
            logger.info(
                "[Timing] stage=llm duration_ms=%.1f status=failed",
                (time.monotonic() - llm_start) * 1000,
            )
            raise

        logger.info("[LLM] Response received")
        logger.info(
            "[Timing] stage=llm duration_ms=%.1f status=success",
            (time.monotonic() - llm_start) * 1000,
        )
        return self._parse_response(text)

    async def _post_with_retries(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> str:
        async def _post() -> str:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    params={"key": self.api_key},
                    json=payload,
                )
                response.raise_for_status()
                return response.text

        try:
            return await retry_async(
                _post,
                max_retries=self.max_retries,
                context=f"Gemini generate {self.model}",
            )
        except httpx.HTTPStatusError as e:
            raise GeminiError(
                f"Gemini HTTP error: {e.response.status_code} {e.response.text[:200]}"
            ) from e
        except httpx.TimeoutException as e:
            raise GeminiTimeoutError(f"Gemini request timed out after {self.timeout}s") from e
        except httpx.RequestError as e:
            raise GeminiError(f"Gemini network error: {e}") from e

    def _parse_response(self, text: str) -> dict[str, Any]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise GeminiInvalidResponseError(f"Invalid JSON from Gemini: {e}") from e

        candidates = data.get("candidates", [])
        if not candidates:
            raise GeminiInvalidResponseError("Gemini returned no candidates")

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts or not parts[0].get("text"):
            raise GeminiInvalidResponseError("Gemini returned empty content")

        raw = parts[0]["text"].strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw).strip()
        raw = re.sub(r"\s*```$", "", raw).strip()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GeminiInvalidResponseError(f"Could not parse Gemini text as JSON: {e}") from e

        if not isinstance(parsed, dict):
            raise GeminiInvalidResponseError("Gemini response is not a JSON object")

        return parsed
