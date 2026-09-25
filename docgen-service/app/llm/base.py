"""LLM provider contract and shared transport policy.

``LLMProvider`` mirrors the POC contract the engines already assume. The
retry policy (3 attempts, exponential backoff + jitter, Retry-After honored)
and the global concurrency cap are shared by every adapter so behavior is
identical regardless of the configured provider.

Load-bearing invariant (ported from the POC): ``stream()`` may retry only
BEFORE the first item has been yielded. Once anything is yielded, an error
must raise — never restart — or committed tokens would be duplicated in
saved artifacts.
"""
from __future__ import annotations

import random
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

from app.core.config import settings

# {"type": "reasoning" | "content", "text": str} — "content" items form the
# saved/parsed artifact; "reasoning" items are for live display only.
StreamItem = dict[str, str]

#: Token counts for one call. A plain dict so every backend can fill it without
#: importing a shared model, and so it serialises straight into the audit event.
TokenUsage = dict[str, int]


def new_usage() -> TokenUsage:
    return {"prompt": 0, "completion": 0, "total": 0}


def usage_from_body(body: dict | None) -> TokenUsage:
    """Token counts out of an OpenAI-shaped response body.

    ``total_tokens`` is derived when the provider omits it. Backends whose
    responses are shaped differently (Bedrock) supply their own extraction
    rather than reusing this.
    """
    reported = (body or {}).get("usage") or {}
    prompt = reported.get("prompt_tokens") or 0
    completion = reported.get("completion_tokens") or 0
    return {
        "prompt": prompt,
        "completion": completion,
        "total": reported.get("total_tokens") or prompt + completion,
    }

MAX_RETRIES = 3
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 300.0  # LLM long generations
EMBED_BATCH = 64

_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_CAP = 20.0
_RETRY_AFTER_CAP = 30.0


class LLMError(RuntimeError):
    """Provider or transport failure (after retries where applicable)."""


class LLMProvider(Protocol):
    """Chat + embeddings provider; engines receive one as an argument."""

    def call(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Non-streaming chat completion; returns the message content string."""
        ...

    def call_usage(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[str, TokenUsage]:
        """``call`` plus the token counts the response already carries.

        Separate from ``call`` so the existing single-value contract is
        unchanged. A backend that does not report usage returns zeros, which the
        admin usage view reads as "not measured" rather than as "free".
        """
        ...

    def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        usage_sink: TokenUsage | None = None,
    ) -> Iterator[StreamItem]:
        """Streaming chat completion; yields StreamItem dicts as they arrive.

        Retries are pre-first-item only (see module docstring).

        A streamed response reports its token counts only if asked, and only in
        a final chunk carrying no choices. When ``usage_sink`` is given it is
        filled in from that chunk; callers that pass nothing are unaffected.
        """
        ...

    def embed(self, model: str, texts: Sequence[str]) -> list[list[float]]:
        """Embedding vectors aligned to the input order."""
        ...


# One process-wide cap on in-flight provider requests across all jobs
# (the POC's unbounded fill fan-out is gone — see ARCHITECTURE.md).
_semaphore = threading.BoundedSemaphore(settings.llm_max_concurrency)


@contextmanager
def concurrency_slot() -> Iterator[None]:
    """Hold a slot of the global LLM concurrency cap for one network request.

    Every adapter wraps each HTTP/SDK request (including the full lifetime of
    a streaming response) in this context manager.
    """
    _semaphore.acquire()
    try:
        yield
    finally:
        _semaphore.release()


def backoff_seconds(attempt: int, retry_after: str | None = None) -> float:
    """Delay before retrying `attempt` (1-based); honors Retry-After if parseable."""
    if retry_after is not None:
        try:
            return min(float(retry_after), _RETRY_AFTER_CAP)
        except (TypeError, ValueError):
            pass  # HTTP-date form or garbage — fall through to exponential
    return min(_BACKOFF_BASE * 2 ** (attempt - 1), _BACKOFF_CAP) + random.uniform(0, 0.5)
