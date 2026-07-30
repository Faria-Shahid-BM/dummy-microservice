"""Minimal OpenAI-compatible chat-completion client, shared by every service
that needs to make an LLM call (collateral-reviewer, document-reviewer's
PDF-heading detection).

Trimmed to just the one method callers actually need (`call`, non-streaming).
Works against any OpenAI-style API: plain OpenAI, OpenRouter, Azure OpenAI
(compatible mode), vLLM, LiteLLM — configured entirely via base URL + API
key, both read from the environment so the real key never lives in a
committed file.
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

MAX_RETRIES = 3
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 300.0
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 20.0
_RETRY_AFTER_CAP = 30.0


class LLMError(RuntimeError):
    pass


def _backoff_seconds(attempt: int, retry_after: str | None = None) -> float:
    if retry_after is not None:
        try:
            return min(float(retry_after), _RETRY_AFTER_CAP)
        except (TypeError, ValueError):
            pass
    return min(_BACKOFF_BASE * 2 ** (attempt - 1), _BACKOFF_CAP)


class OpenAICompatProvider:
    def __init__(self) -> None:
        base_url = os.environ.get("LLM_BASE_URL")
        api_key = os.environ.get("LLM_API_KEY")
        if not base_url or not api_key:
            raise RuntimeError(
                "LLM_BASE_URL and LLM_API_KEY must be set (see .env.example) "
                "before this service can make model calls"
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=CONNECT_TIMEOUT, pool=CONNECT_TIMEOUT)
        self._client = httpx.Client(timeout=timeout)

    def call(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {self._api_key}"}
        url = f"{self._base_url}/chat/completions"

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._client.post(url, json=payload, headers=headers)
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                    time.sleep(_backoff_seconds(attempt, resp.headers.get("Retry-After")))
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:2000]}")
                body = resp.json()
                break
            except httpx.HTTPError as e:
                if attempt < MAX_RETRIES:
                    time.sleep(_backoff_seconds(attempt))
                    continue
                raise LLMError(f"LLM request failed after {attempt} attempt(s): {e}") from e
        else:
            raise LLMError("unreachable: retry loop must return or raise")

        try:
            choice = body["choices"][0]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"malformed chat completion response: {str(body)[:500]}") from e
        content = (choice.get("message") or {}).get("content")
        if content is None:
            raise LLMError(
                f"model returned no content (finish_reason={choice.get('finish_reason', 'unknown')})"
            )
        return content
