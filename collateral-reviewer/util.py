"""Tolerant LLM-JSON parsing, ported from cad-workbench's app/engines/util.py."""
from __future__ import annotations

import json
from typing import Any


class EngineParseError(Exception):
    """LLM response could not be parsed/validated; carries the raw text."""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


def strip_fences(text: str) -> str:
    stripped = text.strip()
    if "```" not in stripped:
        return stripped
    start = stripped.find("```")
    body_start = stripped.find("\n", start)
    if body_start == -1:
        return stripped
    end = stripped.find("```", body_start)
    if end == -1:
        return stripped[body_start + 1:].strip()
    return stripped[body_start + 1:end].strip()


def parse_json_response(text: str, *, required_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    candidates: list[str] = []
    fenced = strip_fences(text)
    if fenced:
        candidates.append(fenced)
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first:last + 1])

    parsed: Any = None
    for candidate in candidates:
        c_first, c_last = candidate.find("{"), candidate.rfind("}")
        if c_first == -1 or c_last <= c_first:
            continue
        try:
            parsed = json.loads(candidate[c_first:c_last + 1], strict=False)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(parsed, dict):
        raise EngineParseError("no JSON object found in model response", raw=text)
    missing = [k for k in required_keys if k not in parsed]
    if missing:
        raise EngineParseError(f"model response missing required keys: {', '.join(missing)}", raw=text)
    return parsed
