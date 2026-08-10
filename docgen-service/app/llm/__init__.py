"""LLM provider layer: protocol, shared retry/concurrency policy, adapters.

Adapters themselves (openai_compat / azure_openai / bedrock) are imported
lazily by the registry so optional dependencies stay optional.
"""
from app.llm.base import LLMError, LLMProvider, StreamItem, concurrency_slot
from app.llm.registry import get_provider, model_for, reset_provider

__all__ = [
    "LLMError",
    "LLMProvider",
    "StreamItem",
    "concurrency_slot",
    "get_provider",
    "model_for",
    "reset_provider",
]
