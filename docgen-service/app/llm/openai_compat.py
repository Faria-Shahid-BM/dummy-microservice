"""OpenAI-compatible chat/embeddings adapter over httpx.

Works against any OpenAI-style API: OpenRouter (default ``LLM_BASE_URL``),
vLLM, LiteLLM, plain OpenAI. Chat at ``POST {base}/chat/completions``,
embeddings at ``POST {base}/embeddings``. The Azure adapter subclasses this
and overrides only URL shape, auth header, and payload trimming — the wire
format is identical.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator, Sequence
from typing import Any

import httpx

from app.core.config import settings
from app.llm.base import (
    CONNECT_TIMEOUT,
    EMBED_BATCH,
    MAX_RETRIES,
    READ_TIMEOUT,
    RETRY_STATUSES,
    LLMError,
    StreamItem,
    backoff_seconds,
    concurrency_slot,
)


class _Retry(Exception):
    """Internal: unwind to the attempt loop and wait before the next try."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


def _delta_items(obj: dict[str, Any]) -> Iterator[StreamItem]:
    """Typed items from one chat-completions chunk.

    Prefers the flat ``delta.reasoning`` string; falls back to the structured
    ``reasoning_details`` array, ignoring summary/encrypted entries that carry
    no text (ported POC behavior).
    """
    for choice in obj.get("choices") or []:
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning")
        if reasoning:
            yield {"type": "reasoning", "text": reasoning}
        else:
            for d in delta.get("reasoning_details") or []:
                if isinstance(d, dict) and d.get("type") == "reasoning.text" and d.get("text"):
                    yield {"type": "reasoning", "text": d["text"]}
        content = delta.get("content")
        if content:
            yield {"type": "content", "text": content}


class OpenAICompatProvider:
    """LLMProvider over an OpenAI-compatible REST endpoint."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or settings.llm_base_url).rstrip("/")
        self._api_key = settings.llm_api_key if api_key is None else api_key
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=CONNECT_TIMEOUT, pool=CONNECT_TIMEOUT
        )
        # `transport` is an injection point for tests (httpx.MockTransport).
        self._client = httpx.Client(timeout=timeout, transport=transport)

    # ---- request shaping (overridden by the Azure adapter) ----

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _chat_url(self, model: str) -> str:
        return f"{self._base_url}/chat/completions"

    def _embed_url(self, model: str) -> str:
        return f"{self._base_url}/embeddings"

    def _chat_payload(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int | None,
        reasoning_effort: str | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        return payload

    def _embed_payload(self, model: str, batch: list[str]) -> dict[str, Any]:
        return {"model": model, "input": batch}

    # ---- LLMProvider ----

    def call(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Non-streaming chat completion; returns the message content string."""
        payload = self._chat_payload(model, messages, temperature, max_tokens, reasoning_effort, stream=False)
        body = self._post_json(self._chat_url(model), payload)
        try:
            choice = body["choices"][0]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"malformed chat completion response: {str(body)[:500]}") from e
        content = (choice.get("message") or {}).get("content")
        if content is None:
            raise LLMError(
                "model returned no content "
                f"(finish_reason={choice.get('finish_reason', 'unknown')}); "
                "likely a content-filter block"
            )
        return content

    def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[StreamItem]:
        """SSE chat completion; yields {"type": "reasoning"|"content", "text": str}.

        Retries transient failures only before the first item is yielded; any
        error after that raises so committed tokens are never duplicated.
        """
        payload = self._chat_payload(model, messages, temperature, max_tokens, reasoning_effort, stream=True)
        url = self._chat_url(model)
        headers = {**self._headers(), "Accept": "text/event-stream"}

        for attempt in range(1, MAX_RETRIES + 1):
            yielded = False
            try:
                with concurrency_slot(), self._client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        detail = resp.read().decode("utf-8", "replace")
                        if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                            raise _Retry(backoff_seconds(attempt, resp.headers.get("Retry-After")))
                        raise LLMError(f"LLM HTTP {resp.status_code}: {detail[:2000]}")
                    for line in resp.iter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if "error" in obj:
                            err = obj["error"]
                            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                            if not yielded and attempt < MAX_RETRIES:
                                raise _Retry(backoff_seconds(attempt))
                            raise LLMError(f"LLM stream error: {msg}")
                        for item in _delta_items(obj):
                            yielded = True
                            yield item
                return  # stream completed cleanly
            except _Retry as r:
                # Raised inside the `with`, caught out here: the connection and
                # concurrency slot are already released before we sleep.
                time.sleep(r.seconds)
                continue
            except httpx.HTTPError as e:
                if not yielded and attempt < MAX_RETRIES:
                    time.sleep(backoff_seconds(attempt))
                    continue
                raise LLMError(f"LLM stream failed after {attempt} attempt(s): {e}") from e
        raise LLMError("unreachable: retry loop must return or raise")

    def embed(self, model: str, texts: Sequence[str]) -> list[list[float]]:
        """Embedding vectors aligned to input order; requests in batches of 64."""
        items = list(texts)
        vectors: list[list[float]] = []
        for start in range(0, len(items), EMBED_BATCH):
            batch = items[start : start + EMBED_BATCH]
            body = self._post_json(self._embed_url(model), self._embed_payload(model, batch))
            try:
                data = sorted(body["data"], key=lambda d: d.get("index", 0))
                got = [d["embedding"] for d in data]
            except (KeyError, TypeError) as e:
                raise LLMError(f"malformed embeddings response: {str(body)[:500]}") from e
            if len(got) != len(batch):
                raise LLMError(f"embeddings response has {len(got)} vectors for {len(batch)} inputs")
            vectors.extend(got)
        return vectors

    # ---- transport ----

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with the shared retry policy; returns the parsed JSON body."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with concurrency_slot():
                    resp = self._client.post(url, json=payload, headers=self._headers())
                if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                    time.sleep(backoff_seconds(attempt, resp.headers.get("Retry-After")))
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:2000]}")
                try:
                    return resp.json()
                except ValueError as e:
                    raise LLMError(f"non-JSON response from LLM endpoint: {resp.text[:500]}") from e
            except httpx.HTTPError as e:
                if attempt < MAX_RETRIES:
                    time.sleep(backoff_seconds(attempt))
                    continue
                raise LLMError(f"LLM request failed after {attempt} attempt(s): {e}") from e
        raise LLMError("unreachable: retry loop must return or raise")
