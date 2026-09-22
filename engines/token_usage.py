"""Token accounting shared by the provider and the engines that report it.

Lives under ``engines/`` rather than beside ``Provider`` because every service
image copies this package, while ``provider.py`` is copied only by the services
that make LLM calls — ``doc_rev-service`` ships the engines without it.

A usage figure is always the same three-key dict, so stages that make no LLM
call (the deterministic comparison tiers) report a real zero rather than a
missing value the frontend has to special-case.
"""
from __future__ import annotations


def new_usage() -> dict[str, int]:
    return {"prompt": 0, "completion": 0, "total": 0}


def usage_from(body: dict | None) -> dict[str, int]:
    """Token counts out of an OpenAI-shaped response body.

    ``total_tokens`` is derived when the provider omits it, which OpenRouter
    does for some models.
    """
    reported = (body or {}).get("usage") or {}
    prompt = reported.get("prompt_tokens") or 0
    completion = reported.get("completion_tokens") or 0
    return {
        "prompt": prompt,
        "completion": completion,
        "total": reported.get("total_tokens") or prompt + completion,
    }


def add_usage(into: dict[str, int], more: dict[str, int] | None) -> dict[str, int]:
    """Accumulate ``more`` into ``into``, in place."""
    for key in ("prompt", "completion", "total"):
        into[key] = into.get(key, 0) + (more or {}).get(key, 0)
    return into
