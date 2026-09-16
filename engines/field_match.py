"""Field-level equivalence for document cross-checks (Tier 1 + Tier 2).

The extraction prompts deliberately capture values VERBATIM ("extract exactly
as written", "do not modify names or addresses") so the raw text stays quotable
in observations and traceable to a source page. The cost of that is that two
documents almost never spell the same fact the same way — ``15/03/2024`` vs
``15th March 2024``, ``M. A. Khan`` vs ``Muhammad Ali Khan``, ``Plot 12, St. 4``
vs ``Plot No. 12, Street No. 4``. A single casefolded ``==`` reads every one of
those as a discrepancy.

This module decides equivalence in two deterministic tiers, per field:

  Tier 1 — typed canonicalization. Each field is compared as what it IS (a date,
           an identifier, a person/entity name, an address, free prose) rather
           than as an opaque string. Canonical forms that agree are a ``match``
           on basis ``normalized`` — no model involved, fully auditable.
  Tier 2 — graded similarity. When canonical forms disagree, a per-kind score in
           [0, 1] decides: at or above ``match_at`` it is a ``match`` on basis
           ``fuzzy``; at or below ``review_above`` it is a plain ``mismatch``;
           the ambiguous band in between is flagged ``escalate`` for the caller
           to adjudicate (Tier 3, an LLM call — see ``collateral.py``). A second
           score measures CONTAINMENT — whether one entry says everything the
           other does and simply says more — and escalates those rows too, since
           saying more is not the same as disagreeing.

Nothing here calls an LLM, reads a file, or holds state: ``compare_field`` is a
pure function of the two values, which is what makes the deterministic verdicts
reproducible and the escalation set small and bounded.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Callable

from dateutil import parser as date_parser

# Comparison outcomes (the wire contract the frontend renders).
STATUS_MATCH = "match"
STATUS_MISMATCH = "mismatch"
STATUS_MISSING = "missing"

# WHY a row matched — a match is never just "true"; the reviewer needs to know
# how much interpretation went into it. "semantic" is set by the Tier-3
# adjudicator in collateral.py, not here.
BASIS_EXACT = "exact"            # identical once whitespace/case/accents fold
BASIS_NORMALIZED = "normalized"  # identical in canonical form (dates, ids, ...)
BASIS_FUZZY = "fuzzy"            # similarity above the field's auto-match bar
BASIS_TRIMMED = "trimmed"        # agreed only once surrounding narrative was set aside
BASIS_SEMANTIC = "semantic"      # an LLM judged the two values to be the same fact

# Bases that involved judgement rather than a literal agreement — these are the
# rows worth a human glance, and what ``summary.adjudicated`` counts.
JUDGED_BASES = (BASIS_FUZZY, BASIS_TRIMMED, BASIS_SEMANTIC)


# ──────────────────────────────────────────────────────────────────────────────
# Canonicalization (Tier 1)
# ──────────────────────────────────────────────────────────────────────────────

def _fold(text: str) -> str:
    """Drop accents/diacritics so ``Peña`` and ``Pena`` are the same token."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def canon_text(value: Any) -> str | None:
    """Baseline normalization: accent-fold, collapse whitespace, casefold.

    Returns None for None/blank — this, and only this, is what "missing" means.
    A value that is present but uninterpretable is never missing.
    """
    if value is None:
        return None
    text = re.sub(r"\s+", " ", _fold(str(value))).strip()
    return text.casefold() or None


# Partial dates ("March 2024") must fill their missing parts from a FIXED date,
# never from today's — otherwise the same document compares differently
# tomorrow.
_DATE_DEFAULT = datetime(2000, 1, 1)
_DATE_NOISE = re.compile(r"\b(?:dated|date|on|as\s+of|day\s+of|this|the)\b")
_DATE_ORDINAL = re.compile(r"(\d+)(?:st|nd|rd|th)\b")


def canon_date(value: Any) -> frozenset[str] | None:
    """Canonicalize a date to the SET of ISO dates it could plausibly mean.

    A set, not a single date, because ``03/04/2024`` is genuinely ambiguous
    between day-first and month-first conventions and the two documents may not
    share one. Two dates agree when their readings intersect — so ``03/04/2024``
    matches ``4 March 2024`` (a real agreement under one reading) but not
    ``9 May 2024``. Returns None when nothing parses.
    """
    text = canon_text(value)
    if text is None:
        return None
    text = _DATE_ORDINAL.sub(r"\1", _DATE_NOISE.sub(" ", text))

    readings: set[str] = set()
    for dayfirst in (True, False):
        try:
            parsed = date_parser.parse(
                text, dayfirst=dayfirst, fuzzy=True, default=_DATE_DEFAULT)
        except (ValueError, OverflowError, TypeError):
            continue
        readings.add(parsed.date().isoformat())
    return frozenset(readings) or None


# "Plot No. 45/2-B" and "45/2B" are the same plot; the label words, the joining
# words, and the typographic separators are noise. "/" is NOT noise — dropping
# it would make "1/2" equal "12", and a false match on an identifier is the one
# error this module must not make. The register/book/volume an entry sits in is
# likewise NOT noise: entry 1621 of Book I is not entry 1621 of Book II.
_ID_NOISE = re.compile(
    r"\b(?:no|nos|num|number|serial|sr|survey|plot|khasra|khewat|reg|"
    r"registration|registry|deed|ref|reference|of|the|dated|date)\b\.?")
_ID_PUNCT = re.compile(r"[\s.,;:#()\[\]{}_+-]+")


def canon_identifier(value: Any) -> str | None:
    """Canonicalize a plot/survey/registration number to its bare core."""
    text = canon_text(value)
    if text is None:
        return None
    text = _ID_NOISE.sub(" ", text).replace("\\", "/")
    return _ID_PUNCT.sub("", text) or None


_NAME_HONORIFICS = frozenset({
    "mr", "mrs", "ms", "miss", "mister", "messrs", "m/s", "dr", "prof",
    "sir", "engr", "advocate",
})
# Corporate forms are unified rather than dropped: "ABC (Private) Limited" and
# "ABC Pvt Ltd" are the same entity, but the suffix still carries information
# worth keeping in the token set.
_NAME_SYNONYMS = {
    "private": "pvt", "limited": "ltd", "company": "co",
    "corporation": "corp", "incorporated": "inc", "and": "&",
}
# Kept, not stripped: a patronymic identifies WHICH person of that name, so
# "Ali s/o Hassan" must not silently equal "Ali s/o Mahmood".
_NAME_RELATIONS = (
    (re.compile(r"\bson\s+of\b"), "s/o"),
    (re.compile(r"\bdaughter\s+of\b"), "d/o"),
    (re.compile(r"\bwife\s+of\b"), "w/o"),
)
_NAME_KEEP = re.compile(r"[^0-9a-z/&\s]+")


def canon_name(value: Any) -> tuple[str, ...] | None:
    """Canonicalize a person/entity name to a token tuple.

    Order-insensitive by construction (``Khan, Muhammad Ali`` vs
    ``Muhammad Ali Khan``), honorifics removed, initials left as single-letter
    tokens for the scorer to expand.
    """
    text = canon_text(value)
    if text is None:
        return None
    for pattern, replacement in _NAME_RELATIONS:
        text = pattern.sub(replacement, text)
    text = _NAME_KEEP.sub(" ", text.replace(".", " "))

    tokens = [_NAME_SYNONYMS.get(t, t) for t in text.split()]
    return tuple(t for t in tokens if t and t not in _NAME_HONORIFICS) or None


_ADDR_SYNONYMS = {
    "st": "street", "str": "street", "rd": "road", "ave": "avenue",
    "av": "avenue", "blvd": "boulevard", "sec": "sector", "sect": "sector",
    "blk": "block", "ph": "phase", "hse": "house", "flr": "floor",
    "fl": "floor", "apt": "apartment", "dist": "district",
    "distt": "district", "teh": "tehsil", "opp": "opposite", "nr": "near",
}
_ADDR_DROP = frozenset({"no", "number", "the", "of", "at", "near", "opposite"})
_ADDR_KEEP = re.compile(r"[^0-9a-z/\s]+")


def canon_address(value: Any) -> tuple[str, ...] | None:
    """Canonicalize an address to a token tuple (abbreviations expanded).

    Hyphens become separators so ``F-8/3`` and ``F 8/3`` yield the same tokens;
    ``/`` survives because it separates sub-plots.
    """
    text = canon_text(value)
    if text is None:
        return None
    text = _ADDR_KEEP.sub(" ", text.replace("-", " "))

    tokens = [_ADDR_SYNONYMS.get(t, t) for t in text.split()]
    return tuple(t for t in tokens if t and t not in _ADDR_DROP) or None


_PROSE_KEEP = re.compile(r"[^0-9a-z\s]+")


def canon_prose(value: Any) -> str | None:
    """Canonicalize free text: punctuation out, single spaces.

    Prose fields (a property description, an enforceability statement) are never
    auto-matched below identity — restating the same thing in different words is
    the norm, not the exception, so anything short of an exact canonical hit goes
    to Tier 3.
    """
    text = canon_text(value)
    if text is None:
        return None
    return re.sub(r"\s+", " ", _PROSE_KEEP.sub(" ", text)).strip() or None


# ──────────────────────────────────────────────────────────────────────────────
# Similarity scoring (Tier 2)
# ──────────────────────────────────────────────────────────────────────────────

def _ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def _token_eq(left: str, right: str) -> bool:
    """Token equality that tolerates initials and OCR noise.

    A single letter matches any token it initials (``m`` ~ ``muhammad``); longer
    tokens tolerate a small edit distance (``mohammad`` ~ ``muhammad``). Short
    tokens are held to exact equality — at 3 characters a fuzzy ratio stops
    discriminating between genuinely different words.
    """
    if left == right:
        return True
    if len(left) == 1:
        return right.startswith(left)
    if len(right) == 1:
        return left.startswith(right)
    if len(left) >= 4 and len(right) >= 4:
        return _ratio(left, right) >= 0.85
    return False


def _matched_tokens(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    """How many of ``left``'s tokens find a partner in ``right``, greedily and
    without reusing a partner. Order-insensitive."""
    pool = list(right)
    matched = 0
    for token in left:
        for index, candidate in enumerate(pool):
            if _token_eq(token, candidate):
                pool.pop(index)
                matched += 1
                break
    return matched


def _common_chars(left: str, right: str) -> int:
    """Total length of the runs of characters the two strings share."""
    return sum(block.size for block in
               SequenceMatcher(None, left, right).get_matching_blocks())


def score_tokens(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    """Order-insensitive token overlap, penalised by the LONGER side.

    Extra tokens cost score, so a name that is a strict subset of the other
    ("Muhammad Ali" inside "Muhammad Ali Khan") does not auto-match — the
    missing part might be what distinguishes two different people.
    """
    if not left or not right:
        return 0.0
    return _matched_tokens(left, right) / max(len(left), len(right))


def cover_tokens(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    """The same overlap measured against the SHORTER side: "is everything the
    briefer entry says also present in the fuller one?"."""
    if not left or not right:
        return 0.0
    return _matched_tokens(left, right) / min(len(left), len(right))


def score_string(left: str, right: str) -> float:
    """Character-level similarity, for identifiers and prose."""
    return _ratio(left, right)


def cover_string(left: str, right: str) -> float:
    """Character-level containment of the shorter string in the longer one."""
    if not left or not right:
        return 0.0
    return _common_chars(left, right) / min(len(left), len(right))


def score_readings(left: frozenset[str], right: frozenset[str]) -> float:
    """Dates agree only if some reading is shared — never partially."""
    return 1.0 if left & right else 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Per-field rules
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FieldRule:
    """How one field is compared.

    ``kind`` selects the canonicalizer/scorer pair. Scores at or above
    ``match_at`` auto-match; scores at or below ``review_above`` are a plain
    mismatch; the band between them escalates to Tier 3. Setting
    ``match_at = 1.0`` means "canonical identity or a human/LLM decides" — the
    right setting for anything where a near miss is itself the finding.
    """
    kind: str
    match_at: float
    review_above: float


# kind -> (canonicalizer, symmetric score, containment score). The containment
# score never decides a match; it only rescues rows from being dismissed. Dates
# have no notion of "partly contained", so they reuse their all-or-nothing score.
_KINDS: dict[str, tuple[Callable[[Any], Any],
                        Callable[[Any, Any], float],
                        Callable[[Any, Any], float]]] = {
    "date": (canon_date, score_readings, score_readings),
    "identifier": (canon_identifier, score_string, cover_string),
    "name": (canon_name, score_tokens, cover_tokens),
    "address": (canon_address, score_tokens, cover_tokens),
    "prose": (canon_prose, score_string, cover_string),
}

# When one entry says everything the other does and simply says MORE, the extra
# detail is not disagreement — "Tariq Mehmood" and "Tariq Mehmood S/o Khalid
# Mehmood" do not contradict each other. The symmetric score punishes exactly
# that shape (2 of 5 tokens = 0.40) and would dismiss it silently. So: near-total
# containment sends the row to the adjudicator instead. It is deliberately only
# an escalation trigger, never a match — whether the omitted detail matters is a
# judgement call ("Ali s/o Hassan" vs "Ali", where the fuller entry may well
# describe a different person), and judgement calls are Tier 3's job.
CONTAINMENT_ESCALATE = 0.9

# Thresholds are per KIND of fact, not per field name, and are set by how
# expensive a wrong auto-match is: identifiers and dates never auto-match on
# similarity (a digit off is the whole point), addresses tolerate a trailing
# "Pakistan", names tolerate initials and reordering.
_DEFAULT_RULE = FieldRule("prose", 1.0, 0.0)

FIELD_RULES: dict[str, FieldRule] = {
    "property_address": FieldRule("address", 0.85, 0.45),
    "plot_or_survey_number": FieldRule("identifier", 1.0, 0.55),
    "land_registration_number": FieldRule("identifier", 1.0, 0.55),
    "property_description": FieldRule("prose", 1.0, 0.0),
    "property_owner_name": FieldRule("name", 0.90, 0.40),
    "mortgagor_name": FieldRule("name", 0.90, 0.40),
    "legal_opinion_date": FieldRule("date", 1.0, 0.0),
    "registration_authority": FieldRule("name", 0.90, 0.35),
    "mortgage_enforceability_reference": FieldRule("prose", 1.0, 0.0),
}


@dataclass(frozen=True)
class Verdict:
    """One field's deterministic outcome.

    ``escalate`` marks the ambiguous band: the status is the CONSERVATIVE answer
    (mismatch) and stays that way unless Tier 3 positively resolves it, so a
    failed or skipped adjudication can only ever over-report discrepancies.
    """
    status: str
    basis: str | None
    score: float | None
    escalate: bool


def compare_field(field: str, legal_value: Any, property_value: Any) -> Verdict:
    """Compare one field's two values through Tier 1 then Tier 2."""
    rule = FIELD_RULES.get(field, _DEFAULT_RULE)

    text_legal = canon_text(legal_value)
    text_property = canon_text(property_value)
    if text_legal is None or text_property is None:
        return Verdict(STATUS_MISSING, None, None, False)
    if text_legal == text_property:
        return Verdict(STATUS_MATCH, BASIS_EXACT, 1.0, False)

    canon, score, cover = _KINDS[rule.kind]
    canon_legal = canon(legal_value)
    canon_property = canon(property_value)
    if canon_legal is None or canon_property is None:
        # Present but uninterpretable (an unparseable date, an id with no
        # alphanumerics). Not missing, and not something to rule on here.
        return Verdict(STATUS_MISMATCH, None, None, True)
    if canon_legal == canon_property:
        return Verdict(STATUS_MATCH, BASIS_NORMALIZED, 1.0, False)

    similarity = round(score(canon_legal, canon_property), 3)
    if similarity >= rule.match_at:
        return Verdict(STATUS_MATCH, BASIS_FUZZY, similarity, False)

    contained = cover(canon_legal, canon_property) >= CONTAINMENT_ESCALATE
    return Verdict(STATUS_MISMATCH, None, similarity,
                   similarity > rule.review_above or contained)
