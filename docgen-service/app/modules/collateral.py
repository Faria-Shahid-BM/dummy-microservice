"""Collateral Reviewer module — legal opinion vs property/title cross-check.

Thin config over :func:`app.modules.reviews_base.make_review_router`.
Two slots (``legal`` / ``property``, .pdf or .docx, both required), analyzed by
:func:`app.engines.collateral.review_collateral` (scanned-PDF vision-OCR
fallback included via the shared extractor).
"""
from __future__ import annotations

from pathlib import Path

from app.control.profile_config import effective_model, prompt_override
from app.core.db import session_scope
from app.engines.collateral import review_collateral
from app.llm.registry import get_provider
from app.modules.reviews_base import EmitFn, make_review_router

_SLOT_SUFFIXES = {".pdf", ".docx"}


def _analyze(profile_id: str, review_id: str, paths: dict[str, Path], emit: EmitFn) -> dict:
    # Per-profile config resolved at run time (this runs inside the job).
    with session_scope() as jdb:
        models = {
            "extraction": effective_model(jdb, profile_id, "extraction"),
            "vision": effective_model(jdb, profile_id, "vision"),
        }
        prompts = {
            key: value
            for key, value in (
                ("extraction", prompt_override(
                    jdb, profile_id, "collateral.extraction.prompt")),
                ("observations", prompt_override(
                    jdb, profile_id, "collateral.observations.prompt")),
            )
            if value is not None
        }
    return review_collateral(
        paths["legal"],
        paths["property"],
        get_provider(),
        models=models,
        prompts=prompts or None,
        emit=emit,
    )


router = make_review_router(
    "collateral",
    upload_slots={"legal": _SLOT_SUFFIXES, "property": _SLOT_SUFFIXES},
    analyze=_analyze,
    min_slots_ready=["legal", "property"],
)
