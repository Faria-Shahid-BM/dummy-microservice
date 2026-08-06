"""Provider selection and model-role mapping.

``get_provider()`` builds the process-wide singleton from
``settings.llm_provider``; adapters are imported lazily so an optional
dependency (boto3) is only required when that provider is configured.
``model_for(role)`` resolves the env-configured model slug for a role.

The global concurrency cap lives in :mod:`app.llm.base`
(``concurrency_slot``) and is acquired inside every adapter around each
network request, so even directly instantiated providers are capped.
"""
from __future__ import annotations

import threading

from app.core.config import settings
from app.llm.base import LLMProvider

_lock = threading.Lock()
_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """The singleton provider resolved from ``settings.llm_provider``."""
    global _provider
    if _provider is None:
        with _lock:
            if _provider is None:
                _provider = _build(settings.llm_provider)
    return _provider


def reset_provider() -> None:
    """Testing hook: drop the singleton so the next get_provider() rebuilds."""
    global _provider
    with _lock:
        _provider = None


def _build(name: str) -> LLMProvider:
    if name == "openai_compat":
        from app.llm.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider()
    if name == "azure_openai":
        from app.llm.azure_openai import AzureOpenAIProvider

        return AzureOpenAIProvider()
    if name == "bedrock":
        from app.llm.bedrock import BedrockProvider

        return BedrockProvider()
    raise ValueError(
        f"unknown LLM_PROVIDER {name!r}; expected one of: openai_compat, azure_openai, bedrock"
    )


def model_for(role: str) -> str:
    """Model slug for a role.

    Roles: extraction | vision | selection | fill | analysis | chat | embedding.
    """
    models = {
        "extraction": settings.llm_model_extraction,
        "vision": settings.llm_model_vision,
        "selection": settings.llm_model_selection,
        "fill": settings.llm_model_fill,
        "analysis": settings.llm_model_analysis,
        "chat": settings.llm_model_chat,
        "embedding": settings.llm_model_embedding,
    }
    try:
        return models[role]
    except KeyError:
        raise ValueError(f"unknown model role {role!r}; expected one of {sorted(models)}") from None
