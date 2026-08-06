"""Valuation Reviewer engine (ported from ``subsystems/valuation_reviewer/main.py``).

Reviews a single property VALUATION REPORT (.pdf or .docx). The pipeline:

  1) Extract a fixed 13-field set from the report with an LLM (the canonical
     extraction prompt is frozen domain IP, stored verbatim in
     ``prompts/valuation_extraction.md``; only the first 12,000 chars of the
     report text are fed to the prompt).
  2) Panel check: fuzzy-match the valuer firm name against the bank's
     approved-valuer PANEL (an Annexure-A Excel with sheets Category A/B/C),
     read out its per-transaction limit, and compare the property value
     (land_value + building_value) against that limit.
  3) Policy rules: a valuation-expiry alert (valuation_date + N years) and a
     self-valuation conflict flag (the valuer firm == the owning entity).
  4) Cushion + lending limit: cushion = value * margin%, net drawable amount =
     value - cushion.

PORTED-BUG FIXES kept from the legacy engine (vs the original source project):
  (1) _parse_date_string's last-resort fallback parses the actual string via
      dateutil and returns None on failure (the original passed the imported
      ``date`` MODULE and crashed).
  (2) self_valuation_check guards with .get and returns None when owned_by /
      valuation_in_name_of is absent (the original KeyError'd).
  (3) panel "Any Amount" stays the STRING "Any Amount" (always WITHIN LIMIT) —
      never float("inf"), so the result is JSON-serializable end-to-end.
  (4) land_value / building_value comma-strings ("24,650,000") are coerced by
      _money() before any math; unparseable values become 0.

Pure logic: no FastAPI, no SQLAlchemy, no ``app.core.config``. The provider,
model names, panel path, and the optional ``emit`` progress callback all
arrive as arguments; ``panel_path=None`` falls back to the bundled
``data/default_panel.xlsx``. Text/OCR extraction goes through the shared
``app.engines.extraction.extract_document``; the panel Excel is read with
openpyxl (NOT pandas); LLM-JSON parsing goes through the shared tolerant
parser in ``app.engines.util``.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

import openpyxl
from dateutil import parser as _date_parser

from app.engines.extraction import extract_document
from app.engines.util import EngineParseError, parse_json_response

if TYPE_CHECKING:  # pragma: no cover — typing only; engines stay import-pure
    from app.llm.base import LLMProvider

# emit(type, text) with type in {"reasoning", "content", "event"}; "event"
# carries a compact single-encoded JSON string.
EmitFn = Callable[[str, str], None]

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Bundled Annexure-A panel used when the caller configures no panel of its own.
DEFAULT_PANEL_PATH = Path(__file__).resolve().parent / "data" / "default_panel.xlsx"

# How many chars of the report text to feed the extraction prompt (ported from
# the source pdf_reader.llm_extract: text[:12000]).
MAX_PROMPT_CHARS = 12000

# Fuzzy panel-match cutoff (ported: difflib.get_close_matches cutoff=0.6).
PANEL_MATCH_CUTOFF = 0.6

# Sheet numbers (e.g. 1500) are in MILLIONS — multiply by this to get the
# absolute per-transaction limit (ported from the source routes.py STEP 6).
MILLION = 1_000_000

# Defaults for the lending-limit math. NOTE: cushion_pct is a PERCENT here
# (30.0 -> 30% margin); the legacy engine took a fraction (0.30).
DEFAULT_CUSHION_PCT = 30.0
DEFAULT_EXPIRY_YEARS = 3

# The 12 structured fields the result contract exposes under extracted_fields
# (valuator_comments is the 13th extracted field but lives at the top level).
EXTRACTED_FIELD_KEYS = [
    "valuation_company",
    "valuation_in_name_of",
    "property_address",
    "owned_by",
    "valuation_date",
    "type_of_land",
    "status_of_land",
    "valuation_type",
    "assets_evaluated",
    "land_value",
    "building_value",
    "construction_status",
]


# ──────────────────────────────────────────────────────────────────────────────
# 1) Prompt loading + progress helper + report-text cleanup
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


def _clean_report_text(text: str) -> str:
    """Ported regex cleanup from the source pdf_reader.extract_text_from_pdf:
    strip "<n> of <m>" page-number markers and collapse runs of blank lines."""
    if not text:
        return ""
    text = re.sub(r"\d+ of \d+", "", text)   # remove page numbers
    text = re.sub(r"\n\s*\n", "\n", text)     # remove extra blank lines
    return text.strip()


# ──────────────────────────────────────────────────────────────────────────────
# 2) Field extraction (13-field prompt, frozen wording)
# ──────────────────────────────────────────────────────────────────────────────

def extract_fields(text: str, provider: "LLMProvider", model: str, *,
                   prompt: str | None = None) -> dict[str, Any]:
    """Extract the 13-field set from the valuation report text.

    Returns the parsed JSON dict (the raw LLM extraction, possibly with missing
    keys). Never crashes on a failed call or bad JSON — on any error it returns
    {} so the pipeline degrades gracefully (callers fill missing keys with None).
    ``prompt`` overrides the shipped extraction prompt template (None -> the
    frozen ``prompts/valuation_extraction.md``).
    """
    template = prompt if prompt is not None else _load_prompt("valuation_extraction.md")
    prompt = template.replace(
        "{report_text}", (text or "")[:MAX_PROMPT_CHARS])
    try:
        response = provider.call(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
    except Exception:
        return {}

    try:
        return parse_json_response(response)
    except EngineParseError:
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# 3) Money coercion (BUG FIX 4)
# ──────────────────────────────────────────────────────────────────────────────

def _money(v: Any) -> float:
    """Coerce a possibly-string, possibly-None monetary value into a float.

    Handles None, ints/floats, and comma/currency strings ("24,650,000",
    "PKR 19,631,000", "44.281m" -> best effort). Strips commas, currency
    symbols, and any non-numeric leading/trailing characters. Returns 0.0 for
    None / blank / unparseable (BUG FIX 4: never let a comma-string crash the
    math). booleans are treated as 0 (they are not monetary values).
    """
    if v is None or isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except (ValueError, OverflowError):
            return 0.0
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0.0
        # Drop everything except digits, dot and minus (kills commas, currency
        # codes/symbols, spaces, stray text).
        cleaned = re.sub(r"[^0-9.\-]", "", s)
        if cleaned in ("", "-", ".", "-.", ".-"):
            return 0.0
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    return 0.0


# ──────────────────────────────────────────────────────────────────────────────
# 4) Panel checker (openpyxl, fuzzy match)
# ──────────────────────────────────────────────────────────────────────────────

# Header / title cells that appear in the name column but are NOT valuer firms.
# Skipping them keeps a fuzzy match from accidentally landing on a header row.
_PANEL_HEADER_TOKENS = {
    "category a", "category b", "category c",
    "valuer name", "s.no", "s.no.", "s. no", "s no",
    "annexure a (i)", "annexure a (ii)", "annexure a (iii)",
    "limit per transaction (million)",
}


def _load_panel_entries(panel_path: str | Path) -> list[tuple[str, Any]]:
    """Read the Annexure-A panel Excel with openpyxl (NOT pandas) and return a
    list of (display_name, raw_limit) across every Category sheet.

    Sheet layout (confirmed against the sample ntf1AnnxA.xlsx): each sheet
    "Category A/B/C" has the valuer name in column B (index 1) and the limit in
    column C (index 2). The first rows are title/header ("Annexure A (..)",
    "Category X", then "S.No / Valuer Name / Limit ..."), so we skip any row
    whose name cell is empty or a known header token. Returns [] on any read
    error so the caller can degrade gracefully.
    """
    entries: list[tuple[str, Any]] = []
    try:
        wb = openpyxl.load_workbook(str(panel_path), data_only=True, read_only=True)
    except Exception:
        return entries
    try:
        for sheet in wb.worksheets:
            for row in sheet.iter_rows(values_only=True):
                if not row or len(row) < 3:
                    continue
                name, limit = row[1], row[2]
                if name is None:
                    continue
                name_str = str(name).strip()
                if not name_str or name_str.lower() in _PANEL_HEADER_TOKENS:
                    continue
                entries.append((name_str, limit))
    finally:
        wb.close()
    return entries


def _normalize_limit(raw_limit: Any) -> float | str:
    """Turn a raw panel limit cell into the contract's panel_limit value.

    "Any Amount" (case-insensitive) -> the string "Any Amount" (UNLIMITED;
    JSON-serializable, always within limit — BUG FIX 3: never float("inf")).
    A numeric value (or numeric string like "1,500") is in MILLIONS, so it is
    scaled by 1,000,000 to the absolute per-transaction limit. A blank or
    unparseable numeric cell is treated as unlimited rather than a spurious
    zero limit.
    """
    if isinstance(raw_limit, str) and raw_limit.strip().lower() == "any amount":
        return "Any Amount"
    if isinstance(raw_limit, bool):
        return "Any Amount"
    if isinstance(raw_limit, (int, float)):
        return float(raw_limit) * MILLION
    num = _money(raw_limit)
    if num == 0:
        return "Any Amount"
    return num * MILLION


def check_panel(panel_name: Any, panel_path: str | Path | None) -> dict[str, Any]:
    """Resolve a valuer firm name to a panel verdict against the bank's panel.

    Reads the panel Excel with openpyxl, collects (name, limit) pairs from the
    Category A/B/C sheets, and fuzzy-matches ``panel_name`` with
    difflib.get_close_matches (cutoff 0.6) on the lower-cased name. Returns a
    dict shaped for the result contract:

        {"matched_name": <str|None>,
         "panel_limit":  "Any Amount" | <number> | None,
         "panel_status": "matched" | "Panel not found"
                         | "No panel name extracted" | "No panel configured"}

    panel_status semantics (ported from the source routes.py STEP 6, extended
    with the contract's two extra states):
      * "No panel configured"    -> panel_path is None / missing on disk.
      * "No panel name extracted"-> the report yielded no valuer firm name.
      * "Panel not found"        -> name present but no fuzzy match on the panel.
      * "matched"                -> a fuzzy match was found (limit returned).
    """
    # No panel file configured for this profile.
    if not panel_path or not Path(str(panel_path)).is_file():
        return {"matched_name": None, "panel_limit": None,
                "panel_status": "No panel configured"}

    # No firm name extracted from the report.
    target = str(panel_name or "").strip()
    if not target:
        return {"matched_name": None, "panel_limit": None,
                "panel_status": "No panel name extracted"}

    entries = _load_panel_entries(panel_path)
    if not entries:
        # Panel file exists but yielded no usable rows — treat as not-found.
        return {"matched_name": None, "panel_limit": None,
                "panel_status": "Panel not found"}

    by_lower = {n.strip().lower(): (n, l) for (n, l) in entries}
    matches = difflib.get_close_matches(
        target.lower(), list(by_lower.keys()), n=5, cutoff=PANEL_MATCH_CUTOFF)
    if not matches:
        return {"matched_name": None, "panel_limit": None,
                "panel_status": "Panel not found"}

    matched_name, raw_limit = by_lower[matches[0]]
    return {
        "matched_name": matched_name,
        "panel_limit": _normalize_limit(raw_limit),
        "panel_status": "matched",
    }


# ──────────────────────────────────────────────────────────────────────────────
# 5) Policy rules (WITH the ported bug fixes)
# ──────────────────────────────────────────────────────────────────────────────

def _parse_date_string(input_date_str: Any) -> _dt.date | None:
    """Parse a variety of human-readable date strings into a date object.

    Handles ISO (YYYY-MM-DD), "31st August 2019" / "31 August 2019" (ordinal
    suffixes stripped), and anything else dateutil can read.

    BUG FIX (1): the original source's last-resort fallback called
    parse_date(date) — passing the imported ``date`` MODULE instead of the
    string, which crashed. Here the fallback parses the actual cleaned string
    via dateutil and returns None on failure (never raises).
    """
    if input_date_str is None:
        return None
    clean = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", str(input_date_str)).strip()
    if not clean:
        return None

    # try isoformat first
    try:
        return _dt.datetime.fromisoformat(clean).date()
    except ValueError:
        pass

    # try day month year with full month name
    try:
        return _dt.datetime.strptime(clean, "%d %B %Y").date()
    except ValueError:
        pass

    # last resort: parse the actual string with dateutil (the fixed fallback).
    try:
        return _date_parser.parse(clean).date()
    except (ValueError, OverflowError, TypeError):
        return None


def _to_date(input_date: Any) -> _dt.date | None:
    """Coerce a date / datetime / string into a date, or None (never raises)."""
    if isinstance(input_date, _dt.datetime):
        return input_date.date()
    if isinstance(input_date, _dt.date):
        return input_date
    if isinstance(input_date, str):
        return _parse_date_string(input_date)
    return None


def _add_years(d: _dt.date, years: int) -> _dt.date:
    """The date exactly ``years`` after d, handling Feb-29 (ported)."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + years)


def alert_on_expiry(valuation_date: Any,
                    expiry_years: int = DEFAULT_EXPIRY_YEARS) -> str:
    """Build the valuation-expiry alert string (ported alert_on_expiry).

    The report expires ``expiry_years`` after valuation_date. Returns a
    sentence stating either how many days ago it expired or how many days
    remain. If the date can't be parsed, returns a graceful 'could not be
    determined' message instead of crashing (BUG FIX 1 lets the parse fail
    softly).
    """
    d = _to_date(valuation_date)
    if d is None:
        return ("Valuation expiry could not be determined "
                "(valuation date missing or unparseable).")

    expiry = _add_years(d, expiry_years)
    days = (expiry - _dt.date.today()).days
    if days < 0:
        return f"Alert: Valuation report has expired {abs(days)} days ago."
    return f"Valuation report is expiring in {days} days."


def self_valuation_check(fields: Any) -> str | None:
    """Flag when the valuation was conducted by the entity that owns the
    property (ported self_valuation_check, with BUG FIX 2).

    BUG FIX (2): the original indexed fields["valuation_in_name_of"] /
    fields["owned_by"] directly and KeyError'd when either was missing. Here we
    guard with .get and return None if either side is absent/blank. Returns the
    flag string on a fuzzy match (difflib cutoff 0.6), else None.
    """
    if not isinstance(fields, dict):
        return None
    valuation_in_name_of = fields.get("valuation_in_name_of")
    owned_by = fields.get("owned_by")
    if not valuation_in_name_of or not owned_by:
        return None

    matches = difflib.get_close_matches(
        str(valuation_in_name_of), [str(owned_by)], n=5, cutoff=0.6)
    if matches:
        return "Flag: Valuation conducted by the client itself."
    return None


# ──────────────────────────────────────────────────────────────────────────────
# 6) Orchestration entry point (the module router calls this)
# ──────────────────────────────────────────────────────────────────────────────

def review_valuation(
    report_path: Path,
    panel_path: Path | None,
    provider: "LLMProvider",
    models: Mapping[str, str],
    *,
    cushion_pct: float = DEFAULT_CUSHION_PCT,
    expiry_years: int = DEFAULT_EXPIRY_YEARS,
    prompt: str | None = None,
    emit: EmitFn | None = None,
) -> dict[str, Any]:
    """Full valuation-review pipeline. Ports the source routes.py STEP 1-9
    orchestration and produces the EXACT result.json contract:

        {
          "extracted_fields":   {<12 structured fields, missing -> None>},
          "valuator_comments":  "<string>",
          "panel_review":       {panel_name, matched_name, panel_limit,
                                 property_value, limit_status, panel_status},
          "policy_review":      {valuation_expiry_alert, self_valuation_check},
          "cushion_calculation":{collateral_value, approved_margin, cushion},
          "lending_limit":      {net_drawable_amount}
        }

    ``cushion_pct`` is a PERCENT (30.0 -> 30% margin). ``panel_path`` is the
    profile's approved-valuer panel Excel; None falls back to the bundled
    ``data/default_panel.xlsx``. ``models`` must provide "extraction" (field
    extraction) and "vision" (scanned-page OCR transcription). ``prompt``
    overrides the shipped extraction prompt template (None -> the frozen
    ``prompts/valuation_extraction.md``). Every number in the result is plain
    int/float (JSON-serializable); panel_limit is "Any Amount" (unlimited), a
    number, or None.
    """
    effective_panel: Path = (
        Path(panel_path) if panel_path is not None else DEFAULT_PANEL_PATH)

    # STEP 1-2 — report text (shared extractor, OCR fallback) + ported cleanup.
    _emit_event(emit, {"stage": "extract_text", "document": "valuation_report"})
    text = _clean_report_text(
        extract_document(Path(report_path), provider, models["vision"], emit=emit).text)

    # STEP 3-4 — extract the structured fields from the report text.
    _emit_event(emit, {"stage": "extract_fields"})
    # Pass the override only when set — the None call shape is unchanged
    # (callers/tests may stub extract_fields with the original signature).
    if prompt is None:
        data = extract_fields(text, provider, models["extraction"])
    else:
        data = extract_fields(text, provider, models["extraction"], prompt=prompt)
    if not isinstance(data, dict):
        data = {}

    # STEP 5 — valuator comments (top-level, not under extracted_fields).
    valuator_comments = data.get("valuator_comments") or ""

    # The 12 structured fields the contract exposes; missing keys -> None.
    extracted_fields = {k: data.get(k) for k in EXTRACTED_FIELD_KEYS}

    # property_value = land_value + building_value, comma-strings coerced (FIX 4).
    property_value = _money(data.get("land_value")) + _money(data.get("building_value"))

    # STEP 6 — panel check.
    panel_name = data.get("valuation_company")
    panel_verdict = check_panel(panel_name, effective_panel)

    panel_limit = panel_verdict["panel_limit"]
    panel_status = panel_verdict["panel_status"]

    # limit_status: WITHIN/EXCEEDS only when we actually matched a panel entry.
    # "Any Amount" (unlimited) is always WITHIN; otherwise compare numerically.
    limit_status: str | None
    if panel_status == "matched":
        if panel_limit == "Any Amount":
            limit_status = "WITHIN LIMIT"
        elif property_value > panel_limit:
            limit_status = "EXCEEDS LIMIT"
        else:
            limit_status = "WITHIN LIMIT"
    else:
        limit_status = None

    panel_review = {
        "panel_name": panel_name,
        "matched_name": panel_verdict["matched_name"],
        "panel_limit": panel_limit,
        "property_value": property_value,
        "limit_status": limit_status,
        "panel_status": panel_status,
    }
    _emit_event(emit, {"stage": "panel_check", "panel_status": panel_status,
                       "limit_status": limit_status})

    # STEP 7 — policy rules.
    policy_review = {
        "valuation_expiry_alert": alert_on_expiry(
            data.get("valuation_date"), expiry_years=expiry_years),
        "self_valuation_check": self_valuation_check(data),
    }

    # STEP 8 — cushion + lending limit (cushion_pct is a percent).
    cushion = property_value * (cushion_pct / 100.0)
    net_drawable = property_value - cushion

    cushion_calculation = {
        "collateral_value": property_value,
        "approved_margin": f"{cushion_pct:.1f}%",
        "cushion": cushion,
    }
    lending_limit = {"net_drawable_amount": net_drawable}

    _emit_event(emit, {"stage": "done"})

    # STEP 9 — final contract.
    return {
        "extracted_fields": extracted_fields,
        "valuator_comments": valuator_comments,
        "panel_review": panel_review,
        "policy_review": policy_review,
        "cushion_calculation": cushion_calculation,
        "lending_limit": lending_limit,
    }
