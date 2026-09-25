"""Collateral Reviewer engine (ported from ``subsystems/collateral_reviewer/main.py``).

Cross-checks a property's LEGAL OPINION (issued by a lawyer) against its
PROPERTY / TITLE document. The pipeline:

  1) Extract a fixed CAD field set from EACH document with an LLM
     (the canonical schema ported from the original collateral-reviewer:
     property_information / ownership_information / legal_information, each
     field -> {value, source_page, core} — ``value`` verbatim for display and
     provenance, ``core`` the same fact with the surrounding narrative removed
     so the comparison has one fact to compare rather than a whole clause).
     Every extracted value is then located back in the document text it came
     from (``app.engines.evidence``), so a field is a citation rather than a
     claim — and a value that CANNOT be found is reported as such.
  2) Compare the two documents field-by-field (match / mismatch / missing) in
     three tiers: typed canonicalization then graded similarity, both
     deterministic and both in ``app.engines.field_match``; then a single
     batched LLM adjudication of only the rows those two left ambiguous.
     Because extraction captures values verbatim by design, the same fact
     routinely arrives worded differently in the two documents — the tiers exist
     so that stops reading as a discrepancy. Every match records WHY it
     matched (``match_basis``), since a match resolved by judgement is weaker
     evidence than a literal one.
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
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

# from app.engines.evidence import has_page_markers, locate_value
# from app.engines.extraction import extract_document
# from app.engines.field_match import ...
# from app.engines.util import EngineParseError, parse_json_response
from engines.document_kind import (LEGAL_OPINION, PROPERTY_DOCUMENT,
                                   DocumentKindError, check_not_duplicate,
                                   verify_document_kind)
from engines.evidence import has_page_markers, locate_value
from engines.extraction import TranscriptionResult, extract_document
from engines.field_match import (
    BASIS_SEMANTIC,
    BASIS_TRIMMED,
    JUDGED_BASES,
    STATUS_MATCH,
    STATUS_MISMATCH,
    STATUS_MISSING,
    canon_text,
    compare_field,
)
from engines.token_usage import add_usage, call_with_usage, new_usage
from engines.util import EngineParseError, parse_json_response

if TYPE_CHECKING:  # pragma: no cover — typing only; engines stay import-pure
    from app.llm.base import LLMProvider

# emit(type, text) with type in {"reasoning", "content", "event"}; "event"
# carries a compact single-encoded JSON string.
EmitFn = Callable[[str, str], None]

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


# ──────────────────────────────────────────────────────────────────────────────
# 1) Canonical CAD field set
#    Field names ported verbatim from the original extraction/schemas.py. The
#    two top-level docs ("legal_opinion", "property_document") share an
#    identical field layout.
# ──────────────────────────────────────────────────────────────────────────────

def _blank_field() -> dict[str, Any]:
    """One leaf of the schema.

    ``value`` is the text exactly as the document words it — that is what the
    reviewer reads, what the observations quote, and what ``source_page``
    points at, so it is never trimmed or rewritten. ``core`` is the same fact
    with the surrounding narrative removed ("1621 of Book No-I dated
    12.03.2024" -> "1621 of Book No-I"), and exists ONLY so the comparison has
    a single fact to compare instead of a whole clause. Nothing is dropped from
    the result: ``core`` narrows what is compared, never what is stored.
    """
    return {"value": None, "source_page": None, "core": None}


EXTRACTION_SCHEMA: dict[str, Any] = {
    document: {
        "property_information": {
            "property_address": _blank_field(),
            "plot_or_survey_number": _blank_field(),
            "land_registration_number": _blank_field(),
            "property_description": _blank_field(),
        },
        "ownership_information": {
            "property_owner_name": _blank_field(),
            "mortgagor_name": _blank_field(),
        },
        "legal_information": {
            "legal_opinion_date": _blank_field(),
            "registration_authority": _blank_field(),
            "mortgage_enforceability_reference": _blank_field(),
        },
    }
    for document in ("legal_opinion", "property_document")
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


def _emit_usage(emit: EmitFn | None, step: str, spent: dict[str, int],
                running: dict[str, int], ms: int) -> None:
    """Report what one pipeline step cost in tokens and wall-clock, plus the
    review's running total.

    Sent after the step finishes — the ``stage`` events fire before the work, so
    they cannot carry figures that do not exist yet. The exception is vision,
    which reports per page as it goes.
    """
    _emit_event(emit, {"stage": "usage", "step": step, "ms": ms,
                       "usage": dict(spent), "cumulative": dict(running)})


def _parse_json_array(response: str | None) -> list[Any] | None:
    """Parse a JSON ARRAY out of an LLM response. Slices the first '[' to the
    last ']' and json-loads it (``strict=False`` per the shared tolerant-parser
    policy; ``parse_json_response`` itself is object-only). Returns None on any
    failure so callers can fall back deterministically."""
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
    return arr if isinstance(arr, list) else None


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
            leaf = schema_section[section_name][field_name]
            leaf["value"] = value
            leaf["source_page"] = field_data.get("source_page")
            # A model that ignores "core" (or returns a blank one) simply leaves
            # it null, and the comparison falls back to the full value.
            core = field_data.get("core")
            leaf["core"] = core if core not in (None, "") else None


def extract_fields(text: str, doc_name: str, provider: "LLMProvider",
                   model: str, *,
                   prompt: str | None = None) -> tuple[dict[str, Any], dict[str, int]]:
    """Extract the CAD field set for ONE document.

    Returns a populated copy of EXTRACTION_SCHEMA[doc_name] and the tokens the
    call cost. Never crashes on a failed call or bad JSON — on any error it
    returns the (possibly all-null) schema so the pipeline degrades gracefully
    instead of aborting. ``prompt`` overrides the shipped extraction prompt
    template (None -> the frozen ``prompts/collateral_extraction.md``).
    """
    schema = deepcopy(EXTRACTION_SCHEMA[doc_name])

    prompt = build_extraction_prompt(text or "", doc_name, prompt=prompt)
    response, usage = call_with_usage(
        provider, model, [{"role": "user", "content": prompt}])
    if response is None:
        return schema, usage

    try:
        parsed = parse_json_response(response)
    except EngineParseError:
        return schema, usage
    _merge_document_fields(schema, parsed)
    return schema, usage


def attach_evidence(fields: dict[str, Any], text: str) -> dict[str, int]:
    """Locate every extracted value back in its document's text, IN PLACE.

    Each populated leaf gains ``evidence``: ``{start, end, page, found_by,
    confidence}`` pointing into ``text``, or None when the document does not
    contain the value at all. That None is the point of doing this — it says the
    model reported something the document does not say, which nothing else in
    the pipeline can tell you.

    ``evidence`` is added HERE rather than declared in ``EXTRACTION_SCHEMA``
    because the schema doubles as the shape shown to the model in the extraction
    prompt: a field in there is a field the model would try to fill, and where a
    value sits in the text is ours to establish, never the model's to claim.

    The offsets index the exact ``text`` passed in, so whoever stores them must
    store that same text (or be able to reproduce it byte for byte) or they
    point at nothing. Returns ``{"found": n, "total": n}`` over the fields that
    had a value at all.
    """
    found = 0
    total = 0
    for section in fields.values():
        for leaf in section.values():
            if canon_text(leaf.get("value")) is None:
                leaf["evidence"] = None
                continue
            total += 1
            span = locate_value(text, leaf.get("value"), leaf.get("core"))
            if span is None:
                leaf["evidence"] = None
                continue
            found += 1
            leaf["evidence"] = {
                "start": span.start,
                "end": span.end,
                "page": span.page,
                "found_by": span.found_by,
                "confidence": span.confidence,
            }
    return {"found": found, "total": total}


def _source_report(source: TranscriptionResult,
                   fields: dict[str, Any]) -> dict[str, Any]:
    """Attach this document's evidence and describe the text it was read from.

    ``paged`` is the honest answer to "can page numbers be trusted here?" — only
    a page-by-page extraction carries ``=== PAGE N ===`` markers, so for a .docx
    or a text-layer PDF it is False and every ``evidence.page`` is null. A
    consumer should show no page at all in that case rather than fall back to
    the model's ``source_page``, which had nothing in the text to derive from.
    """
    located = attach_evidence(fields, source.text)
    return {
        "chars": len(source.text),
        "pages_total": source.pages_total,
        "pages_failed": list(source.pages_failed),
        "paged": has_page_markers(source.text),
        "values_located": located["found"],
        "values_total": located["total"],
    }


# ──────────────────────────────────────────────────────────────────────────────
# 4) Field-by-field comparison — Tiers 1 & 2, deterministic
# ──────────────────────────────────────────────────────────────────────────────

# Private row key carrying "Tier 1 and 2 could not decide this one" from the
# comparison to the adjudicator. ``adjudicate_comparison`` strips it, so it never
# reaches the stored result contract.
_ESCALATE_KEY = "_needs_adjudication"


def _compare_leaves(field: str, legal_leaf: Mapping[str, Any],
                    property_leaf: Mapping[str, Any]) -> tuple[Any, str | None]:
    """Decide one field from its two schema leaves, using ``core`` as a fallback.

    The full values are compared first, so a field that already agrees as
    written is settled without the trimmed form entering into it. Only when
    that fails does the extractor's ``core`` get a turn — the same fact with the
    surrounding narrative set aside, which is what lets "1621 of Book No-I dated
    12.03.2024" meet "1621, Book No. I".

    ``core`` can only ever HELP: it can turn a mismatch into a match (recorded
    as basis "trimmed", so the row is visibly one where text was set aside), or
    it can push a borderline row to the Tier-3 adjudicator. It can never turn a
    match into a mismatch, and it can never make a present value "missing" — so
    an extractor that omits ``core``, or trims it badly, lands back on exactly
    the full-value behaviour. Returns (verdict, reason).
    """
    full = compare_field(field, legal_leaf.get("value"), property_leaf.get("value"))
    if full.status != STATUS_MISMATCH:
        return full, None

    core_legal = legal_leaf.get("core")
    core_property = property_leaf.get("core")
    if core_legal is None or core_property is None:
        return full, None

    core = compare_field(field, core_legal, core_property)
    if core.status == STATUS_MATCH:
        return (replace(core, basis=BASIS_TRIMMED),
                f'the entries agree on "{core_legal}" and "{core_property}"; '
                f'the rest of each entry is surrounding detail')
    # The trimmed forms did not settle it either — but if they came closer than
    # the full text did, that is worth asking the adjudicator about.
    return replace(full, escalate=full.escalate or core.escalate), None


def _compare_section(legal_fields: dict[str, Any], property_fields: dict[str, Any],
                     section: str, field_names: list[str]) -> list[dict[str, Any]]:
    """Compare a list of fields within one schema section.

    Each result row is {field, label, legal_value (raw), property_value (raw),
    status, match_basis, match_reason, similarity}. Raw values are always
    preserved for display and for quoting in observations; the decision itself
    runs on the field's canonical form (see ``field_match.compare_field``) and,
    failing that, on the extractor's trimmed ``core`` (see ``_compare_leaves``).

    Status: "missing" if either side has no value at all, "match" if the two
    values are equivalent, else "mismatch". Rows the deterministic tiers found
    ambiguous carry a private escalation flag for the Tier-3 adjudicator and
    remain "mismatch" until it positively resolves them.
    """
    results: list[dict[str, Any]] = []
    for field in field_names:
        legal_leaf = legal_fields[section][field]
        property_leaf = property_fields[section][field]
        legal_value = legal_leaf["value"]
        property_value = property_leaf["value"]

        verdict, reason = _compare_leaves(field, legal_leaf, property_leaf)

        results.append({
            "field": field,
            "label": FIELD_LABELS.get(field, field),
            "legal_value": legal_value,
            "property_value": property_value,
            "status": verdict.status,
            "match_basis": verdict.basis,
            "match_reason": reason,
            "similarity": verdict.score,
            _ESCALATE_KEY: verdict.escalate,
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
    (property, then ownership, then legal).

    Rows are PRE-adjudication: ambiguous ones still carry the private escalation
    flag and their conservative "mismatch" status. Pass them through
    ``adjudicate_comparison`` before storing or counting them.
    """
    return (
        compare_property_info(legal_fields, property_fields)
        + compare_ownership(legal_fields, property_fields)
        + compare_legal_info(legal_fields, property_fields)
    )


# ──────────────────────────────────────────────────────────────────────────────
# 5) Tier 3 — one batched LLM adjudication of the ambiguous rows
# ──────────────────────────────────────────────────────────────────────────────

# A cleared discrepancy is not reviewed by a human, so the model has to be sure:
# below this self-reported confidence the deterministic mismatch stands.
ADJUDICATION_MIN_CONFIDENCE = 0.7


def _build_adjudication_prompt(rows: list[dict[str, Any]], *,
                               prompt: str | None = None) -> str:
    """Build the single-call prompt asking whether each ambiguous pair of values
    refers to the same underlying fact (``prompts/collateral_adjudication.md``;
    ``prompt`` overrides that shipped template)."""
    pairs = [
        {
            "index": i,
            "field": row.get("label") or row.get("field"),
            "legal_opinion_value": row.get("legal_value"),
            "property_document_value": row.get("property_value"),
            "textual_similarity": row.get("similarity"),
        }
        for i, row in enumerate(rows)
    ]
    payload = json.dumps(pairs, indent=2, default=str)
    template = prompt if prompt is not None else _load_prompt("collateral_adjudication.md")
    return template.replace("{payload}", payload)


def adjudicate_comparison(comparison: list[dict[str, Any]],
                          provider: "LLMProvider", model: str, *,
                          prompt: str | None = None,
                          emit: EmitFn | None = None
                          ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Resolve the rows Tiers 1 and 2 left ambiguous, IN PLACE, and return them
    with the tokens adjudication cost — zero when no row needed escalating.

    One LLM call for every ambiguous row (at most one per field), asking only
    whether the two values name the same fact. Three invariants keep this safe:

      * Only flagged rows are sent. A row the deterministic tiers decided —
        either way — is never re-litigated by a model.
      * The verdict can only turn "mismatch" into "match" (basis "semantic",
        with the model's reason recorded). It can never manufacture a
        discrepancy, so the worst case is the pre-adjudication result.
      * The response is used only if it lines up 1:1 with what was sent and
        clears ``ADJUDICATION_MIN_CONFIDENCE``; anything else is discarded whole
        and every conservative mismatch stands.

    The escalation flag is stripped either way, so the returned rows are the
    public contract: {field, label, legal_value, property_value, status,
    match_basis, match_reason, similarity}.
    """
    pending = [row for row in comparison if row.get(_ESCALATE_KEY)]
    if not pending:
        for row in comparison:
            row.pop(_ESCALATE_KEY, None)
        return comparison, new_usage()

    _emit_event(emit, {"stage": "adjudicate", "rows": len(pending)})
    response, usage = call_with_usage(
        provider, model,
        [{"role": "user", "content": _build_adjudication_prompt(
            pending, prompt=prompt)}])

    verdicts = _parse_json_array(response)
    # 1:1 or nothing — a partial or reordered array can't be attributed to rows
    # safely, and guessing here would clear the wrong discrepancy.
    if verdicts is not None and len(verdicts) == len(pending):
        for row, verdict in zip(pending, verdicts):
            if not isinstance(verdict, dict) or verdict.get("equivalent") is not True:
                continue
            try:
                confidence = float(verdict.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            if confidence < ADJUDICATION_MIN_CONFIDENCE:
                continue
            reason = str(verdict.get("reason") or "").strip()
            row["status"] = STATUS_MATCH
            row["match_basis"] = BASIS_SEMANTIC
            row["match_reason"] = reason or "the two values state the same fact"

    for row in comparison:
        row.pop(_ESCALATE_KEY, None)
    return comparison, usage


# ──────────────────────────────────────────────────────────────────────────────
# 6) Plain-English observations for the discrepancies
# ──────────────────────────────────────────────────────────────────────────────

def _fallback_observation(row: dict[str, Any]) -> str:
    """Deterministic one-sentence finding for a single non-match row, used when
    the LLM observation call fails or returns unparseable output. Tone mirrors
    the ported banking collateral-review prompt."""
    label = row.get("label") or row.get("field")
    legal_value = row.get("legal_value")
    property_value = row.get("property_value")
    status = row.get("status")

    if status == STATUS_MISSING:
        legal_missing = canon_text(legal_value) is None
        property_missing = canon_text(property_value) is None
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
    """Parse the observation response into a list of non-empty strings, or None
    if there is nothing usable."""
    arr = _parse_json_array(response)
    if arr is None:
        return None
    out = [str(x).strip() for x in arr if str(x).strip()]
    return out or None


def generate_observations(comparison: list[dict[str, Any]],
                          provider: "LLMProvider", model: str, *,
                          prompt: str | None = None,
                          emit: EmitFn | None = None
                          ) -> tuple[list[str], dict[str, int]]:
    """Produce plain-English, one-sentence findings for every non-match row,
    with the tokens the call cost.

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
    non_match_rows = [r for r in comparison if r.get("status") != STATUS_MATCH]
    if not non_match_rows:
        return [], new_usage()

    prompt = _build_observation_prompt(non_match_rows, prompt=prompt)
    messages = [{"role": "user", "content": prompt}]
    response: str | None
    usage = new_usage()
    stream_fn = getattr(provider, "stream", None)

    if emit is not None and callable(stream_fn):
        try:
            chunks: list[str] = []
            for delta in stream_fn(model=model, messages=messages, temperature=0.0,
                                   usage_sink=usage):
                chunks.append(delta)
                emit("content", delta)
            response = "".join(chunks)
        except Exception:
            response = None

    if not (emit is not None and callable(stream_fn)) or response is None:
        # Non-streaming path (no emit, no stream support, or streaming failed).
        # Reassigning drops any usage a part-finished stream recorded, so a
        # retried call is not counted twice.
        response, usage = call_with_usage(provider, model, messages)

    parsed = _parse_observation_array(response)

    # Use the parsed array only if it lines up 1:1 with the discrepancies;
    # otherwise fall back deterministically so every discrepancy is described.
    if parsed and len(parsed) == len(non_match_rows):
        return parsed, usage

    return [_fallback_observation(row) for row in non_match_rows], usage


# ──────────────────────────────────────────────────────────────────────────────
# 7) Orchestration entry point (the module router calls this)
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
    comparison, adjudicates the rows it could not decide mechanically, generates
    observations for what is left, and returns the result.json contract:

        {
          "extracted":   {"legal_opinion": <schema>, "property_document": <schema>},
          "sources":     {"legal_opinion": {...}, "property_document": {...}},
          "source_texts":{"legal_opinion": "<full text>", ...},   # see below
          "comparison":  [ {field, label, legal_value, property_value, status,
                            match_basis, match_reason, similarity}, ... ],
          "observations":[ "<one-sentence discrepancy>", ... ],
          "summary":     {matches, mismatches, missing, adjudicated, fields}
        }

    Each leaf of ``extracted`` carries ``evidence`` — where that value sits in
    the document text ({start, end, page, found_by, confidence}), or null when
    the document does not contain it, which is itself worth showing. ``sources``
    describes each document's text: how long it was, its pages, whether page
    numbers are knowable at all (``paged``), and how many of its values could be
    located.

    ``source_texts`` is the BULK artifact and is meant to be moved, not stored:
    the evidence offsets index these exact strings, so a caller that wants to
    render an evidence span must keep the text verbatim (re-extracting later
    would re-run vision OCR and produce different characters, leaving every
    offset pointing at the wrong place). It is returned separately from
    ``sources`` precisely so a caller can persist it out-of-band and drop it
    from what it stores and audits — see ``collateral-service/main.py``, which
    writes it beside the pair's uploads and pops it off the result.

    ``match_basis`` says how a match was reached ("exact" / "normalized" /
    "fuzzy" / "semantic", null on a non-match) and ``match_reason`` carries the
    adjudicator's one-clause justification for a "semantic" one, so a match that
    rests on judgement is visibly weaker evidence than a literal one.
    ``summary.adjudicated`` counts exactly those judgement calls.

    ``models`` must provide "extraction" (field extraction, adjudication, and
    observations) and "vision" (scanned-page OCR transcription). ``prompts`` may
    override the shipped prompt templates via the optional keys "extraction",
    "adjudication" and "observations" (a missing key -> the frozen prompt file).
    """
    extraction_model = models["extraction"]
    vision_model = models["vision"]
    prompts = prompts or {}
    extraction_prompt = prompts.get("extraction")
    adjudication_prompt = prompts.get("adjudication")
    observations_prompt = prompts.get("observations")

    # Per-step token spend, in pipeline order. "compare" stays at zero by
    # design — Tiers 1 and 2 are pure rules — and is reported anyway so the
    # reviewer can see which parts of the review actually cost anything.
    usage_by_step: dict[str, dict[str, int]] = {}
    # Wall-clock per step. Worth recording beside the tokens because the two
    # come apart: a cheap step on a slow-serving model can dominate the review.
    ms_by_step: dict[str, int] = {}
    total_usage = new_usage()
    started_at: dict[str, float] = {}

    def _start(step: str) -> None:
        started_at[step] = time.monotonic()

    def _ms(step: str) -> int:
        return int((time.monotonic() - started_at.get(step, time.monotonic())) * 1000)

    def _record(step: str, spent: dict[str, int]) -> None:
        elapsed = _ms(step)
        usage_by_step[step] = spent
        ms_by_step[step] = elapsed
        add_usage(total_usage, spent)
        _emit_usage(emit, step, spent, total_usage, elapsed)

    # Vision is the slowest step and bills per page, so its cost is reported as
    # each page lands rather than once both documents are done — otherwise a
    # long scanned review shows nothing at all for minutes.
    extract_text_usage = new_usage()
    _start("extract_text")

    def _page_usage(spent: dict[str, int]) -> None:
        add_usage(extract_text_usage, spent)
        add_usage(total_usage, spent)
        _emit_usage(emit, "extract_text", extract_text_usage, total_usage,
                    _ms("extract_text"))

    _emit_event(emit, {"stage": "extract_text", "document": "legal_opinion"})
    legal_source = extract_document(
        Path(legal_opinion_path), provider, vision_model, emit=emit,
        on_usage=_page_usage)
    _emit_event(emit, {"stage": "extract_text", "document": "property_document"})
    property_source = extract_document(
        Path(property_doc_path), provider, vision_model, emit=emit,
        on_usage=_page_usage)
    legal_text = legal_source.text
    property_text = property_source.text
    # Already accumulated per page above, so this records the step without
    # double-counting. Stays zero when both documents had a usable text layer.
    usage_by_step["extract_text"] = extract_text_usage
    ms_by_step["extract_text"] = _ms("extract_text")
    _emit_usage(emit, "extract_text", extract_text_usage, total_usage,
                ms_by_step["extract_text"])

    # Nothing about a file proves which slot it belongs on — the field it was
    # posted on is the entire claim. Left unchecked, a property deed sent as the
    # legal opinion is compared against the property document (very often the
    # same instrument), every field matches, and the review returns the
    # cleanest report it can produce. A silent false pass.
    #
    # Runs here rather than before extraction because this engine has no
    # partial-read path: the text is already in hand, so the gate is free of
    # further extraction cost and still spares the four LLM calls below.
    _start("verify_documents")
    _emit_event(emit, {"stage": "verify_documents"})
    verify_usage = new_usage()
    with ThreadPoolExecutor(max_workers=2) as ex:
        legal_check = ex.submit(
            verify_document_kind, legal_text, LEGAL_OPINION, provider,
            extraction_model)
        property_check = ex.submit(
            verify_document_kind, property_text, PROPERTY_DOCUMENT, provider,
            extraction_model)
        checks = [legal_check.result(), property_check.result()]
    # Asks the blunter question the kind check cannot: are these the same file?
    # Two property documents carry no legal signature, so both reach the model,
    # which may label one plausibly — identical text cannot be explained away.
    checks.append(check_not_duplicate(legal_text, property_text))
    for check in checks:
        add_usage(verify_usage, check.usage)
    _record("verify_documents", verify_usage)

    failures = [check.detail for check in checks if not check.ok]
    if failures:
        raise DocumentKindError(" ".join(failures))
    warnings = [check.warning for check in checks if check.warning]

    _start("extract_fields")
    _emit_event(emit, {"stage": "extract_fields"})
    with ThreadPoolExecutor(max_workers=2) as ex:
        legal_future = ex.submit(
            extract_fields, legal_text, "legal_opinion", provider,
            extraction_model, prompt=extraction_prompt)
        property_future = ex.submit(
            extract_fields, property_text, "property_document", provider,
            extraction_model, prompt=extraction_prompt)
        legal_fields, legal_usage = legal_future.result()
        property_fields, property_usage = property_future.result()
    _record("extract_fields", add_usage(dict(legal_usage), property_usage))

    # Cite each extracted value back to where it sits in the document text, so
    # a reviewer can check what the model picked instead of taking its word.
    sources = {
        "legal_opinion": _source_report(legal_source, legal_fields),
        "property_document": _source_report(property_source, property_fields),
    }

    _start("compare")
    comparison = run_all_comparisons(legal_fields, property_fields)
    _emit_event(emit, {"stage": "compare", "fields": len(comparison)})
    _record("compare", new_usage())

    # Tier 3 runs before the observations so a difference that is only a
    # difference in wording never becomes a written-up finding.
    _start("adjudicate")
    comparison, adjudication_usage = adjudicate_comparison(
        comparison, provider, extraction_model,
        prompt=adjudication_prompt, emit=emit)
    _record("adjudicate", adjudication_usage)

    _start("observations")
    _emit_event(emit, {"stage": "observations"})
    observations, observations_usage = generate_observations(
        comparison, provider, extraction_model,
        prompt=observations_prompt, emit=emit)
    _record("observations", observations_usage)

    matches = sum(1 for r in comparison if r["status"] == STATUS_MATCH)
    mismatches = sum(1 for r in comparison if r["status"] == STATUS_MISMATCH)
    missing = sum(1 for r in comparison if r["status"] == STATUS_MISSING)
    adjudicated = sum(1 for r in comparison if r["match_basis"] in JUDGED_BASES)

    summary = {
        "matches": matches,
        "mismatches": mismatches,
        "missing": missing,
        # Matches that rest on judgement rather than on the two documents
        # literally agreeing — the subset a reviewer may want to spot-check.
        "adjudicated": adjudicated,
        "fields": len(comparison),
    }
    token_usage = {"by_step": usage_by_step, "total": total_usage,
                   "ms_by_step": ms_by_step, "total_ms": sum(ms_by_step.values())}
    _emit_event(emit, {"stage": "done", "summary": summary,
                       "token_usage": token_usage})

    return {
        "extracted": {
            "legal_opinion": legal_fields,
            "property_document": property_fields,
        },
        "sources": sources,
        # Bulk, and deliberately separate from `sources` so a caller can move it
        # to disk and drop it before storing/auditing the result.
        "source_texts": {
            "legal_opinion": legal_text,
            "property_document": property_text,
        },
        "comparison": comparison,
        "observations": observations,
        "summary": summary,
        "token_usage": token_usage,
        # Set when the document-kind gate could not reach a verdict. The review
        # ran, and says so, rather than leaving a reader to assume it passed.
        "warnings": warnings,
    }
