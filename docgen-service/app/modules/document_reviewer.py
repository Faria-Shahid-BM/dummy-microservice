"""Document Reviewer module — original vs returned/signed copy redline.

Thin config over :func:`app.modules.reviews_base.make_review_router`.
Two slots (``original`` / ``returned``, .docx or .pdf, both required), then a
deterministic word-level diff via :func:`app.engines.document_diff.compare_documents`.

Text-extractable inputs ONLY (matching POC 5.1): extraction uses
``engines.extraction.extract_text`` — no vision OCR. A scanned/image-only PDF
fails the job with a clear message telling the user this module needs text
documents.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.engines import document_diff, extraction
from app.modules.reviews_base import EmitFn, make_review_router

_SLOT_SUFFIXES = {".docx", ".pdf"}


def _emit_event(emit: EmitFn, payload: dict) -> None:
    emit("event", json.dumps(payload, separators=(",", ":")))


def _read_text_document(slot: str, path: Path) -> str:
    """Extract plain text; reject scanned PDFs with an actionable error."""
    if path.suffix.lower() == ".pdf" and extraction.is_scanned_pdf(path):
        raise ValueError(
            f"The '{slot}' file appears to be a scanned/image-only PDF with no usable "
            "text layer. The Document Reviewer compares text documents only — upload a "
            "text-based PDF or a .docx version of this document."
        )
    text = extraction.extract_text(path)
    if not text.strip():
        raise ValueError(
            f"No text could be extracted from the '{slot}' file. The Document Reviewer "
            "needs text documents (.docx or text-based .pdf)."
        )
    return text


def _analyze(profile_id: str, review_id: str, paths: dict[str, Path], emit: EmitFn) -> dict:
    _emit_event(emit, {"stage": "extract_text", "document": "original"})
    original_text = _read_text_document("original", paths["original"])
    _emit_event(emit, {"stage": "extract_text", "document": "returned"})
    returned_text = _read_text_document("returned", paths["returned"])
    _emit_event(emit, {"stage": "compare"})
    result = document_diff.compare_documents(original_text, returned_text)
    _emit_event(emit, {"stage": "done", "changes": result["summary"]["changes"]})
    return result


router = make_review_router(
    "document",
    upload_slots={"original": _SLOT_SUFFIXES, "returned": _SLOT_SUFFIXES},
    analyze=_analyze,
    min_slots_ready=["original", "returned"],
)
