"""Collateral review engine: legal opinion vs property/title cross-check.

Ported from cad-workbench's app/engines/collateral.py. Pipeline:

  1) Extract a fixed field set from EACH document with an LLM.
  2) Compare the two documents field-by-field with normalization
     (match / mismatch / missing).
  3) Generate plain-English, one-sentence observations for the discrepancies
     (single LLM call, validated 1:1 against the discrepancy count,
     deterministic fallback otherwise).

DOMAIN IP (ported verbatim): the field set, the extraction prompt wording, and
the observation prompt tone — stored as prompts/*.md next to this module.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from extraction import extract_document
from util import EngineParseError, parse_json_response

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


# --- canonical field set -------------------------------------------------------
# Each leaf field is {value, source_page}. The two top-level docs
# ("legal_opinion", "property_document") share an identical field layout.

EXTRACTION_SCHEMA: dict[str, Any] = {
    "legal_opinion": {
        "property_information": {
            "property_address": {"value": None, "source_page": None},
            "plot_or_survey_number": {"value": None, "source_page": None},
            "land_registration_number": {"value": None, "source_page": None},
            "property_description": {"value": None, "source_page": None},
        },
        "ownership_information": {
            "property_owner_name": {"value": None, "source_page": None},
            "mortgagor_name": {"value": None, "source_page": None},
        },
        "legal_information": {
            "legal_opinion_date": {"value": None, "source_page": None},
            "registration_authority": {"value": None, "source_page": None},
            "mortgage_enforceability_reference": {"value": None, "source_page": None},
        },
    },
    "property_document": {
        "property_information": {
            "property_address": {"value": None, "source_page": None},
            "plot_or_survey_number": {"value": None, "source_page": None},
            "land_registration_number": {"value": None, "source_page": None},
            "property_description": {"value": None, "source_page": None},
        },
        "ownership_information": {
            "property_owner_name": {"value": None, "source_page": None},
            "mortgagor_name": {"value": None, "source_page": None},
        },
        "legal_information": {
            "legal_opinion_date": {"value": None, "source_page": None},
            "registration_authority": {"value": None, "source_page": None},
            "mortgage_enforceability_reference": {"value": None, "source_page": None},
        },
    },
}

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


@lru_cache(maxsize=None)
def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


# --- per-document field extraction -------------------------------------------------------

def build_extraction_prompt(document_text: str, document_name: str, *, prompt: str | None = None) -> str:
    schema_template = json.dumps(EXTRACTION_SCHEMA[document_name], indent=2)
    template = prompt if prompt is not None else _load_prompt("collateral_extraction.md")
    return (
        template
        .replace("{document_name}", document_name)
        .replace("{schema_template}", schema_template)
        .replace("{document_text}", document_text)
    )


def _merge_document_fields(schema_section: dict[str, Any], extracted: Any) -> None:
    """Merge {section -> field -> {value, source_page}} into a schema section
    IN PLACE. Only known sections/fields are touched, so a partial/garbled
    response can never corrupt the schema."""
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
            schema_section[section_name][field_name]["source_page"] = field_data.get("source_page")


def extract_fields(text: str, doc_name: str, provider, model: str, *, prompt: str | None = None) -> dict[str, Any]:
    """Extract the field set for ONE document. Never crashes on a failed call
    or bad JSON — returns the (possibly all-null) schema on any error so the
    pipeline degrades gracefully instead of aborting."""
    schema = deepcopy(EXTRACTION_SCHEMA[doc_name])
    built_prompt = build_extraction_prompt(text or "", doc_name, prompt=prompt)
    try:
        response = provider.call(model=model, messages=[{"role": "user", "content": built_prompt}], temperature=0.0)
    except Exception:
        return schema

    try:
        parsed = parse_json_response(response)
    except EngineParseError:
        return schema
    _merge_document_fields(schema, parsed)
    return schema


# --- field-by-field comparison -------------------------------------------------------

def _norm(value: Any) -> str | None:
    """Normalize for equality: None/empty -> None; else collapse whitespace,
    strip, casefold. Raw values are preserved in the comparison row."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s.casefold() if s else None


def _compare_section(legal_fields: dict[str, Any], property_fields: dict[str, Any], section: str, field_names: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for field_name in field_names:
        legal_value = legal_fields[section][field_name]["value"]
        property_value = property_fields[section][field_name]["value"]
        n_legal = _norm(legal_value)
        n_property = _norm(property_value)

        if n_legal is None or n_property is None:
            status = "missing"
        elif n_legal == n_property:
            status = "match"
        else:
            status = "mismatch"

        results.append({
            "field": field_name,
            "label": FIELD_LABELS.get(field_name, field_name),
            "legal_value": legal_value,
            "property_value": property_value,
            "status": status,
        })
    return results


def run_all_comparisons(legal_fields: dict[str, Any], property_fields: dict[str, Any]) -> list[dict[str, Any]]:
    return (
        _compare_section(legal_fields, property_fields, "property_information",
                          ["property_address", "plot_or_survey_number", "land_registration_number", "property_description"])
        + _compare_section(legal_fields, property_fields, "ownership_information",
                            ["property_owner_name", "mortgagor_name"])
        + _compare_section(legal_fields, property_fields, "legal_information",
                            ["legal_opinion_date", "registration_authority", "mortgage_enforceability_reference"])
    )


# --- plain-English observations for the discrepancies -------------------------------------------------------

def _fallback_observation(row: dict[str, Any]) -> str:
    """Deterministic one-sentence finding, used when the LLM observation call
    fails or returns unparseable output."""
    label = row.get("label") or row.get("field")
    legal_value = row.get("legal_value")
    property_value = row.get("property_value")
    status = row.get("status")

    if status == "missing":
        legal_missing = _norm(legal_value) is None
        property_missing = _norm(property_value) is None
        if legal_missing and property_missing:
            return f"{label} is missing from both the legal opinion and the property document."
        if legal_missing:
            return f"{label} is recorded as \"{property_value}\" in the property document but is missing from the legal opinion."
        return f"{label} is recorded as \"{legal_value}\" in the legal opinion but is missing from the property document."

    return f"{label} does not match: the legal opinion states \"{legal_value}\" while the property document states \"{property_value}\"."


def _build_observation_prompt(non_match_rows: list[dict[str, Any]], *, prompt: str | None = None) -> str:
    discrepancies = [
        {
            "index": i,
            "field": row.get("label") or row.get("field"),
            "legal_opinion_value": row.get("legal_value"),
            "property_document_value": row.get("property_value"),
            "status": row.get("status"),
        }
        for i, row in enumerate(non_match_rows)
    ]
    payload = json.dumps(discrepancies, indent=2, default=str)
    template = prompt if prompt is not None else _load_prompt("collateral_observations.md")
    return template.replace("{payload}", payload)


def _parse_observation_array(response: str | None) -> list[str] | None:
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
    return out or None


def generate_observations(comparison: list[dict[str, Any]], provider, model: str, *, prompt: str | None = None) -> list[str]:
    """One LLM call for every discrepancy at once. Falls back to a
    deterministic sentence per row if parsing fails, or the count doesn't
    line up 1:1 with the discrepancies."""
    non_match_rows = [r for r in comparison if r.get("status") != "match"]
    if not non_match_rows:
        return []

    built_prompt = _build_observation_prompt(non_match_rows, prompt=prompt)
    try:
        response = provider.call(model=model, messages=[{"role": "user", "content": built_prompt}], temperature=0.0)
    except Exception:
        response = None

    parsed = _parse_observation_array(response)
    if parsed and len(parsed) == len(non_match_rows):
        return parsed
    return [_fallback_observation(row) for row in non_match_rows]


# --- orchestration entry point -------------------------------------------------------

def review_collateral(
    legal_opinion_path: Path,
    property_doc_path: Path,
    provider,
    models: Mapping[str, str],
    *,
    prompts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Full collateral cross-check pipeline. ``models`` must provide
    "extraction" and "vision"; ``prompts`` may override the shipped templates
    via optional keys "extraction"/"observations"."""
    extraction_model = models["extraction"]
    vision_model = models["vision"]
    prompts = prompts or {}
    extraction_prompt = prompts.get("extraction")
    observations_prompt = prompts.get("observations")

    legal_text = extract_document(Path(legal_opinion_path), provider, vision_model).text
    property_text = extract_document(Path(property_doc_path), provider, vision_model).text

    with ThreadPoolExecutor(max_workers=2) as ex:
        legal_future = ex.submit(extract_fields, legal_text, "legal_opinion", provider, extraction_model, prompt=extraction_prompt)
        property_future = ex.submit(extract_fields, property_text, "property_document", provider, extraction_model, prompt=extraction_prompt)
        legal_fields = legal_future.result()
        property_fields = property_future.result()

    comparison = run_all_comparisons(legal_fields, property_fields)
    observations = generate_observations(comparison, provider, extraction_model, prompt=observations_prompt)

    summary = {
        "matches": sum(1 for r in comparison if r["status"] == "match"),
        "mismatches": sum(1 for r in comparison if r["status"] == "mismatch"),
        "missing": sum(1 for r in comparison if r["status"] == "missing"),
        "fields": len(comparison),
    }

    return {
        "extracted": {"legal_opinion": legal_fields, "property_document": property_fields},
        "comparison": comparison,
        "observations": observations,
        "summary": summary,
    }
