"""AWS Bedrock adapter via the Converse / ConverseStream APIs.

boto3 is an optional dependency: a clear RuntimeError is raised at
construction if it is missing. OpenAI-style messages are mapped to the
Converse shape (system prompts split out; consecutive same-role turns merged
to satisfy Bedrock's alternation rule; text parts and ``data:image/...``
URIs supported). ``reasoning_effort`` is accepted for protocol parity but
ignored — Converse has no portable equivalent. Embeddings are unsupported.
"""
from __future__ import annotations

import base64
import time
from collections.abc import Iterator, Sequence
from typing import Any

from app.core.config import settings
from app.llm.base import (
    CONNECT_TIMEOUT,
    MAX_RETRIES,
    READ_TIMEOUT,
    LLMError,
    StreamItem,
    TokenUsage,
    backoff_seconds,
    concurrency_slot,
)

_RETRYABLE_CODES = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelNotReadyException",
    }
)
_STREAM_ERROR_KEYS = (
    "internalServerException",
    "modelStreamErrorException",
    "validationException",
    "throttlingException",
    "serviceUnavailableException",
)


def _image_block(url: str) -> dict[str, Any]:
    """Converse image block from an OpenAI-style data URI."""
    if not url.startswith("data:image/"):
        raise LLMError("bedrock Converse requires inline image bytes (data:image/... URI), got a remote URL")
    try:
        header, b64 = url.split(",", 1)
        fmt = header.removeprefix("data:image/").split(";", 1)[0]
        raw = base64.b64decode(b64)
    except ValueError as e:  # binascii.Error subclasses ValueError
        raise LLMError(f"invalid image data URI: {e}") from e
    if fmt == "jpg":
        fmt = "jpeg"
    return {"image": {"format": fmt, "source": {"bytes": raw}}}


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    """OpenAI message content (str or parts list) -> Converse content blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}]
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            blocks.append({"text": part.get("text", "")})
        elif ptype == "image_url":
            blocks.append(_image_block((part.get("image_url") or {}).get("url", "")))
        # unknown part types are dropped rather than corrupting the prompt
    return blocks


def _convert_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """OpenAI-style messages -> Converse ``(system, messages)`` pair."""
    system: list[dict[str, Any]] = []
    converted: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "user")
        blocks = _content_blocks(msg.get("content"))
        if not blocks:
            continue
        if role == "system":
            system.extend(b for b in blocks if "text" in b)
            continue
        if role not in ("user", "assistant"):
            role = "user"
        # Converse requires strict user/assistant alternation.
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})
    return system, converted


def _usage_from_converse(reported: dict | None) -> TokenUsage:
    """Token counts out of a Converse `usage` block (inputTokens/outputTokens)."""
    reported = reported or {}
    prompt = reported.get("inputTokens") or 0
    completion = reported.get("outputTokens") or 0
    return {
        "prompt": prompt,
        "completion": completion,
        "total": reported.get("totalTokens") or prompt + completion,
    }


class BedrockProvider:
    """LLMProvider over AWS Bedrock Converse / ConverseStream."""

    def __init__(self, *, region: str | None = None, client: Any | None = None) -> None:
        try:
            import boto3
            from botocore.config import Config
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError as e:
            raise RuntimeError(
                "LLM_PROVIDER=bedrock requires the optional boto3 dependency; "
                "install it with: pip install boto3"
            ) from e
        self._ClientError = ClientError
        self._BotoCoreError = BotoCoreError
        # retries disabled in botocore so our shared policy is the only one
        self._client = client or boto3.client(
            "bedrock-runtime",
            region_name=region or settings.aws_region,
            config=Config(
                connect_timeout=CONNECT_TIMEOUT,
                read_timeout=READ_TIMEOUT,
                retries={"max_attempts": 0},
            ),
        )

    def _converse_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        system, converted = _convert_messages(messages)
        inference: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            inference["maxTokens"] = max_tokens
        kwargs: dict[str, Any] = {
            "modelId": model,
            "messages": converted,
            "inferenceConfig": inference,
        }
        if system:
            kwargs["system"] = system
        return kwargs

    def _error_code(self, e: Exception) -> str:
        if isinstance(e, self._ClientError):
            return (e.response.get("Error") or {}).get("Code", "")
        return ""

    # ---- LLMProvider ----

    def call(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Non-streaming Converse call; returns the joined text blocks."""
        kwargs = self._converse_kwargs(model, messages, temperature, max_tokens)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with concurrency_slot():
                    response = self._client.converse(**kwargs)
                break
            except (self._ClientError, self._BotoCoreError) as e:
                code = self._error_code(e)
                retryable = isinstance(e, self._BotoCoreError) or code in _RETRYABLE_CODES
                if retryable and attempt < MAX_RETRIES:
                    time.sleep(backoff_seconds(attempt))
                    continue
                raise LLMError(f"bedrock converse failed ({code or type(e).__name__}): {e}") from e
        blocks = ((response.get("output") or {}).get("message") or {}).get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        if not text:
            raise LLMError(
                f"model returned no text content (stopReason={response.get('stopReason', 'unknown')})"
            )
        return text

    def call_usage(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[str, TokenUsage]:
        """``call`` plus token counts, read from Converse's own `usage` block.

        Bedrock names these differently from the OpenAI shape
        (inputTokens/outputTokens), so this cannot reuse base.usage_from_body.
        """
        kwargs = self._converse_kwargs(model, messages, temperature, max_tokens)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with concurrency_slot():
                    response = self._client.converse(**kwargs)
                break
            except (self._ClientError, self._BotoCoreError) as e:
                code = self._error_code(e)
                retryable = isinstance(e, self._BotoCoreError) or code in _RETRYABLE_CODES
                if retryable and attempt < MAX_RETRIES:
                    time.sleep(backoff_seconds(attempt))
                    continue
                raise LLMError(f"bedrock converse failed ({code or type(e).__name__}): {e}") from e
        blocks = ((response.get("output") or {}).get("message") or {}).get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        if not text:
            raise LLMError(
                f"model returned no text content (stopReason={response.get('stopReason', 'unknown')})"
            )
        return text, _usage_from_converse(response.get("usage"))

    def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        usage_sink: TokenUsage | None = None,
    ) -> Iterator[StreamItem]:
        """ConverseStream; yields {"type": "reasoning"|"content", "text": str}.

        ConverseStream reports its token counts in a trailing ``metadata``
        event; when ``usage_sink`` is given it is filled in from that.

        Retries transient failures only before the first item is yielded; any
        error after that raises so committed tokens are never duplicated.
        """
        kwargs = self._converse_kwargs(model, messages, temperature, max_tokens)
        for attempt in range(1, MAX_RETRIES + 1):
            yielded = False
            try:
                with concurrency_slot():
                    response = self._client.converse_stream(**kwargs)
                    for event in response.get("stream") or []:
                        for key in _STREAM_ERROR_KEYS:
                            if key in event:
                                msg = (event[key] or {}).get("message", "")
                                raise LLMError(f"bedrock stream error ({key}): {msg}")
                        if usage_sink is not None and "metadata" in event:
                            usage_sink.update(
                                _usage_from_converse((event["metadata"] or {}).get("usage")))
                        delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
                        reasoning = (delta.get("reasoningContent") or {}).get("text")
                        if reasoning:
                            yielded = True
                            yield {"type": "reasoning", "text": reasoning}
                        text = delta.get("text")
                        if text:
                            yielded = True
                            yield {"type": "content", "text": text}
                return  # stream completed cleanly
            except (self._ClientError, self._BotoCoreError) as e:
                code = self._error_code(e)
                retryable = isinstance(e, self._BotoCoreError) or code in _RETRYABLE_CODES
                if retryable and not yielded and attempt < MAX_RETRIES:
                    time.sleep(backoff_seconds(attempt))
                    continue
                raise LLMError(f"bedrock stream failed ({code or type(e).__name__}): {e}") from e
            except LLMError:
                if not yielded and attempt < MAX_RETRIES:
                    time.sleep(backoff_seconds(attempt))
                    continue
                raise
        raise LLMError("unreachable: retry loop must return or raise")

    def embed(self, model: str, texts: Sequence[str]) -> list[list[float]]:
        """Unsupported on Bedrock in this deployment."""
        raise NotImplementedError(
            "the bedrock provider does not support embeddings; configure an "
            "openai_compat or azure_openai provider for the embedding role"
        )
