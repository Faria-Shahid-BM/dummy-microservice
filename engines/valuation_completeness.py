
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from engines.completeness import FieldGroup, score, shortfall_detail
from engines.extraction import pdf_page_count, text_is_thin

logger = logging.getLogger(__name__)

# One entry per point; any path in a group scores it once. Each of these three
# records the ACT of valuing - not the property, which any property document
# describes.
IDENTITY_FIELDS: tuple[FieldGroup, ...] = (
    # Who valued it. A deed, a challan and an allotment letter name no valuer.
    ("valuation company", ("valuation_company",)),
    # When it was valued - not the date the document was registered or stamped.
    ("valuation date", ("valuation_date",)),
    # What they said it was worth. A valuation states the figures separately,
    # so either one is evidence.
    ("land or building value", ("land_value", "building_value")),
)

# Not in config.py: this decides whether a review happens at all, and a setting
# a deployment can lower to zero is a gate that silently stops working.
MIN_IDENTITY_FIELDS: int = 2

#: While False the score is logged on every review and nothing is rejected, so
#: a threshold can be measured against real reports before it turns one away.
#: Now on: a lease deed scored 0/3 against the list above, and a real report
#: scores 3/3. Set it back to False to re-measure after changing the fields.
ENFORCE_COMPLETENESS: bool = True


class UnreadableReportError(Exception):
    """Neither the text layer nor a transcription produced usable text."""


class InsufficientValuationDetailError(Exception):
    """Too little valuation detail was read to treat this as a valuation report."""


def require_readable_text(text: str, page_count: int, *, name: str = "") -> None:
    """Raise :class:`UnreadableReportError` when nothing readable came back.

    Reached only after ``extract_document`` has already tried the text layer
    and then the vision transcription, so getting here means both failed. A
    blank, rotated or badly degraded scan is the usual cause.
    """
    if not text_is_thin(text, page_count):
        return

    detail = (
        f"Could not read this document. It has no usable text layer and "
        f"transcribing its {page_count} page(s) produced "
        f"{len(text.strip())} characters - the scan may be blank, rotated, or "
        f"too poor to read. Send a clearer scan or a text-based PDF."
    )
    logger.info("Valuation report rejected as unreadable%s: %s",
                f" ({name})" if name else "", detail)
    raise UnreadableReportError(detail)


def page_count_or_reject(path: Path) -> int:
    """The PDF's page count, or :class:`UnreadableReportError`.

    A file that carries a .pdf suffix but that the PDF library cannot open at
    all is a request problem, not a server one - it must not surface as a 500.
    """
    try:
        return pdf_page_count(path)
    except Exception as exc:
        detail = (
            f"Could not open this file as a PDF ({exc.__class__.__name__}). "
            f"It may be corrupt, truncated, or not a PDF despite its name. "
            f"Send a valid PDF."
        )
        logger.info("Valuation report rejected as unopenable: %s", detail)
        raise UnreadableReportError(detail) from exc


def identity_fields(extraction: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """``(found, missing)`` group labels from :data:`IDENTITY_FIELDS`."""
    return score(extraction, IDENTITY_FIELDS)


def check_valuation_document(extraction: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Score the extraction, and reject it only once enforcement is on.

    Returns ``(found, missing)`` either way, so a caller can record the score
    whatever the flag says.
    """
    found, missing = identity_fields(extraction or {})
    total = len(IDENTITY_FIELDS)

    if len(found) >= MIN_IDENTITY_FIELDS:
        logger.info("Valuation completeness: %d/%d (%s)",
                    len(found), total, ", ".join(found))
        return found, missing

    detail = shortfall_detail(
        found, missing, total,
        what="valuation detail",
        advice="Check that the upload is a property valuation report and that "
               "the scan is legible.",
    )
    if not ENFORCE_COMPLETENESS:
        # Log only, on purpose - the threshold has not been measured against
        # real reports yet, so it must not turn one away.
        logger.warning(
            "Valuation completeness WOULD have rejected this document "
            "(enforcement off): %s", detail,
        )
        return found, missing

    logger.info("Valuation completeness check rejected a document: %s", detail)
    raise InsufficientValuationDetailError(detail)
