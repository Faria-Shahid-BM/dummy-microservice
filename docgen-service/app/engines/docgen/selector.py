"""Selection Agent engine: decides which output templates a case needs.

Ported from ``subsystems/selector/main.py`` (POC). Pure logic: case text,
descriptors, domain knowledge, provider and model all arrive as arguments. The
frozen system prompt lives in ``prompts/selector.md`` (verbatim copy of the
legacy ``prompt.md``).

Legacy invariants preserved:
- ``condense_descriptors``: captures the ``## Overview`` / ``## Selection``
  sections of each descriptor; if neither heading is found (hand-edited
  descriptor), falls back to the FULL descriptor so the selector never
  evaluates an effectively empty template;
- ``build_messages`` user-text layout is byte-identical;
- ``temperature=0.0``;
- JSON contract ``{case_summary, selected_documents:[{template_name, count,
  evidence, entities[]}], ambiguous_documents:[...]}``.

POC bug fixed here (ARCHITECTURE.md "Known POC bugs" #3): the legacy parser
lacked ``strict=False`` and choked on multi-line string values; parsing now
goes through the shared tolerant ``util.parse_json_response`` with schema
validation (``selected_documents`` required). On failure ``EngineParseError``
propagates with the raw response attached — the caller persists it.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.engines.util import parse_json_response

if TYPE_CHECKING:
    from app.llm.base import LLMProvider

# emit(type, text) with type in {"reasoning", "content", "event"}
EmitFn = Callable[[str, str], None]

_PROMPT_PATH = Path(__file__).parent / "prompts" / "selector.md"


@lru_cache(maxsize=1)
def _system_instruction() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def condense_descriptors(descriptors: dict[str, str]) -> str:
    """Condense each descriptor to its Overview/Selection sections.

    Ported verbatim from the POC (including the full-descriptor fallback).
    """
    parts = []
    for name, content in descriptors.items():
        lines = content.split("\n")
        body = []
        in_section = False
        for line in lines:
            low = line.lower()
            if low.startswith("## overview") or low.startswith("## selection"):
                in_section = True
            elif line.startswith("## ") and in_section:
                in_section = False
            if in_section:
                body.append(line)
        # Fallback: if no recognized section was captured (hand-edited
        # descriptor or different headings), include the FULL descriptor rather
        # than emitting only the header — otherwise the selector evaluates an
        # effectively empty template and silently skips it.
        if not any(l.strip() for l in body):
            body = lines
        parts.append("\n".join([f"### TEMPLATE: {name}\n"] + body))
    return "\n\n---\n\n".join(parts)


def build_messages(
    system_instruction: str,
    domain_knowledge: str,
    descriptors_text: str,
    case_text: str,
) -> list[dict[str, Any]]:
    user_text = (
        f"## Domain Knowledge\n\n{domain_knowledge}\n\n"
        f"---\n\n"
        f"## Available Output Document Templates\n\n"
        f"For each, evaluate whether it should be selected for this case.\n\n"
        f"{descriptors_text}\n\n"
        f"---\n\n"
        f"## Input Document (transcribed)\n\n{case_text}"
    )
    return [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": user_text},
    ]


def _complete(
    provider: LLMProvider,
    model: str,
    messages: list[dict[str, Any]],
    emit: EmitFn | None,
) -> str:
    """call() when emit is None; stream() otherwise, forwarding each item."""
    if emit is None:
        return provider.call(model=model, messages=messages, temperature=0.0)
    parts: list[str] = []
    for item in provider.stream(model=model, messages=messages, temperature=0.0):
        kind = item.get("type", "")
        text = item.get("text", "")
        if not text:
            continue
        if kind == "content":
            parts.append(text)
        if kind in ("reasoning", "content"):
            emit(kind, text)
    return "".join(parts)


def select_documents(
    case_text: str,
    descriptors: dict[str, str],
    provider: LLMProvider,
    model: str,
    *,
    domain_knowledge: str = "",
    emit: EmitFn | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    """Select the output templates this case needs.

    Returns the parsed selection dict. Raises ``EngineParseError`` (with the
    raw model response attached) when the response cannot be parsed or lacks
    ``selected_documents`` — the caller persists the raw text.

    ``prompt`` overrides the shipped system prompt (None -> the frozen
    ``prompts/selector.md``).
    """
    descriptors_text = condense_descriptors(descriptors)
    messages = build_messages(
        prompt if prompt is not None else _system_instruction(),
        domain_knowledge, descriptors_text, case_text,
    )
    response = _complete(provider, model, messages, emit)
    return parse_json_response(response, required_keys=("selected_documents",))
