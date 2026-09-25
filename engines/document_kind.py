"""Is this file the kind of document the caller said it was?

The collateral endpoint takes two uploads on two form fields, ``legal`` and
``property``. Nothing about a file proves which is which: ``save_upload`` keeps
only the suffix and writes it as ``legal.pdf``, so *the slot it was posted on is
the entire claim*. This module is what tests that claim.

Without it, a property deed posted as the legal opinion is compared against the
property document — very often the same instrument — and every field matches. A
mis-filed upload comes back as the cleanest report the service can produce.
That is a false pass, and a silent one.

Two layers, cheap first:

1. **Signatures.** A short list of phrases that settle the question on sight —
   ``LEGAL OPINION``, the bank's ``LEGAL DEPARTMENT ("LGD")`` heading, "we are
   of the opinion". Present means the document *is* an opinion: accept it on the
   ``legal`` slot, reject it on ``property``. Both free.

2. **A blind classification call**, on everything the signatures cannot settle.
   It is a separate call on purpose: ``collateral_extraction.md`` opens with
   "The document type is: {document_name}", and a model asked to classify a
   document it has just been told the identity of will agree. This prompt is
   never told what was expected.

**Why signatures and not counting.** An earlier version counted markers per kind
and accepted whichever dominated. It shipped a bug: a real legal opinion is
mostly a *recital of the title documents it examines*, so it is dense with deed
vocabulary — one specimen scored 2 opinion phrases against 21 deed phrases — and
the count waved it straight through the ``property`` slot without ever asking
the model. The text did contain ``legal opinion``; counting reduced a decisive
phrase to one point and let twenty-one generic ones outvote it.

The reason is that containment runs **one way only**:

* Opinion vocabulary appears only in opinions. Trustworthy.
* Deed vocabulary appears in *both*, because describing the deed is what an
  opinion does. Evidence of nothing.

So there is no property-document signature list, and that asymmetry is the
point: every deed phrase can legitimately appear inside an opinion's recital, so
none can ever be decisive. Absence of a legal signature proves nothing, which is
why an unsignatured document always goes to the model. Do not add a deed
signature list, and do not reintroduce acceptance by count.

``marker_scores`` survives only so ``tools/score_document_kind.py`` can show
what a document looks like when auditing the gate. Nothing in the review path
calls it, and it decides nothing.

**What this does not do.** It catches mis-filed uploads, which is the common and
real failure. It is not a control against someone who wants a file to pass: a
signature is a phrase present in the text, so the right wording placed anywhere
in a PDF moves the verdict without an LLM call ever running. Do not cite this as
validation of document authenticity.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from functools import lru_cache
from pathlib import Path

from engines.token_usage import call_with_usage, new_usage
from engines.util import EngineParseError, parse_json_response

if TYPE_CHECKING:  # typing only — the engine never imports the app at runtime
    from app.llm.base import LLMProvider

logger = logging.getLogger(__name__)

LEGAL_OPINION = "legal_opinion"
PROPERTY_DOCUMENT = "property_document"
OTHER = "other"

KINDS = (LEGAL_OPINION, PROPERTY_DOCUMENT, OTHER)

# Human wording for rejection messages and warnings.
KIND_LABELS: dict[str, str] = {
    LEGAL_OPINION: "a legal opinion",
    PROPERTY_DOCUMENT: "a property/title document",
    OTHER: "neither a legal opinion nor a property document",
}


# ──────────────────────────────────────────────────────────────────────────────
# Layer 1 — signatures
#
# Phrases that identify a legal opinion ON SIGHT. One is enough; they are not
# counted and nothing outvotes them.
#
# The bar is certainty, not likelihood: a phrase belongs here only if a title
# instrument cannot contain it. That rules out "advocate", "counsel" and
# "witness" — a deed may be drafted before, or attested by, an advocate — so
# those stay below as ordinary markers.
#
# There is deliberately NO property-document equivalent. See the module
# docstring: deed vocabulary appears inside opinions, so none of it is decisive.
# ──────────────────────────────────────────────────────────────────────────────

LEGAL_OPINION_SIGNATURES: tuple[str, ...] = (
    r"legal opinion",
    r"legal department",       # the bank's LGD letterhead
    r"\blgd\b",
    r"we are of the opinion",
    r"in our opinion",
    r"our considered opinion",
    r"opinion is hereby",
    r"clear and marketable",
)


# ──────────────────────────────────────────────────────────────────────────────
# Markers — no longer decide anything
#
# Retained solely so tools/score_document_kind.py can show what a document looks
# like when auditing the gate. Nothing in the review path reads them. Phrases,
# not single words: "opinion" alone appears in a deed's recitals, "we are of the
# opinion" does not.
# ──────────────────────────────────────────────────────────────────────────────

# What only an opinion says: it is authored by a lawyer and it JUDGES a title.
LEGAL_OPINION_MARKERS: tuple[str, ...] = (
    r"legal opinion",
    r"we are of the opinion",
    r"in our opinion",
    r"our considered opinion",
    r"opinion is hereby",
    r"advocates?\b",
    r"barrister",
    r"counsel for",
    r"law associates",
    r"law chambers",
    r"advocate high court",
    r"we have examined",
    r"we have perused",
    r"on perusal of",
    r"scrutiny of the (?:title|documents?|record)",
    r"clear and marketable",
    r"marketable title",
    r"searched the record",
    r"search (?:was|has been) (?:carried out|conducted)",
    r"enforceable against the mortgagor",
    r"equitable mortgage (?:can|may) be created",
    r"we hereby certify",
    r"subject to the (?:above|foregoing)",
    r"to the manager",
)

# What only the instrument itself has: execution, registration, stamping.
TITLE_DOCUMENT_MARKERS: tuple[str, ...] = (
    r"sub[- ]registrar",
    r"registrar of (?:properties|assurances)",
    r"book no",
    r"volume no",
    r"stamp duty",
    r"stamp paper",
    r"registration fee",
    r"sale deed",
    r"conveyance deed",
    r"transfer deed",
    r"gift deed",
    r"lease deed",
    r"allotment (?:order|letter)",
    r"vendors?\b",
    r"vendees?\b",
    r"lessor",
    r"lessee",
    r"hereby (?:sells?|transfers?|conveys?|grants?)",
    r"executants?\b",
    r"in favou?r of the (?:vendee|purchaser|lessee)",
    r"witness(?:es|eth)?\b",
    r"mutation",
    r"khasra",
    r"khewat",
    r"khatooni",
    r"\bdeh\b",
    r"survey sheet",
    r"possession (?:was|has been) (?:handed|delivered)",
)

MARKERS: dict[str, tuple[str, ...]] = {
    LEGAL_OPINION: LEGAL_OPINION_MARKERS,
    PROPERTY_DOCUMENT: TITLE_DOCUMENT_MARKERS,
}


# ──────────────────────────────────────────────────────────────────────────────
# Thresholds
#
# These decide whether a review happens at all, so they live here rather than in
# config.py — the same reason TEXT_DENSITY_THRESHOLD does. Exposing them would
# let a deployment weaken the gate to nothing, at which point every document
# clears it and no part of the response says the check stopped working.
# ──────────────────────────────────────────────────────────────────────────────

# Below this, a disagreeing classification is treated as undetermined rather
# than as grounds to reject — a shaky call must not fail a valid document.
# The same 0.7 the adjudicator uses for the same kind of judgement.
REJECT_MIN_CONFIDENCE: float = 0.7

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@lru_cache(maxsize=None)
def _classification_prompt() -> str:
    """Loaded lazily, matching the other engines here — importing this module
    must not depend on the prompt file being readable."""
    return (_PROMPTS_DIR / "document_kind.md").read_text(encoding="utf-8")


class DocumentKindError(Exception):
    """The upload is definitely not the kind of document its slot claimed."""


@dataclass
class KindCheck:
    """The gate's verdict on one document.

    ``ok`` false means reject, with ``detail`` as the reason. ``warning`` is set
    when the gate could not reach a verdict — the review goes ahead, but the
    caller is told the check did not run rather than left to assume it passed.
    """

    ok: bool
    basis: str
    detail: str = ""
    warning: str = ""
    #: What the classification call cost; zeros when a signature settled it for
    #: free, which is the point of having the signature layer at all.
    usage: dict[str, int] = field(default_factory=new_usage)


def _normalize(text: str) -> str:
    """Lower-case, collapse whitespace, drop the punctuation OCR varies on."""
    text = text.lower()
    for dash in ("‐", "‑", "–", "—"):
        text = text.replace(dash, "-")
    text = re.sub(r"[^\w\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def count_markers(text: str, markers: Iterable[str]) -> int:
    """How many DISTINCT markers appear — a phrase repeated ten times counts once."""
    normalized = _normalize(text)
    return sum(1 for marker in markers if re.search(rf"\b{marker}", normalized))


def marker_scores(text: str) -> dict[str, int]:
    """Distinct marker hits per kind."""
    return {kind: count_markers(text, markers) for kind, markers in MARKERS.items()}


def _display(pattern: str) -> str:
    """A signature's words without its regex syntax, for quoting in a message."""
    return re.sub(r"\\b", "", pattern)


def legal_signatures_in(text: str) -> list[str]:
    """Which legal-opinion signatures the text carries, if any.

    One is enough to settle the question. Returned as a list rather than a bool
    so a rejection can quote what it saw.
    """
    normalized = _normalize(text)
    return [
        signature for signature in LEGAL_OPINION_SIGNATURES
        if re.search(rf"\b{signature}", normalized)
    ]


def build_classification_prompt(text: str) -> str:
    """The blind prompt: the sample, and no hint of what was expected.

    Appended rather than substituted — the prompt carries no placeholder, so
    document content cannot land anywhere but at the end.
    """
    return f"{_classification_prompt()}\n\n{text}"


def classify_blind(
    text: str,
    provider: "LLMProvider",
    model: str,
) -> tuple[str | None, float, dict[str, int]]:
    """Ask what kind of document this is, without saying what was expected.

    Returns ``(kind, confidence, usage)``, or ``(None, 0.0, zeros)`` if the call
    failed or came back unusable — which the caller turns into a warning, not a
    rejection. Usage is reported so this gate shows up in the admin token view
    like every other call the review makes.
    """
    response, usage = call_with_usage(
        provider, model,
        [{"role": "user", "content": build_classification_prompt(text)}])
    if response is None:
        logger.warning("document-kind classification call failed")
        return None, 0.0, usage

    try:
        parsed: Any = parse_json_response(response)
    except EngineParseError:
        logger.warning("document-kind classification returned unparseable JSON")
        return None, 0.0, usage

    if not isinstance(parsed, dict):
        return None, 0.0, usage

    kind = parsed.get("document_kind")
    if kind not in KINDS:
        return None, 0.0, usage

    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return kind, max(0.0, min(1.0, confidence)), usage


def verify_document_kind(
    text: str,
    expected: str,
    provider: "LLMProvider",
    model: str,
) -> KindCheck:
    """Check one document's sample text against the slot it was uploaded on.

    ``expected`` is ``"legal_opinion"`` or ``"property_document"``. Returns a
    :class:`KindCheck`; the caller raises :class:`DocumentKindError` on
    ``ok=False``.

    A legal-opinion signature settles it for free, either way: accepted on the
    ``legal`` slot, rejected on ``property``. Everything else asks the model,
    because no signature proves nothing. An empty sample, a failed call and a
    low-confidence disagreement all pass with a warning — the review runs, and
    says it could not tell.
    """
    slot = KIND_LABELS[expected]

    if not text.strip():
        return KindCheck(
            ok=True,
            basis="undetermined",
            warning=f"No readable text in the first pages of the upload sent as "
                    f"{slot}, so the document-kind check could not run.",
        )

    # A signature is decisive both ways, and costs nothing.
    signatures = legal_signatures_in(text)
    if signatures:
        if expected == LEGAL_OPINION:
            return KindCheck(ok=True, basis="signature")
        quoted = ", ".join(f'"{s}"' for s in map(_display, signatures[:3]))
        return KindCheck(
            ok=False,
            basis="signature",
            detail=f"The file uploaded as {slot} is a legal opinion - it reads "
                   f"{quoted}. Check that the right file was sent on each field.",
        )

    # No signature proves nothing on its own, so everything else asks the model.
    kind, confidence, usage = classify_blind(text, provider, model)

    if kind is None:
        return KindCheck(
            ok=True,
            basis="undetermined",
            warning=f"The document-kind check on the upload sent as {slot} could "
                    f"not be completed, so it was not verified.",
            usage=usage,
        )

    if kind == expected:
        return KindCheck(ok=True, basis="model", usage=usage)

    if confidence < REJECT_MIN_CONFIDENCE:
        return KindCheck(
            ok=True,
            basis="undetermined",
            warning=f"The upload sent as {slot} reads more like "
                    f"{KIND_LABELS[kind]}, but not clearly enough to reject it "
                    f"(confidence {confidence:.2f}). Worth a check.",
            usage=usage,
        )

    return KindCheck(
        ok=False,
        basis="model",
        detail=f"The file uploaded as {slot} appears to be {KIND_LABELS[kind]} "
               f"(confidence {confidence:.2f}). Check that the right file was "
               f"sent on each field.",
        usage=usage,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Near-duplicate guard
#
# The kind check asks what each document IS. This asks a blunter question that
# targets the harm directly: are the two uploads the same instrument?
#
# That is the failure the gate exists for — the same deed sent on both fields
# compares against itself, every field matches, and the review reads as a clean
# pass. Classification reaches that conclusion indirectly and can be wrong about
# it; two property documents carry no legal signature, so both fall through to
# the model, which may label one plausibly enough to let the pair through.
# Identical text cannot be explained away.
#
# Deliberately free: shingles over normalized text, no model and no embeddings,
# so it costs nothing and does not care what language the documents are in.
# ──────────────────────────────────────────────────────────────────────────────

#: Word-count of each shingle. Long enough that shared boilerplate (a registry
#: address, a standard covenant) does not register as overlap, short enough to
#: survive the odd OCR error inside an otherwise identical page.
_SHINGLE_WORDS = 8

#: Jaccard overlap above which two uploads are treated as the same document.
#: A legal opinion recites its deed at length, so genuine pairs DO overlap —
#: measured pairs sit far below this. The bar is "substantially the same text",
#: not "related text", which is why it is this high.
DUPLICATE_THRESHOLD: float = 0.80

#: Below this many shingles a document is too short for the ratio to mean
#: anything, and the check abstains rather than guessing.
_MIN_SHINGLES = 40


def _shingles(text: str) -> set[str]:
    words = _normalize(text).split()
    if len(words) < _SHINGLE_WORDS:
        return set()
    return {
        " ".join(words[i:i + _SHINGLE_WORDS])
        for i in range(len(words) - _SHINGLE_WORDS + 1)
    }


def duplicate_ratio(left: str, right: str) -> float | None:
    """Jaccard overlap of the two texts' shingles, or None if too short to judge."""
    a, b = _shingles(left), _shingles(right)
    if len(a) < _MIN_SHINGLES or len(b) < _MIN_SHINGLES:
        return None
    return len(a & b) / len(a | b)


def check_not_duplicate(legal_text: str, property_text: str) -> KindCheck:
    """Reject a pair whose two uploads are substantially the same document.

    Abstains (passes, with no warning) when either document is too short to
    measure — an unreadable scan is the kind check's problem, not this one's.
    """
    ratio = duplicate_ratio(legal_text, property_text)
    if ratio is None or ratio < DUPLICATE_THRESHOLD:
        return KindCheck(ok=True, basis="distinct")
    return KindCheck(
        ok=False,
        basis="duplicate",
        detail=f"The two uploads are {ratio:.0%} identical, so they appear to be "
               f"the same document sent on both fields. Comparing a document "
               f"against itself would match on every field. Check that the "
               f"legal opinion and the property document are different files.",
    )
