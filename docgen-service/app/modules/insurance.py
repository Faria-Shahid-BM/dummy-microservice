"""Insurance Reviewer module — policy PDF compliance review.

Thin config over :func:`app.modules.reviews_base.make_review_router`.
One slot (``policy``, .pdf), analyzed by
:func:`app.engines.insurance.review_insurance`.

Model roles: the engine's ``models["extraction"]`` drives BOTH the structured
extraction and the compliance-analysis LLM stages (see the engine docstring),
so we deliberately pass the ANALYSIS role slug there — this workload is
analysis-grade, not plain extraction. ``models["vision"]`` covers the
scanned-page OCR path inside the shared extractor.
"""
from __future__ import annotations

from pathlib import Path

from app.control.profile_config import effective_model, prompt_override
from app.core.db import session_scope
from app.engines.insurance import review_insurance
from app.llm.registry import get_provider
from app.modules.reviews_base import EmitFn, make_review_router


def _analyze(profile_id: str, review_id: str, paths: dict[str, Path], emit: EmitFn) -> dict:
    # Per-profile config resolved at run time (this runs inside the job).
    with session_scope() as jdb:
        models = {
            # "extraction" intentionally maps to the ANALYSIS model role — the
            # engine uses it for structuring AND compliance analysis.
            "extraction": effective_model(jdb, profile_id, "analysis"),
            "vision": effective_model(jdb, profile_id, "vision"),
        }
        prompts = {
            key: value
            for key, value in (
                ("extraction", prompt_override(
                    jdb, profile_id, "insurance.extraction.prompt")),
                ("analysis", prompt_override(
                    jdb, profile_id, "insurance.analysis.prompt")),
            )
            if value is not None
        }
    return review_insurance(
        paths["policy"],
        get_provider(),
        models=models,
        prompts=prompts or None,
        emit=emit,
    )


router = make_review_router(
    "insurance",
    upload_slots={"policy": {".pdf"}},
    analyze=_analyze,
    min_slots_ready=["policy"],
)
