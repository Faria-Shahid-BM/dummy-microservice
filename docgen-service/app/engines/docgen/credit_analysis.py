"""Credit Application Analysis engine: pre-documentation review of a case.

Ported from ``subsystems/credit_analysis/main.py`` (POC). Pure logic: case
text, domain knowledge, provider and model all arrive as arguments. The frozen
system prompt lives in ``prompts/credit_analysis.md`` (verbatim copy of the
legacy ``prompt.md``).

Legacy invariants preserved: user message layout (Domain Knowledge / Case
Document (transcribed)); ``temperature=0.0``; fence strip only when the whole
response starts fenced (legacy gate), via the shared ``util.strip_fences``.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.engines.util import strip_fences

if TYPE_CHECKING:
    from app.llm.base import LLMProvider

# emit(type, text) with type in {"reasoning", "content", "event"}
EmitFn = Callable[[str, str], None]

_PROMPT_PATH = Path(__file__).parent / "prompts" / "credit_analysis.md"


@lru_cache(maxsize=1)
def _system_instruction() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


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


def analyze_case(
    case_text: str,
    provider: LLMProvider,
    model: str,
    *,
    domain_knowledge: str = "",
    emit: EmitFn | None = None,
    prompt: str | None = None,
) -> str:
    """Review a transcribed case document; return the analysis markdown.

    ``prompt`` overrides the shipped system prompt (None -> the frozen
    ``prompts/credit_analysis.md``).
    """
    user_message = (
        f"## Domain Knowledge\n\n{domain_knowledge}\n\n"
        f"---\n\n"
        f"## Case Document (transcribed)\n\n{case_text}"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompt if prompt is not None else _system_instruction()},
        {"role": "user", "content": user_message},
    ]
    analysis = _complete(provider, model, messages, emit)

    if analysis.strip().startswith("```"):
        analysis = strip_fences(analysis)
    return analysis
