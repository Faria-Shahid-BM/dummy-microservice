"""Locate an extracted value back inside the document text it came from.

An LLM reports a field's value; this module answers "where in the document does
that actually appear?" — which is what turns an extracted field from a claim
into a citation. Deterministic, pure, no LLM and no file access: given the text
and the value, it returns a character span (plus the page, where the text
carries page markers) or None.

Why the value is not simply searched for verbatim: extraction captures a value
as the document words it, but the document's own wording arrives through
``app.engines.extraction``, where line breaks and spacing depend on whether the
file was a .docx, a text-layer PDF, or a vision transcription. And a value like
a property description is often lifted from the middle of a long recital, so the
model's rendering of it and the document's own run of characters agree in
substance while differing in whitespace. So the search runs in stages, and
records WHICH stage succeeded:

    exact       the value is in the text character for character
    whitespace  it matches once spacing and case are normalized
    core        the value's trimmed core matches (the full value spans a clause)
    approximate the longest run of the value's words that the text does contain

A value that reaches the end of that list unfound is a finding in itself: the
model reported something the document does not say, which is either heavy
paraphrase or invention. Callers surface that rather than hiding it — hence None
is a real answer, not an error.

Offsets are indices into the EXACT text string passed in. A caller that stores
them must store (or be able to reproduce) that same text, or they point at
nothing.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

# How a span was found, weakest last. Callers show this so a reviewer knows how
# much interpretation stands between the reported value and the document.
FOUND_EXACT = "exact"
FOUND_WHITESPACE = "whitespace"
FOUND_CORE = "core"
FOUND_APPROXIMATE = "approximate"

# An approximate hit has to cover a real share of the value's words. Without a
# floor, any value could be "located" at the document's first "the".
_MIN_APPROXIMATE_COVERAGE = 0.34
_MIN_APPROXIMATE_WORDS = 2

# Written by app.engines.extraction ahead of each page: "=== PAGE 3 ===", or
# "=== PAGE 3 (TRANSCRIPTION FAILED) ===" for a page vision could not read.
_PAGE_MARKER = re.compile(
    r"^===\s*PAGE\s+(\d+)(?:\s*\([^)]*\))?\s*===\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Span:
    """Where a value sits in the document text.

    ``start``/``end`` are character offsets into the text as passed in (``end``
    exclusive). ``page`` is the page the span falls on, or None when the text
    carries no page markers at all — a .docx has no pages, and only a scanned
    PDF is currently marked up, so None means "unknowable", never "page 1".
    ``confidence`` is 1.0 for the three precise stages and the word coverage for
    an approximate one.
    """
    start: int
    end: int
    page: int | None
    found_by: str
    confidence: float


# ──────────────────────────────────────────────────────────────────────────────
# Whitespace/case-insensitive search that can still report original offsets
# ──────────────────────────────────────────────────────────────────────────────

def _normalize(text: str) -> tuple[str, list[int]]:
    """Return (normalized text, offset map back into ``text``).

    Whitespace runs collapse to a single space and letters lower-case, so the
    same value matches whatever spacing the extractor produced. ``offsets[i]``
    is where normalized character ``i`` started in the original, which is what
    lets a match in normalized space be reported as real document offsets.

    Lower-casing is skipped for the rare character that changes LENGTH when
    cased (German ``ß`` -> ``ss``); one such character would desynchronise the
    whole offset map, and leaving it uncased costs only that character's match.
    """
    chars: list[str] = []
    offsets: list[int] = []
    in_space = False
    for index, char in enumerate(text):
        if char.isspace():
            if in_space:
                continue
            chars.append(" ")
            offsets.append(index)
            in_space = True
            continue
        lowered = char.lower()
        chars.append(lowered if len(lowered) == 1 else char)
        offsets.append(index)
        in_space = False
    return "".join(chars), offsets


def _normalize_needle(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _span_from_normalized(offsets: list[int], text_len: int,
                          start: int, length: int) -> tuple[int, int]:
    """Map a [start, start+length) hit in normalized space back to the original.

    The end offset comes from the LAST matched character rather than from the
    character after it, so a match ending at a collapsed whitespace run does not
    swallow the whitespace that follows it.
    """
    original_start = offsets[start]
    last = start + length - 1
    original_end = offsets[last] + 1 if last < len(offsets) else text_len
    return original_start, original_end


def _find_normalized(text: str, normalized_text: str, offsets: list[int],
                     value: str) -> tuple[int, int] | None:
    needle = _normalize_needle(value)
    if not needle:
        return None
    position = normalized_text.find(needle)
    if position == -1:
        return None
    return _span_from_normalized(offsets, len(text), position, len(needle))


def _find_longest_word_run(text: str, normalized_text: str, offsets: list[int],
                           value: str) -> tuple[int, int, float] | None:
    """Find the longest contiguous run of ``value``'s words present in the text.

    Tries the whole word sequence, then every shorter contiguous window, longest
    first — so the result is the most of the value the document actually
    contains, and its length over the value's total length is the coverage. Runs
    below the coverage/word floors are rejected: a one-word or one-tenth hit
    locates nothing in particular.
    """
    words = _normalize_needle(value).split()
    if len(words) < _MIN_APPROXIMATE_WORDS:
        return None
    floor = max(_MIN_APPROXIMATE_WORDS,
                math.ceil(_MIN_APPROXIMATE_COVERAGE * len(words)))

    for length in range(len(words), floor - 1, -1):
        for first in range(0, len(words) - length + 1):
            probe = " ".join(words[first:first + length])
            position = normalized_text.find(probe)
            if position == -1:
                continue
            start, end = _span_from_normalized(
                offsets, len(text), position, len(probe))
            return start, end, round(length / len(words), 3)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Pages
# ──────────────────────────────────────────────────────────────────────────────

def has_page_markers(text: str) -> bool:
    """Whether this text was extracted page-by-page.

    False for a .docx and for a text-layer PDF (both arrive as one unmarked
    run), True for a vision-transcribed scan. Callers use it to tell "no page
    information exists" apart from "the value is on page 1".
    """
    return _PAGE_MARKER.search(text) is not None


def page_at(text: str, offset: int) -> int | None:
    """The page number containing ``offset``, or None if the text is unpaged.

    Read off the markers rather than trusted from the model: the page a span
    physically falls under is a fact about the text, where an LLM's reported
    ``source_page`` is a guess whenever the text it read carried no markers.
    """
    page: int | None = None
    for match in _PAGE_MARKER.finditer(text):
        if match.start() > offset:
            break
        page = int(match.group(1))
    return page


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def locate_value(text: str, value: object,
                 core: object = None) -> Span | None:
    """Locate ``value`` (falling back to ``core``) in ``text``.

    Stages run strongest-first, and the two PRECISE fallbacks are both tried
    before the approximate one: a hit on the trimmed ``core`` is a real run of
    document characters, so it beats a partial word window on the full value.
    Returns None when the document does not contain the value in any of these
    senses — a genuine answer meaning "the model reported something this
    document does not say".
    """
    if not text or not isinstance(value, str) or not value.strip():
        return None

    def span(start: int, end: int, found_by: str, confidence: float) -> Span:
        return Span(start=start, end=end, page=page_at(text, start),
                    found_by=found_by, confidence=confidence)

    # Character-for-character, before any normalization — the strongest claim.
    position = text.find(value)
    if position != -1:
        return span(position, position + len(value), FOUND_EXACT, 1.0)

    normalized_text, offsets = _normalize(text)

    hit = _find_normalized(text, normalized_text, offsets, value)
    if hit is not None:
        return span(*hit, FOUND_WHITESPACE, 1.0)

    if isinstance(core, str) and core.strip() and core != value:
        hit = _find_normalized(text, normalized_text, offsets, core)
        if hit is not None:
            return span(*hit, FOUND_CORE, 1.0)

    run = _find_longest_word_run(text, normalized_text, offsets, value)
    if run is None and isinstance(core, str) and core.strip() and core != value:
        run = _find_longest_word_run(text, normalized_text, offsets, core)
    if run is not None:
        start, end, coverage = run
        return span(start, end, FOUND_APPROXIMATE, coverage)

    return None
