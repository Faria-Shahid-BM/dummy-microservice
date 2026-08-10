"""Azure OpenAI adapter.

Azure speaks the same chat-completions/embeddings wire format as OpenAI, so
this subclasses :class:`OpenAICompatProvider` and overrides only what
differs: URLs are per-deployment
(``{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...``),
auth is an ``api-key`` header, and the model is selected by the URL rather
than the payload. The ``model`` argument doubles as the deployment name.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings
from app.llm.openai_compat import OpenAICompatProvider


class AzureOpenAIProvider(OpenAICompatProvider):
    """LLMProvider over the Azure OpenAI REST API."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        api_key: str | None = None,
        api_version: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        resolved = (endpoint or settings.azure_openai_endpoint).rstrip("/")
        if not resolved:
            raise RuntimeError(
                "LLM_PROVIDER=azure_openai requires AZURE_OPENAI_ENDPOINT to be set "
                "(e.g. https://<resource>.openai.azure.com)"
            )
        super().__init__(base_url=resolved, api_key=api_key, transport=transport)
        self._api_version = api_version or settings.azure_openai_api_version

    def _headers(self) -> dict[str, str]:
        return {"api-key": self._api_key}

    def _chat_url(self, model: str) -> str:
        return (
            f"{self._base_url}/openai/deployments/{model}"
            f"/chat/completions?api-version={self._api_version}"
        )

    def _embed_url(self, model: str) -> str:
        return (
            f"{self._base_url}/openai/deployments/{model}"
            f"/embeddings?api-version={self._api_version}"
        )

    def _chat_payload(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int | None,
        reasoning_effort: str | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload = super()._chat_payload(model, messages, temperature, max_tokens, reasoning_effort, stream)
        payload.pop("model", None)  # the deployment in the URL selects the model
        return payload

    def _embed_payload(self, model: str, batch: list[str]) -> dict[str, Any]:
        return {"input": batch}
