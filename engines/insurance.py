"""Insurance Reviewer engine (port of ``subsystems/insurance_reviewer/main.py``).

Two-stage LLM workflow:

1) Extraction — obtain the full text of the insurance policy PDF via
   :func:`app.engines.extraction.extract_document` (native text layer when
   present, vision-OCR page transcription otherwise; page-by-page progress is
   emitted by that engine), then convert the text into the strict structured
   schema of ``insurance_extraction.md`` with the extraction model.
2) Analysis — compare the extracted JSON against the bank policy text and the
   collateral rules (``insurance_analysis.md``) and return the compliance
   report. The result's top-level key is ``insurance_report`` — the same
   shape the legacy ``run()`` returned.

Pure logic: no FastAPI, no SQLAlchemy, no ``app.core.config``. The LLM
provider, model names, rules texts, and the optional ``emit`` callback all
arrive as arguments. Prompt wording is frozen domain IP, copied verbatim from
the legacy ``prompt_extraction.txt`` / ``prompt_analysis.txt``; the default
rules texts live in ``data/`` next to this module and can be overridden per
call.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engines.extraction import extract_document
from engines.util import parse_json_response

if TYPE_CHECKING:  # avoid importing app.core.config transitively at runtime
    from app.llm.base import LLMProvider

# emit(type, text) with type in {"reasoning", "content", "event"}; "event"
# carries a compact single-encoded JSON string (the POC double-encoded these).
EmitFn = Callable[[str, str], None]

_MODULE_DIR = Path(__file__).resolve().parent
_PROMPTS_DIR = _MODULE_DIR / "prompts"      # frozen prompt files (repo convention)
_DATA_DIR = _MODULE_DIR / "data"            # bundled rules/data (flat, like valuation)

_EXTRACTION_PROMPT_PATH = _PROMPTS_DIR / "insurance_extraction.md"
_ANALYSIS_PROMPT_PATH = _PROMPTS_DIR / "insurance_analysis.md"

_REQUIRED_MODEL_ROLES = ("extraction", "vision")


# ── helpers ──────────────────────────────────────────────────────────────────

def _read_text(path: Path) -> str:
    """Read a text file, falling back to latin-1 for non-UTF-8 content."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _emit_event(emit: EmitFn | None, payload: dict[str, Any]) -> None:
    """Emit a compact, single-encoded JSON progress event (if emitting)."""
    if emit is not None:
        emit("event", json.dumps(payload, separators=(",", ":")))


# ── stage 1: structured extraction from document text ────────────────────────

def extract_insurance_json(
    document_text: str,
    provider: "LLMProvider",
    model: str,
    *,
    prompt: str | None = None,
    emit: EmitFn | None = None,
) -> dict[str, Any]:
    """Convert raw policy-document text into the strict extraction schema.

    Sends the frozen extraction prompt followed by the document text (tagged
    ``<DOCUMENT_TEXT>``, mirroring the tag convention of the analysis stage)
    to the extraction model and parses the JSON object from the response.
    ``prompt`` overrides the shipped extraction prompt (None -> the frozen
    ``insurance_extraction.md``).

    Raises :class:`app.engines.util.EngineParseError` if the response holds
    no JSON object.
    """
    prompt = prompt if prompt is not None else _read_text(_EXTRACTION_PROMPT_PATH)
    user_content = f"{prompt}\n\n<DOCUMENT_TEXT>\n{document_text}\n</DOCUMENT_TEXT>"
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    _emit_event(emit, {"stage": "structure", "status": "start"})
    raw = provider.call(model, messages, temperature=0.0)
    extracted = parse_json_response(raw)
    _emit_event(emit, {"stage": "structure", "status": "done"})
    return extracted


# ── stage 2: compliance analysis ──────────────────────────────────────────────

def analyze_against_policy(
    insurance_json: dict[str, Any],
    policy_text: str,
    collateral_rules: str,
    provider: "LLMProvider",
    model: str,
    *,
    prompt: str | None = None,
    emit: EmitFn | None = None,
) -> dict[str, Any]:
    """Analyze extracted policy JSON against bank policy text + collateral
    rules; return the compliance report (top-level key ``insurance_report``).

    Message composition is identical to the legacy engine: the analysis
    prompt (with ``{COLLATERAL_POLICY_RULES}`` substituted via ``str.replace``
    — never ``str.format``, the prompt is full of literal braces) as the
    system message, and the tagged ``<INSURANCE_JSON>`` / ``<BANK_POLICY_TEXT>``
    payload as the user message. ``prompt`` overrides the shipped analysis
    prompt template (None -> the frozen ``insurance_analysis.md``); the
    ``{COLLATERAL_POLICY_RULES}`` substitution applies either way.

    Raises :class:`app.engines.util.EngineParseError` if the response holds
    no JSON object or lacks the ``insurance_report`` key.
    """
    template = prompt if prompt is not None else _read_text(_ANALYSIS_PROMPT_PATH)
    system_prompt = template.replace(
        "{COLLATERAL_POLICY_RULES}", collateral_rules)
    user_msg = (
        "<INSURANCE_JSON>\n"
        f"{json.dumps(insurance_json, indent=2)}\n"
        "</INSURANCE_JSON>\n\n"
        "<BANK_POLICY_TEXT>\n"
        f"{policy_text}\n"
        "</BANK_POLICY_TEXT>"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    _emit_event(emit, {"stage": "analyze", "status": "start"})
    raw = provider.call(model, messages, temperature=0.0)
    report = parse_json_response(raw, required_keys=("insurance_report",))
    _emit_event(emit, {"stage": "analyze", "status": "done"})
    return report


# ── orchestration entry point (the module router's job calls this) ───────────

def review_insurance(
    policy_pdf_path: Path,
    provider: "LLMProvider",
    models: dict[str, str],
    *,
    policy_rules_text: str | None = None,
    collateral_rules_text: str | None = None,
    prompts: dict[str, str] | None = None,
    emit: EmitFn | None = None,
) -> dict[str, Any]:
    """Full pipeline: extract the policy PDF's text (text layer or vision
    OCR), structure it, then analyze against the bank policy + collateral
    rules. Returns the final report dict (top-level key ``insurance_report``)
    — the same result shape as the legacy ``run()``.

    Args:
        policy_pdf_path: Path to the insurance policy PDF under review.
        provider: LLM provider (``call``/``stream``/``embed`` contract).
        models: Model roles — requires ``"extraction"`` (structured
            extraction and compliance analysis) and ``"vision"`` (the OCR
            transcription path inside ``extract_document``).
        policy_rules_text: Bank policy text; defaults to the bundled
            ``data/policy.txt``.
        collateral_rules_text: Collateral classification/compatibility rules;
            defaults to the bundled ``data/collateral_policy_rules.txt``.
        prompts: Optional prompt overrides — keys ``"extraction"`` and
            ``"analysis"`` (a missing key -> the shipped frozen prompt file).
        emit: Optional progress callback ``emit(type, text)``.
    """
    missing = [role for role in _REQUIRED_MODEL_ROLES if role not in models]
    if missing:
        raise ValueError(
            f"models dict missing required role(s): {', '.join(missing)}")
    policy_pdf_path = Path(policy_pdf_path)

    policy_text = (
        policy_rules_text
        if policy_rules_text is not None
        else _read_text(_DATA_DIR / "policy.txt")
    )
    collateral_rules = (
        collateral_rules_text
        if collateral_rules_text is not None
        else _read_text(_DATA_DIR / "collateral_policy_rules.txt")
    )

    _emit_event(
        emit,
        {"stage": "extract", "status": "start", "file": policy_pdf_path.name},
    )
    extraction = extract_document(
        policy_pdf_path, provider, models["vision"], emit=emit)
    _emit_event(
        emit,
        {
            "stage": "extract",
            "status": "done",
            "chars": len(extraction.text),
            "pages_total": extraction.pages_total,
            "pages_failed": len(extraction.pages_failed),
        },
    )

    prompts = prompts or {}
    insurance_json = extract_insurance_json(
        extraction.text, provider, models["extraction"],
        prompt=prompts.get("extraction"), emit=emit)
    report = analyze_against_policy(
        insurance_json,
        policy_text,
        collateral_rules,
        provider,
        models["extraction"],
        prompt=prompts.get("analysis"),
        emit=emit,
    )
    return report
