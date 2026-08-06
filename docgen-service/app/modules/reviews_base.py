"""Reviewer-module router factory — ONE parameterized pattern for all four
reviewer modules (document / collateral / valuation / insurance).

The POC implemented four near-identical routers (~76% copy-paste). Here each
module is a thin config over :func:`make_review_router`: it names its upload
slots (slot -> allowed suffixes), the minimum slots required before analysis
may start, and an ``analyze(profile_id, review_id, paths, emit) -> dict``
callable that runs inside a job and returns the result JSON persisted on the
``Review`` row.

Storage layout: each upload lives at ``review_dir/{slot}{original suffix}``
(one file per slot — re-uploading replaces it, including across suffixes);
slot files are located by glob ``{slot}.*``. Slot names are code-defined
literals, never user input.

Status machine: ``new`` -> ``ready`` (all ``min_slots_ready`` uploaded) ->
``analyzing`` -> ``done`` | ``failed``; ``done``/``failed`` may be
re-analyzed. On failure the error text is kept in ``Review.result`` as
``{"error": ...}`` (surfaced via the detail payload; the full traceback-free
message also lands on the job row).

Guards: any profile member may view; only makers mutate.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, storage
from app.auth.deps import current_user, require_profile_maker, require_profile_member
from app.core.db import db_session, session_scope
from app.jobs.runner import JobConflict, runner
from app.models import REVIEW_MODULES, Review, User

# emit(type, text) with type in {"reasoning", "content", "event"} (job runner).
EmitFn = Callable[[str, str], None]

# analyze(profile_id, review_id, paths, emit) -> result dict; runs inside a
# job worker thread. ``paths`` maps every uploaded slot to its file on disk
# (all ``min_slots_ready`` slots guaranteed present).
AnalyzeFn = Callable[[str, str, dict[str, Path], EmitFn], dict]

# Statuses from which POST /analyze is allowed (re-analysis included).
ANALYZABLE_STATUSES = ("ready", "done", "failed")


class ReviewCreateBody(BaseModel):
    name: str


def _normalize_suffixes(suffixes: set[str]) -> set[str]:
    return {s.lower() if s.startswith(".") else f".{s.lower()}" for s in suffixes}


def _slot_file(profile_id: str, review_id: str, slot: str) -> Path | None:
    """The stored file for a slot (``{slot}.*`` in the review dir), if any."""
    review_dir = storage.review_dir(profile_id, review_id)
    return next((p for p in sorted(review_dir.glob(f"{slot}.*")) if p.is_file()), None)


def _get_review(db: Session, profile_id: str, module: str, review_id: str) -> Review:
    r = db.get(Review, review_id)
    if r is None or r.profile_id != profile_id or r.module != module:
        raise HTTPException(status_code=404, detail="Review not found")
    return r


def _list_payload(r: Review) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "status": r.status,
        "uploads": r.uploads or {},
        "created_at": r.created_at.isoformat(),
    }


def _detail_payload(r: Review) -> dict:
    out = _list_payload(r)
    out["module"] = r.module
    out["result"] = r.result if r.status == "done" else None
    out["error"] = (r.result or {}).get("error") if r.status == "failed" else None
    return out


def make_review_router(
    module: str,
    *,
    upload_slots: dict[str, set[str]],
    analyze: AnalyzeFn,
    min_slots_ready: list[str],
    extend: Callable[[APIRouter], None] | None = None,
) -> APIRouter:
    """Build the full ``/api/profiles/{profile_id}/reviews/{module}`` router.

    Args:
        module: One of :data:`app.models.REVIEW_MODULES` — becomes the literal
            path segment and the ``Review.module`` discriminator.
        upload_slots: slot name -> allowed file suffixes for that slot.
        analyze: Job-side callable producing the result dict (see AnalyzeFn).
        min_slots_ready: Slots that must be uploaded before analysis; once all
            are present the review status becomes "ready".
        extend: Optional hook receiving the router BEFORE the ``/{review_id}``
            routes are registered — lets a module add literal sibling paths
            (e.g. valuation's ``/config`` and ``/panel``) that would otherwise
            be captured by the ``/{review_id}`` parameter.
    """
    if module not in REVIEW_MODULES:
        raise ValueError(f"unknown review module {module!r}; expected one of {REVIEW_MODULES}")
    unknown = set(min_slots_ready) - set(upload_slots)
    if unknown:
        raise ValueError(f"min_slots_ready names unknown slot(s): {sorted(unknown)}")

    slots = {slot: _normalize_suffixes(suffixes) for slot, suffixes in upload_slots.items()}
    base = f"/profiles/{{profile_id}}/reviews/{module}"
    router = APIRouter(prefix="/api", tags=[f"reviews:{module}"])

    # ------------------------------------------------------------ list/create

    @router.get(base)
    def list_reviews(
        profile_id: str,
        role: str = Depends(require_profile_member),
        db: Session = Depends(db_session),
    ) -> dict:
        rows = db.execute(
            select(Review)
            .where(Review.profile_id == profile_id, Review.module == module)
            .order_by(Review.created_at.desc())
        ).scalars().all()
        return {"reviews": [_list_payload(r) for r in rows]}

    @router.post(base, status_code=201)
    def create_review(
        profile_id: str,
        body: ReviewCreateBody,
        request: Request,
        role: str = Depends(require_profile_maker),
        user: User = Depends(current_user),
        db: Session = Depends(db_session),
    ) -> dict:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="A review name is required")
        r = Review(
            profile_id=profile_id, module=module, name=name,
            status="new", uploads={}, created_by=user.id,
        )
        db.add(r)
        db.flush()
        audit.record(db, user, "review.create", profile_id=profile_id,
                     subject_type="review", subject_id=r.id,
                     detail={"module": module, "name": name}, request=request)
        db.commit()
        return _detail_payload(r)

    # Literal sibling paths (e.g. valuation /config, /panel) must be declared
    # before the /{review_id} routes so they are not captured as review ids.
    if extend is not None:
        extend(router)

    # ---------------------------------------------------------- detail/delete

    @router.get(base + "/{review_id}")
    def get_review(
        profile_id: str,
        review_id: str,
        role: str = Depends(require_profile_member),
        db: Session = Depends(db_session),
    ) -> dict:
        return _detail_payload(_get_review(db, profile_id, module, review_id))

    @router.delete(base + "/{review_id}")
    def delete_review(
        profile_id: str,
        review_id: str,
        request: Request,
        role: str = Depends(require_profile_maker),
        user: User = Depends(current_user),
        db: Session = Depends(db_session),
    ) -> dict:
        r = _get_review(db, profile_id, module, review_id)
        if r.status == "analyzing":
            raise HTTPException(status_code=409, detail="Analysis is running; wait for it to finish")
        storage.delete_tree(storage.review_dir(profile_id, review_id))
        db.delete(r)
        audit.record(db, user, "review.delete", profile_id=profile_id,
                     subject_type="review", subject_id=review_id,
                     detail={"module": module, "name": r.name}, request=request)
        db.commit()
        return {"ok": True}

    # ---------------------------------------------------------------- uploads

    @router.post(base + "/{review_id}/uploads/{slot}")
    async def upload_slot(
        profile_id: str,
        review_id: str,
        slot: str,
        request: Request,
        file: UploadFile = File(...),
        role: str = Depends(require_profile_maker),
        user: User = Depends(current_user),
        db: Session = Depends(db_session),
    ) -> dict:
        r = _get_review(db, profile_id, module, review_id)
        if slot not in slots:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown upload slot '{slot}'; expected one of: {', '.join(sorted(slots))}",
            )
        if r.status == "analyzing":
            raise HTTPException(
                status_code=409, detail="Analysis is running; wait for it to finish before replacing files"
            )
        allowed = slots[slot]
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"File type {suffix or '(none)'!r} is not allowed for slot '{slot}'; "
                       f"expected one of: {', '.join(sorted(allowed))}",
            )
        review_dir = storage.review_dir(profile_id, review_id)
        dest = review_dir / f"{slot}{suffix}"
        try:
            await storage.save_upload(file, dest, allowed_suffixes=allowed)
        except storage.UploadError as exc:
            # A failed overwrite deletes the partial file — if that lost the
            # slot's previous file, drop the stale uploads entry too.
            if (r.uploads or {}).get(slot) and _slot_file(profile_id, review_id, slot) is None:
                uploads = dict(r.uploads or {})
                uploads.pop(slot, None)
                r.uploads = uploads
                if r.status == "ready" and not all(s in uploads for s in min_slots_ready):
                    r.status = "new"
                db.commit()
            raise HTTPException(status_code=422, detail=str(exc))

        # Replace semantics: one file per slot — remove other-suffix leftovers.
        for stale in review_dir.glob(f"{slot}.*"):
            if stale != dest and stale.is_file():
                stale.unlink(missing_ok=True)

        display = storage.sanitize_display_name(file.filename or dest.name)
        uploads = dict(r.uploads or {})
        uploads[slot] = display
        r.uploads = uploads
        if all(s in uploads for s in min_slots_ready):
            r.status = "ready"
        audit.record(db, user, "review.upload", profile_id=profile_id,
                     subject_type="review", subject_id=review_id,
                     detail={"module": module, "slot": slot, "file_name": display},
                     request=request)
        db.commit()
        return _detail_payload(r)

    # ---------------------------------------------------------------- analyze

    @router.post(base + "/{review_id}/analyze")
    def start_analyze(
        profile_id: str,
        review_id: str,
        request: Request,
        role: str = Depends(require_profile_maker),
        user: User = Depends(current_user),
        db: Session = Depends(db_session),
    ) -> dict:
        r = _get_review(db, profile_id, module, review_id)
        if r.status not in ANALYZABLE_STATUSES:
            hint = (
                f"; upload {', '.join(min_slots_ready)} first"
                if r.status == "new"
                else ""
            )
            raise HTTPException(
                status_code=409, detail=f"Cannot analyze from status '{r.status}'{hint}"
            )
        paths: dict[str, Path] = {}
        for s in slots:
            p = _slot_file(profile_id, review_id, s)
            if p is not None:
                paths[s] = p
        missing = [s for s in min_slots_ready if s not in paths]
        if missing:
            raise HTTPException(
                status_code=409, detail=f"Missing required upload(s): {', '.join(missing)}"
            )

        def run(emit: EmitFn) -> dict:
            with session_scope() as jdb:
                row = jdb.get(Review, review_id)
                if row is None:
                    raise RuntimeError("Review was deleted before analysis started")
                row.status = "analyzing"
            try:
                result = analyze(profile_id, review_id, paths, emit)
            except Exception as exc:
                with session_scope() as jdb:
                    row = jdb.get(Review, review_id)
                    if row is not None:
                        row.status = "failed"
                        row.result = {"error": f"{type(exc).__name__}: {exc}"}
                raise  # the job row records the error and streams it to subscribers
            if not isinstance(result, dict):
                raise TypeError(f"analyze() must return a dict, got {type(result).__name__}")
            with session_scope() as jdb:
                row = jdb.get(Review, review_id)
                if row is not None:
                    row.status = "done"
                    row.result = result
            summary = result.get("summary")
            return {
                "review_id": review_id,
                "module": module,
                "summary": summary if isinstance(summary, dict) else None,
            }

        try:
            job = runner.submit(
                f"review.{module}",
                f"review-analyze:{review_id}",
                run,
                profile_id=profile_id,
                subject_id=review_id,
                user_id=user.id,
            )
        except JobConflict:
            raise HTTPException(status_code=409, detail="Analysis is already running for this review")
        audit.record(db, user, "review.analyze", profile_id=profile_id,
                     subject_type="review", subject_id=review_id,
                     detail={"module": module}, request=request)
        db.commit()
        return job

    # ----------------------------------------------------------------- result

    @router.get(base + "/{review_id}/result")
    def get_result(
        profile_id: str,
        review_id: str,
        role: str = Depends(require_profile_member),
        db: Session = Depends(db_session),
    ) -> dict:
        r = _get_review(db, profile_id, module, review_id)
        if r.status != "done" or r.result is None:
            raise HTTPException(status_code=404, detail="Result not available until analysis completes")
        return r.result

    return router
