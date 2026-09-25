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


def call_with_usage(provider, model: str, messages: list, *,
                    temperature: float = 0.0) -> tuple[str | None, dict[str, int]]:
    """One non-streaming LLM call, returning its text and what it cost.

    ``(None, zeros)`` on any failure, so callers keep their
    degrade-gracefully behaviour instead of aborting a review. A provider
    without ``call_usage`` — a stub in a caller or test — still works and
    simply reports zeros.

    Takes the provider as an object rather than importing it: ``provider.py``
    is not copied into every image that ships this package (doc_rev-service
    ships the engines without it).
    """
    call_usage = getattr(provider, "call_usage", None)
    try:
        if callable(call_usage):
            return call_usage(model, messages, temperature=temperature)
        return provider.call(model=model, messages=messages,
                             temperature=temperature), new_usage()
    except Exception:
        return None, new_usage()


def call_with_usage_raising(provider, model: str, messages: list, *,
                            temperature: float = 0.0) -> tuple[str, dict[str, int]]:
    """Like :func:`call_with_usage`, but lets failures propagate.

    Two contracts exist in these engines: collateral and valuation degrade to a
    partial result on a bad call, while insurance lets the error surface. This
    keeps that difference explicit rather than quietly converting a network
    failure into a parse failure.
    """
    call_usage = getattr(provider, "call_usage", None)
    if callable(call_usage):
        return call_usage(model, messages, temperature=temperature)
    return provider.call(model, messages, temperature=temperature), new_usage()
