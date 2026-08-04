"""Document Diff engine (deterministic, no LLM).

Port of the POC ``subsystems/document_reviewer/main.py`` compare logic.
Compares an ORIGINAL document's text against a RETURNED/signed copy's text and
produces a word-level redline diff using ``difflib.SequenceMatcher``
(``autojunk=False``). Text extraction is NOT this engine's job — callers pass
already-extracted plain text (see ``app.engines.extraction``). CRLF/CR line
endings are normalized to LF internally so the diff is not polluted by
platform-specific newline differences (ported invariant).

Tokenization uses ``\\S+|\\s+`` so word/punctuation runs AND whitespace runs are
both preserved; joining the tokens back reconstructs the input text exactly,
which makes the redline ``segments`` lossless.

Return shape of :func:`compare_documents` (unchanged from the legacy engine):

.. code-block:: python

    {
        "identical": bool,        # True when no non-whitespace change was flagged
        "similarity": float,      # SequenceMatcher.ratio() over tokens, 0.0..1.0
        "summary": {
            "insertions": int,
            "deletions": int,
            "replacements": int,
            "changes": int,       # == len(changes)
        },
        "changes": [              # whitespace-only spans are SUPPRESSED here
            {
                "type": "insertion" | "deletion" | "replacement",
                "before": str,    # whitespace-collapsed original span ("" for insertion)
                "after": str,     # whitespace-collapsed returned span ("" for deletion)
                "context": str,   # localized window of surrounding text (see below)
            },
            ...
        ],
        "segments": [             # lossless redline; whitespace-only spans KEPT
            {"op": "equal" | "delete" | "insert", "text": str},
            ...
        ],
    }

Invariants (ported verbatim from the legacy engine):

- ``"".join`` of the ``equal`` + ``delete`` segment texts reconstructs the
  (newline-normalized) original text exactly; ``equal`` + ``insert``
  reconstructs the returned text exactly.
- A ``replace`` opcode emits a ``delete`` segment followed by an ``insert``
  segment.
- Whitespace-only spans (trivial reflow noise, e.g. a run of spaces becoming a
  newline) stay in ``segments`` but are excluded from ``changes``/``summary``;
  consequently ``identical`` is True whenever only whitespace differs.
- ``before``/``after``/``context`` fields are whitespace-collapsed
  (internal runs -> single space, trimmed).
- Deletion context is described against the ORIGINAL text (the removed words no
  longer exist in the returned copy); insertion/replacement context against the
  RETURNED text, so the change list reads naturally.

Known POC bug fixed in this port (ARCHITECTURE.md "Known POC bugs" #1):
the legacy ``_make_context`` collapsed the WHOLE source document and truncated
it to its first 120 characters, so every change's "context" was the document
head. This port computes the change's character span from the token offsets and
returns a real window of ~60 characters on EACH side of the change site within
the relevant text, with "…" marking truncated ends.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

# Tokenize into a list preserving words/punct runs AND whitespace runs, so the
# redline reconstructs the original text exactly when joined back together.
_TOKEN_RE = re.compile(r"\S+|\s+")

# Bug #1 fix: characters of surrounding text kept on EACH side of a change
# site for its "context" line (the POC truncated the whole document to its
# first 120 chars instead).
_CONTEXT_WINDOW_CHARS = 60


def _normalize_newlines(text: str) -> str:
    """Normalize CRLF/CR line endings to LF (ported from legacy extract_text)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _collapse_ws(text: str) -> str:
    """Collapse internal whitespace runs to single spaces and trim, for
    readable before/after/context fields."""
    return re.sub(r"\s+", " ", text).strip()


def _token_offsets(tokens: list[str]) -> list[int]:
    """Prefix character offsets for a token list.

    ``offsets[k]`` is the character position where ``tokens[k]`` starts in the
    joined text; ``offsets[len(tokens)]`` is the total text length. Valid
    because the tokenization is lossless (tokens join back to the exact text).
    """
    offsets = [0]
    total = 0
    for token in tokens:
        total += len(token)
        offsets.append(total)
    return offsets


def _make_context(source_text: str, span_start: int, span_end: int) -> str:
    """Build a localized context window around a change site.

    Returns ~``_CONTEXT_WINDOW_CHARS`` characters on each side of the
    ``[span_start:span_end)`` character span within ``source_text`` (the change
    span itself included), whitespace-collapsed, with "…" marking ends that
    were truncated. This is the fixed version of the POC helper that truncated
    the whole document to 120 chars (ARCHITECTURE.md known bug #1).
    """
    window_start = max(0, span_start - _CONTEXT_WINDOW_CHARS)
    window_end = min(len(source_text), span_end + _CONTEXT_WINDOW_CHARS)
    window = _collapse_ws(source_text[window_start:window_end])
    prefix = "…" if window_start > 0 else ""
    suffix = "…" if window_end < len(source_text) else ""
    return prefix + window + suffix


def compare_documents(original_text: str, returned_text: str) -> dict[str, Any]:
    """Compare two documents' plain text and return the diff dict.

    Whitespace-only spans are kept in ``segments`` (so the redline
    reconstructs exactly) but are NOT flagged in ``changes``/``summary`` —
    trivial reflow noise is suppressed. See the module docstring for the full
    return shape and invariants.

    Args:
        original_text: Extracted plain text of the original document.
        returned_text: Extracted plain text of the returned/signed copy.

    Returns:
        ``{"identical", "similarity", "summary", "changes", "segments"}`` —
        the exact legacy shape.
    """
    text_a = _normalize_newlines(original_text)
    text_b = _normalize_newlines(returned_text)

    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)

    offsets_a = _token_offsets(tokens_a)
    offsets_b = _token_offsets(tokens_b)

    sm = difflib.SequenceMatcher(a=tokens_a, b=tokens_b, autojunk=False)
    similarity = sm.ratio()

    segments: list[dict[str, str]] = []
    changes: list[dict[str, str]] = []
    insertions = 0
    deletions = 0
    replacements = 0

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        a_text = "".join(tokens_a[i1:i2])
        b_text = "".join(tokens_b[j1:j2])

        if op == "equal":
            segments.append({"op": "equal", "text": a_text})
            continue

        # Emit redline segments (always, regardless of whitespace-only status).
        if op == "replace":
            segments.append({"op": "delete", "text": a_text})
            segments.append({"op": "insert", "text": b_text})
        elif op == "delete":
            segments.append({"op": "delete", "text": a_text})
        elif op == "insert":
            segments.append({"op": "insert", "text": b_text})

        before = _collapse_ws(a_text)
        after = _collapse_ws(b_text)

        # Skip whitespace-only changes from the flagged "changes" list so
        # trivial reflow noise (e.g. a run of spaces becoming a newline) is not
        # reported. They remain in `segments` above.
        if not before and not after:
            continue

        if op == "replace":
            change_type = "replacement"
            replacements += 1
        elif op == "delete":
            change_type = "deletion"
            deletions += 1
        else:  # insert
            change_type = "insertion"
            insertions += 1

        # Pure deletions are windowed against the ORIGINAL text (the removed
        # words no longer exist in the returned copy); everything else against
        # the RETURNED text so the list reads naturally.
        if change_type == "deletion":
            context = _make_context(text_a, offsets_a[i1], offsets_a[i2])
        else:
            context = _make_context(text_b, offsets_b[j1], offsets_b[j2])

        changes.append(
            {
                "type": change_type,
                "before": before,
                "after": after,
                "context": context,
            }
        )

    summary = {
        "insertions": insertions,
        "deletions": deletions,
        "replacements": replacements,
        "changes": len(changes),
    }

    return {
        "identical": len(changes) == 0,
        "similarity": similarity,
        "summary": summary,
        "changes": changes,
        "segments": segments,
    }
