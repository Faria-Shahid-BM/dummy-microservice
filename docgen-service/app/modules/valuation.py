"""Valuation Reviewer module — single valuation report review.

Thin config over :func:`app.modules.reviews_base.make_review_router`.

Text configuration (cushion percentage, report validity years, extraction
prompt, model roles) lives in the generic per-profile configuration
(``app.control.profile_config`` — the ``/api/profiles/{pid}/config``
endpoints); it is resolved at analysis run time via ``effective_float`` /
``effective_int`` / ``effective_model`` / ``prompt_override``.

The one per-profile file this module still owns is the bank's approved-valuer
Annexure-A panel (a DOCUMENT, not text config):

- ``profile_dir/valuation_panel.xlsx`` — uploaded via POST
  ``.../reviews/valuation/panel``; status via GET ``.../panel/status``. When
  absent the engine falls back to its bundled default panel
  (``panel_path=None``). The uploaded file's display name is kept in
  ``profile_dir/valuation_config.json`` (panel metadata only).

The literal ``/panel`` routes are registered through the factory's ``extend``
hook so they are declared BEFORE the ``/{review_id}`` routes and never
captured as review ids.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app import audit, storage
from app.auth.deps import current_user, require_profile_maker, require_profile_member
from app.control.profile_config import (
    effective_float,
    effective_int,
    effective_model,
    prompt_override,
)
from app.core.db import db_session, session_scope
from app.engines.valuation import review_valuation
from app.llm.registry import get_provider
from app.modules.reviews_base import EmitFn, make_review_router

# Holds ONLY panel metadata now ({"panel_file_name": ...}); cushion/expiry
# moved to the generic profile configuration.
PANEL_META_FILE = "valuation_config.json"
PANEL_FILE = "valuation_panel.xlsx"

_BASE = "/profiles/{profile_id}/reviews/valuation"


# ------------------------------------------------------------- panel storage


def _panel_meta_path(profile_id: str) -> Path:
    return storage.profile_dir(profile_id) / PANEL_META_FILE


def _panel_path(profile_id: str) -> Path:
    return storage.profile_dir(profile_id) / PANEL_FILE


def _read_panel_meta(profile_id: str) -> dict:
    """The stored panel metadata JSON as-is ({} when missing/corrupt)."""
    path = _panel_meta_path(profile_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_panel_meta(profile_id: str, raw: dict) -> None:
    _panel_meta_path(profile_id).write_text(
        json.dumps(raw, indent=2), encoding="utf-8"
    )


# ------------------------------------------------------------------- analysis


def _analyze(profile_id: str, review_id: str, paths: dict[str, Path], emit: EmitFn) -> dict:
    # Per-profile config resolved at run time (this runs inside the job), so
    # the review always uses the profile's CURRENT overrides.
    with session_scope() as jdb:
        cushion_pct = effective_float(jdb, profile_id, "valuation.cushion_pct")
        expiry_years = effective_int(jdb, profile_id, "valuation.expiry_years")
        models = {
            "extraction": effective_model(jdb, profile_id, "extraction"),
            "vision": effective_model(jdb, profile_id, "vision"),
        }
        extraction_prompt = prompt_override(
            jdb, profile_id, "valuation.extraction.prompt")
    panel = _panel_path(profile_id)
    return review_valuation(
        paths["report"],
        panel if panel.exists() else None,  # None -> engine's bundled default panel
        get_provider(),
        models=models,
        cushion_pct=cushion_pct,
        expiry_years=expiry_years,
        prompt=extraction_prompt,
        emit=emit,
    )


# ----------------------------------------------- extra literal routes (extend)


def _extend(router: APIRouter) -> None:
    @router.post(_BASE + "/panel", status_code=201)
    async def upload_panel(
        profile_id: str,
        request: Request,
        file: UploadFile = File(...),
        role: str = Depends(require_profile_maker),
        user=Depends(current_user),
        db: Session = Depends(db_session),
    ) -> dict:
        try:
            await storage.save_upload(file, _panel_path(profile_id), allowed_suffixes={".xlsx"})
        except storage.UploadError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        display = storage.sanitize_display_name(file.filename or PANEL_FILE)
        raw = _read_panel_meta(profile_id)
        raw["panel_file_name"] = display
        _write_panel_meta(profile_id, raw)
        audit.record(db, user, "review.valuation_panel", profile_id=profile_id,
                     subject_type="valuation_panel", subject_id=profile_id,
                     detail={"file_name": display}, request=request)
        db.commit()
        return {"configured": True, "file_name": display}

    @router.get(_BASE + "/panel/status")
    def panel_status(
        profile_id: str,
        role: str = Depends(require_profile_member),
    ) -> dict:
        if not _panel_path(profile_id).exists():
            return {"configured": False, "file_name": None}
        name = _read_panel_meta(profile_id).get("panel_file_name")
        return {"configured": True, "file_name": name if isinstance(name, str) else PANEL_FILE}


router = make_review_router(
    "valuation",
    upload_slots={"report": {".pdf"}},
    analyze=_analyze,
    min_slots_ready=["report"],
    extend=_extend,
)
