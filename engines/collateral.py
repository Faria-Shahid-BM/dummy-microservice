"""Collateral Reviewer engine (ported from ``subsystems/collateral_reviewer/main.py``).

Cross-checks a property's LEGAL OPINION (issued by a lawyer) against its
PROPERTY / TITLE document. The pipeline:

  1) Extract a fixed CAD field set from EACH document with an LLM
     (the canonical schema ported from the original collateral-reviewer:
     property_information / ownership_information / legal_information, each
     field -> {value, source_page}).
  2) Compare the two documents field-by-field with normalization
     (match / mismatch / missing).
  3) Generate plain-English, one-sentence observations for the discrepancies
     in a banking collateral-review tone (single LLM call, validated 1:1
     against the discrepancy count, deterministic fallback otherwise).

DOMAIN IP (frozen, ported verbatim): the field set, the extraction prompt
wording, and the observation prompt tone — stored as ``prompts/*.md`` next to
this module. INFRASTRUCTURE: text/OCR extraction goes through the shared
``app.engines.extraction.extract_document``; every LLM call goes through the
injected ``LLMProvider``; JSON object parsing goes through the shared tolerant
parser in ``app.engines.util``.

Pure logic: no FastAPI, no SQLAlchemy, no ``app.core.config``. The provider,
model names, and the optional ``emit`` progress callback all arrive as
arguments.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

# from app.engines.extraction import extract_document
# from app.engines.util import EngineParseError, parse_json_response
from engines.extraction import extract_document
from engines.util import EngineParseError, parse_json_response

if TYPE_CHECKING:  # pragma: no cover — typing only; engines stay import-pure
    from app.llm.base import LLMProvider

# emit(type, text) with type in {"reasoning", "content", "event"}; "event"
# carries a compact single-encoded JSON string.
EmitFn = Callable[[str, str], None]

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


# ──────────────────────────────────────────────────────────────────────────────
# 1) Canonical CAD field set
#    Ported verbatim from the original extraction/schemas.py. Each leaf field is
#    {value, source_page}. The two top-level docs ("legal_opinion",
#    "property_document") share an identical field layout.
# ──────────────────────────────────────────────────────────────────────────────

EXTRACTION_SCHEMA: dict[str, Any] = {

    "legal_opinion": {

        "property_information": {

            "property_address": {
                "value": None,
                "source_page": None
            },

            "plot_or_survey_number": {
                "value": None,
                "source_page": None
            },

            "land_registration_number": {
                "value": None,
                "source_page": None
            },

            "property_description": {
                "value": None,
                "source_page": None
            }
        },

        "ownership_information": {

            "property_owner_name": {
                "value": None,
                "source_page": None
            },

            "mortgagor_name": {
                "value": None,
                "source_page": None
            }
        },

        "legal_information": {

            "legal_opinion_date": {
                "value": None,
                "source_page": None
            },

            "registration_authority": {
                "value": None,
                "source_page": None
            },

            "mortgage_enforceability_reference": {
                "value": None,
                "source_page": None
            }
        }
    },


    "property_document": {

        "property_information": {

            "property_address": {
                "value": None,
                "source_page": None
            },

            "plot_or_survey_number": {
                "value": None,
                "source_page": None
            },

            "land_registration_number": {
                "value": None,
                "source_page": None
            },

            "property_description": {
                "value": None,
                "source_page": None
            }
        },

        "ownership_information": {

            "property_owner_name": {
                "value": None,
                "source_page": None
            },

            "mortgagor_name": {
                "value": None,
                "source_page": None
            }
        },

        "legal_information": {

            "legal_opinion_date": {
                "value": None,
                "source_page": None
            },

            "registration_authority": {
                "value": None,
                "source_page": None
            },

            "mortgage_enforceability_reference": {
                "value": None,
                "source_page": None
            }
        }
    }

}


# Human-readable labels for every comparable field (used in comparison rows and
# the deterministic observation fallback).
FIELD_LABELS: dict[str, str] = {
    "property_address": "Property address",
    "plot_or_survey_number": "Plot or survey number",
    "land_registration_number": "Land registration number",
    "property_description": "Property description",
    "property_owner_name": "Property owner name",
    "mortgagor_name": "Mortgagor name",
    "legal_opinion_date": "Legal opinion date",
    "registration_authority": "Registration authority",
    "mortgage_enforceability_reference": "Mortgage enforceability reference",
}


# ──────────────────────────────────────────────────────────────────────────────
# 2) Prompt loading + progress helper
# ──────────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def _load_prompt(name: str) -> str:
    """Load a frozen prompt template verbatim (no stripping — whitespace is
    part of the ported wording)."""
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _emit_event(emit: EmitFn | None, payload: dict[str, Any]) -> None:
    """Send a compact, SINGLE-encoded JSON event (the POC double-encoded)."""
    if emit is not None:
        emit("event", json.dumps(payload, separators=(",", ":")))


# ──────────────────────────────────────────────────────────────────────────────
# 3) Per-document field extraction
# ──────────────────────────────────────────────────────────────────────────────

def build_extraction_prompt(document_text: str, document_name: str, *,
                            prompt: str | None = None) -> str:
    """Build the structured field-extraction prompt for one document.

    Wording is frozen domain IP (``prompts/collateral_extraction.md``, ported
    verbatim from the original extraction/prompts.py). ``prompt`` overrides
    that shipped template (None -> load the frozen file). The schema template
    shows only the requested document's section, so the model returns exactly
    that shape. ``document_text`` is substituted last so document content can
    never be re-expanded as a placeholder.
    """
    schema_template = json.dumps(
        EXTRACTION_SCHEMA[document_name],
        indent=2
    )
    template = prompt if prompt is not None else _load_prompt("collateral_extraction.md")
    return (
        template
        .replace("{document_name}", document_name)
        .replace("{schema_template}", schema_template)
        .replace("{document_text}", document_text)
    )


def _merge_document_fields(schema_section: dict[str, Any],
                           extracted: Any) -> None:
    """Merge an extracted {section -> field -> {value, source_page}} dict into a
    document's schema section IN PLACE. Only known sections/fields are touched;
    unknown keys and null values are skipped (so a partial/garbled response can
    never corrupt the schema).

    Ported from the source FieldExtractor._merge_document_fields.
    """
    if not isinstance(extracted, dict):
        return

    for section_name, fields in extracted.items():
        if section_name not in schema_section or not isinstance(fields, dict):
            continue
        for field_name, field_data in fields.items():
            if field_name not in schema_section[section_name]:
                continue
            if not isinstance(field_data, dict):
                continue
            value = field_data.get("value")
            if value is None:
                continue
            schema_section[section_name][field_name]["value"] = value
            schema_section[section_name][field_name]["source_page"] = \
                field_data.get("source_page")


def extract_fields(text: str, doc_name: str, provider: "LLMProvider",
                   model: str, *, prompt: str | None = None) -> dict[str, Any]:
    """Extract the CAD field set for ONE document.

    Returns a populated copy of EXTRACTION_SCHEMA[doc_name]. Never crashes on a
    failed call or bad JSON — on any error it returns the (possibly all-null)
    schema so the pipeline degrades gracefully instead of aborting.
    ``prompt`` overrides the shipped extraction prompt template (None -> the
    frozen ``prompts/collateral_extraction.md``).
    """
    schema = deepcopy(EXTRACTION_SCHEMA[doc_name])

    prompt = build_extraction_prompt(text or "", doc_name, prompt=prompt)
    try:
        response = provider.call(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
    except Exception:
        return schema

    try:
        parsed = parse_json_response(response)
    except EngineParseError:
        return schema
    _merge_document_fields(schema, parsed)
    return schema


# ──────────────────────────────────────────────────────────────────────────────
# 4) Field-by-field comparison (with normalization)
# ──────────────────────────────────────────────────────────────────────────────

def _norm(value: Any) -> str | None:
    """Normalize a field value for equality: None/empty -> None; otherwise
    collapse internal whitespace, strip, and casefold. Used only for the
    match/mismatch/missing decision — raw values are preserved in the row."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    if not s:
        return None
    return s.casefold()


def _compare_section(legal_fields: dict[str, Any], property_fields: dict[str, Any],
                     section: str, field_names: list[str]) -> list[dict[str, Any]]:
    """Compare a list of fields within one schema section.

    Each result row is {field, label, legal_value (raw), property_value (raw),
    status}. Status: "missing" if either normalized side is empty, "match" if
    the normalized values are equal, else "mismatch".
    """
    results: list[dict[str, Any]] = []
    for field in field_names:
        legal_value = legal_fields[section][field]["value"]
        property_value = property_fields[section][field]["value"]

        n_legal = _norm(legal_value)
        n_property = _norm(property_value)

        if n_legal is None or n_property is None:
            status = "missing"
        elif n_legal == n_property:
            status = "match"
        else:
            status = "mismatch"

        results.append({
            "field": field,
            "label": FIELD_LABELS.get(field, field),
            "legal_value": legal_value,
            "property_value": property_value,
            "status": status,
        })
    return results


def compare_property_info(legal_fields: dict[str, Any],
                          property_fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare the property_information section.

    NOTE: land_registration_number IS included here (the original omitted it).
    """
    return _compare_section(
        legal_fields, property_fields, "property_information",
        [
            "property_address",
            "plot_or_survey_number",
            "land_registration_number",
            "property_description",
        ],
    )


def compare_ownership(legal_fields: dict[str, Any],
                      property_fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare the ownership_information section."""
    return _compare_section(
        legal_fields, property_fields, "ownership_information",
        [
            "property_owner_name",
            "mortgagor_name",
        ],
    )


def compare_legal_info(legal_fields: dict[str, Any],
                       property_fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare the legal_information section."""
    return _compare_section(
        legal_fields, property_fields, "legal_information",
        [
            "legal_opinion_date",
            "registration_authority",
            "mortgage_enforceability_reference",
        ],
    )


def run_all_comparisons(legal_fields: dict[str, Any],
                        property_fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Run every section comparison and return the flat list of comparison rows
    (property, then ownership, then legal)."""
    return (
        compare_property_info(legal_fields, property_fields)
        + compare_ownership(legal_fields, property_fields)
        + compare_legal_info(legal_fields, property_fields)
    )


# ──────────────────────────────────────────────────────────────────────────────
# 5) Plain-English observations for the discrepancies
# ──────────────────────────────────────────────────────────────────────────────

def _fallback_observation(row: dict[str, Any]) -> str:
    """Deterministic one-sentence finding for a single non-match row, used when
    the LLM observation call fails or returns unparseable output. Tone mirrors
    the ported banking collateral-review prompt."""
    label = row.get("label") or row.get("field")
    legal_value = row.get("legal_value")
    property_value = row.get("property_value")
    status = row.get("status")

    if status == "missing":
        legal_missing = _norm(legal_value) is None
        property_missing = _norm(property_value) is None
        if legal_missing and property_missing:
            return (f"{label} is missing from both the legal opinion and the "
                    f"property document.")
        if legal_missing:
            return (f"{label} is recorded as \"{property_value}\" in the property "
                    f"document but is missing from the legal opinion.")
        return (f"{label} is recorded as \"{legal_value}\" in the legal opinion "
                f"but is missing from the property document.")

    # mismatch
    return (f"{label} does not match: the legal opinion states \"{legal_value}\" "
            f"while the property document states \"{property_value}\".")


def _build_observation_prompt(non_match_rows: list[dict[str, Any]], *,
                              prompt: str | None = None) -> str:
    """Build the single-call observation prompt. The banking collateral-review
    tone is frozen domain IP (``prompts/collateral_observations.md``, ported
    verbatim from the original observations/prompts.py) — one clear sentence
    per discrepancy, returned as a JSON array. ``prompt`` overrides that
    shipped template (None -> load the frozen file)."""
    discrepancies = []
    for i, row in enumerate(non_match_rows):
        discrepancies.append({
            "index": i,
            "field": row.get("label") or row.get("field"),
            "legal_opinion_value": row.get("legal_value"),
            "property_document_value": row.get("property_value"),
            "status": row.get("status"),
        })

    payload = json.dumps(discrepancies, indent=2, default=str)
    template = prompt if prompt is not None else _load_prompt("collateral_observations.md")
    return template.replace("{payload}", payload)


def _parse_observation_array(response: str | None) -> list[str] | None:
    """Parse the observation response into a list of non-empty strings. Slices
    the first '[' to the last ']' and json-loads it (``strict=False`` per the
    shared tolerant-parser policy; ``parse_json_response`` itself is
    object-only, and this response is a JSON ARRAY). Returns None on any
    failure so the caller can fall back deterministically."""
    if not response:
        return None
    start = response.find("[")
    end = response.rfind("]") + 1
    if start == -1 or end <= start:
        return None
    try:
        arr = json.loads(response[start:end], strict=False)
    except json.JSONDecodeError:
        return None

    if not isinstance(arr, list):
        return None

    out = [str(x).strip() for x in arr if str(x).strip()]
    if not out:
        return None
    return out


def generate_observations(comparison: list[dict[str, Any]],
                          provider: "LLMProvider", model: str, *,
                          prompt: str | None = None,
                          emit: EmitFn | None = None) -> list[str]:
    """Produce plain-English, one-sentence findings for every non-match row.

    Takes only the non-match rows, makes ONE LLM call for all discrepancies, and
    robustly parses the JSON array. If parsing fails (or the count doesn't line
    up 1:1 with the discrepancies), falls back to a deterministic sentence per
    row. Returns [] when there are no discrepancies. ``prompt`` overrides the
    shipped observation prompt template (None -> the frozen
    ``prompts/collateral_observations.md``).

    When ``emit`` is provided AND the provider exposes a ``stream`` method, the
    call is streamed and each content delta is forwarded as an ``("content",
    delta)`` event so the frontend can show the model's output live. The full
    text is still accumulated and parsed exactly as the non-streaming path, so
    the returned observations are identical either way. Any streaming failure
    falls back to the plain ``call``.
    """
    non_match_rows = [r for r in comparison if r.get("status") != "match"]
    if not non_match_rows:
        return []

    prompt = _build_observation_prompt(non_match_rows, prompt=prompt)
    messages = [{"role": "user", "content": prompt}]
    response: str | None
    stream_fn = getattr(provider, "stream", None)

    if emit is not None and callable(stream_fn):
        try:
            chunks: list[str] = []
            for delta in stream_fn(model=model, messages=messages, temperature=0.0):
                chunks.append(delta)
                emit("content", delta)
            response = "".join(chunks)
        except Exception:
            response = None

    if not (emit is not None and callable(stream_fn)) or response is None:
        # Non-streaming path (no emit, no stream support, or streaming failed).
        try:
            response = provider.call(
                model=model,
                messages=messages,
                temperature=0.0,
            )
        except Exception:
            response = None

    parsed = _parse_observation_array(response)

    # Use the parsed array only if it lines up 1:1 with the discrepancies;
    # otherwise fall back deterministically so every discrepancy is described.
    if parsed and len(parsed) == len(non_match_rows):
        return parsed

    return [_fallback_observation(row) for row in non_match_rows]


# ──────────────────────────────────────────────────────────────────────────────
# 6) Orchestration entry point (the module router calls this)
# ──────────────────────────────────────────────────────────────────────────────

def review_collateral(
    legal_opinion_path: Path,
    property_doc_path: Path,
    provider: "LLMProvider",
    models: Mapping[str, str],
    *,
    prompts: dict[str, str] | None = None,
    emit: EmitFn | None = None,
) -> dict[str, Any]:
    """Full collateral cross-check pipeline.

    Extracts text from both files via the shared extractor (scanned-PDF
    vision-OCR fallback included), extracts the CAD field set from the legal
    opinion and the property document IN PARALLEL, runs the field-by-field
    comparison, generates observations for the discrepancies, and returns the
    legacy result.json contract:

        {
          "extracted":   {"legal_opinion": <schema>, "property_document": <schema>},
          "comparison":  [ {field, label, legal_value, property_value, status}, ... ],
          "observations":[ "<one-sentence discrepancy>", ... ],
          "summary":     {matches, mismatches, missing, fields}
        }

    ``models`` must provide "extraction" (field extraction + observations) and
    "vision" (scanned-page OCR transcription). ``prompts`` may override the
    shipped prompt templates via the optional keys "extraction" and
    "observations" (a missing key -> the frozen prompt file).
    """
    extraction_model = models["extraction"]
    vision_model = models["vision"]
    prompts = prompts or {}
    extraction_prompt = prompts.get("extraction")
    observations_prompt = prompts.get("observations")

    _emit_event(emit, {"stage": "extract_text", "document": "legal_opinion"})
    legal_text = extract_document(
        Path(legal_opinion_path), provider, vision_model, emit=emit).text
    _emit_event(emit, {"stage": "extract_text", "document": "property_document"})
    property_text = extract_document(
        Path(property_doc_path), provider, vision_model, emit=emit).text

    _emit_event(emit, {"stage": "extract_fields"})
    with ThreadPoolExecutor(max_workers=2) as ex:
        legal_future = ex.submit(
            extract_fields, legal_text, "legal_opinion", provider,
            extraction_model, prompt=extraction_prompt)
        property_future = ex.submit(
            extract_fields, property_text, "property_document", provider,
            extraction_model, prompt=extraction_prompt)
        legal_fields = legal_future.result()
        property_fields = property_future.result()

    comparison = run_all_comparisons(legal_fields, property_fields)
    _emit_event(emit, {"stage": "compare", "fields": len(comparison)})

    _emit_event(emit, {"stage": "observations"})
    observations = generate_observations(
        comparison, provider, extraction_model,
        prompt=observations_prompt, emit=emit)

    matches = sum(1 for r in comparison if r["status"] == "match")
    mismatches = sum(1 for r in comparison if r["status"] == "mismatch")
    missing = sum(1 for r in comparison if r["status"] == "missing")

    summary = {
        "matches": matches,
        "mismatches": mismatches,
        "missing": missing,
        "fields": len(comparison),
    }
    _emit_event(emit, {"stage": "done", "summary": summary})

    return {
        "extracted": {
            "legal_opinion": legal_fields,
            "property_document": property_fields,
        },
        "comparison": comparison,
        "observations": observations,
        "summary": summary,
    }
