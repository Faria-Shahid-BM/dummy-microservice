"""case_store.py — shared persisted "Cases" backend for the stateless review
services (collateral-service, valuation-service, insurance-service,
doc_rev-service).

Modeled on docgen-service's own lighter "Review" pattern
(docgen-service/app/modules/reviews_base.py) rather than its multi-stage
`Case` pipeline (extract/analyze/select/fill) — these services each do one
upload-and-analyze pass, not a multi-stage pipeline, so the simpler
new -> ready -> analyzing -> done|failed machine is the right fit. Adapted
from that reference to this project's actual auth model: flat JWT scopes via
security.py's require_scope() (no "profiles"/maker-checker roles), and cases
are private to their creator (owner == JWT `sub`).

Usage in a service's main.py::

    from case_store import init_db, make_case_router

    app = FastAPI()
    init_db()

    def _analyze(paths, emit, user_sub):
        return review_collateral(paths["legal"], paths["property"], _provider, models=_models(), emit=emit)

    app.include_router(make_case_router(
        service_scope="collateral",
        upload_slots={"legal": {".pdf", ".docx"}, "property": {".pdf", ".docx"}},
        min_slots_ready=["legal", "property"],
        analyze=_analyze,
    ))

Endpoints: list/create/get/delete cases, upload one slot, analyze the case
(SSE), fetch a stored result — plus extra pairs on a case (`POST/DELETE
/cases/{id}/pairs`, `POST /cases/{id}/pairs/{i}/uploads/{slot}`). A case
reviews its own uploads plus every extra pair, one pass each, and keeps a
result per pair; see start_analyze below for the event contract.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import audit_client
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import JSON, DateTime, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from security import require_scope
from streaming import sse_stream

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/app.db")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))

_engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
# expire_on_commit=False: several handlers read attributes off a row (e.g.
# case.name for an audit call) right after commit() — no need to force a
# re-fetch for a single-process SQLite-backed service this size.
SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(24), default="new")
    uploads: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(_engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _case_dir(case_id: str) -> Path:
    d = DATA_DIR / "cases" / case_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slot_file(case_id: str, slot: str) -> Path | None:
    case_dir = _case_dir(case_id)
    return next((p for p in sorted(case_dir.glob(f"{slot}.*")) if p.is_file()), None)


# --- extra pairs ------------------------------------------------------------
# A case reviews one set of uploads by default, but the same case can hold more:
# an "extra pair" is another full set of the same slots, analyzed in its own
# pass and keeping its own result. Both live on the one case, so the results sit
# side by side where the work was done instead of scattering across new cases.
EXTRA_PAIRS_KEY = "extra_pairs"
PAIRS_RESULT_KEY = "__pairs__"


def _extra_dir(case_id: str, index: int) -> Path:
    d = _case_dir(case_id) / "extra" / str(index)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _extra_slot_file(case_id: str, index: int, slot: str) -> Path | None:
    d = _extra_dir(case_id, index)
    return next((p for p in sorted(d.glob(f"{slot}.*")) if p.is_file()), None)


def _extra_pairs(case: Case) -> list[dict]:
    """The extra pairs' uploaded file names, [{slot: filename}, ...]."""
    raw = (case.uploads or {}).get(EXTRA_PAIRS_KEY)
    return [dict(p) for p in raw] if isinstance(raw, list) else []


def _set_extra_pairs(case: Case, pairs: list[dict]) -> None:
    uploads = {k: v for k, v in (case.uploads or {}).items() if k != EXTRA_PAIRS_KEY}
    if pairs:
        uploads[EXTRA_PAIRS_KEY] = pairs
    case.uploads = uploads


def _main_uploads(case: Case) -> dict:
    return {k: v for k, v in (case.uploads or {}).items() if k != EXTRA_PAIRS_KEY}


def _stored_pair_outcomes(case: Case) -> list[dict] | None:
    """The per-pair outcomes if this case stores them, else None (single pair)."""
    if isinstance(case.result, dict):
        raw = case.result.get(PAIRS_RESULT_KEY)
        if isinstance(raw, list):
            return raw
    return None


def _audit(service: str, user: str, action: str, resource: str | None = None,
           metadata: dict | None = None) -> None:
    audit_client.audit(user, service, action, resource=resource, metadata=metadata)


def _emit_event(emit: EmitFn | None, payload: dict) -> None:
    if emit:
        emit("event", json.dumps(payload, separators=(",", ":")))


def _scoped_emit(emit: EmitFn, scope: dict[str, Any]) -> EmitFn:
    """Wrap `emit` so the engine's own stage events carry the case they belong
    to. Token chunks ("content"/"reasoning") pass through untouched — the data
    field of those frames is a bare string by contract."""

    def scoped(ev_type: str, text: str) -> None:
        if ev_type == "event":
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                text = json.dumps({**payload, **scope}, separators=(",", ":"))
        emit(ev_type, text)

    return scoped


def _get_owned_case(db: Session, case_id: str, user_sub: str) -> Case:
    # 404, not 403, on a case that exists but belongs to someone else —
    # don't reveal that the id is valid.
    case = db.get(Case, case_id)
    if case is None or case.created_by != user_sub:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


def _list_payload(c: Case) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "status": c.status,
        "uploads": _main_uploads(c),
        "extra_pairs": len(_extra_pairs(c)),
        "created_at": c.created_at.isoformat(),
    }


def _detail_payload(c: Case) -> dict[str, Any]:
    out = _list_payload(c)
    outcomes = _stored_pair_outcomes(c)
    extras = _extra_pairs(c)

    if outcomes is None:
        # Single-pair case: `result`/`error` exactly as before.
        out["result"] = c.result if c.status == "done" else None
        out["error"] = (c.result or {}).get("error") if c.status == "failed" else None
        first = {"result": out["result"], "error": out["error"]}
    else:
        first = outcomes[0] if outcomes else {}
        out["result"] = first.get("result")
        out["error"] = first.get("error")

    # `pairs` is always present and always includes pair 0, so a client renders
    # one tab per pair without special-casing the single-pair shape.
    pairs = [{
        "index": 0,
        "uploads": _main_uploads(c),
        "result": first.get("result"),
        "error": first.get("error"),
    }]
    for i, uploads in enumerate(extras, start=1):
        outcome = outcomes[i] if outcomes and i < len(outcomes) else {}
        pairs.append({
            "index": i,
            "uploads": uploads,
            "result": outcome.get("result"),
            "error": outcome.get("error"),
        })
    out["pairs"] = pairs
    return out


class CaseCreateBody(BaseModel):
    name: str


# emit(type, text) with type in {"event", "content", "reasoning"} — same
# contract as streaming.py's sse_stream(), since analyze() is handed
# straight through to it.
EmitFn = Callable[[str, str], None]
# analyze(paths, emit, user_sub) -> result dict. `paths` maps every upload slot
# to its file on disk (all `min_slots_ready` slots guaranteed present).
# `user_sub` is the case owner (cases are per-user, see Case.created_by) — for
# services whose analysis depends on that account's own configuration rather
# than only on the uploads, e.g. insurance grading against the bank policy that
# account uploaded. Most services ignore it.
AnalyzeFn = Callable[[dict[str, Path], EmitFn, str], dict]

# Statuses from which POST /analyze is allowed (re-analysis included).
ANALYZABLE_STATUSES = ("ready", "done", "failed")


def make_case_router(
    *,
    service_scope: str,
    upload_slots: dict[str, set[str]],
    min_slots_ready: list[str],
    analyze: AnalyzeFn,
    to_audit_output: Callable[[dict], dict] | None = None,
) -> APIRouter:
    """Build a `/cases` router for a stateless review service.

    `service_scope` doubles as the JWT scope required to use any endpoint
    here (via security.py's require_scope) and as the audit-log `service`
    label.

    `to_audit_output`: this router is shared across services with very
    different result shapes, so it has no opinion on what's audit-worthy in
    any of them — only the caller does. When given, it's called on a
    successful analyze() result to build what actually gets sent to the
    audit trail; `case.result` (what the frontend reads) always gets the
    real, untouched value. The intended shape is a service's own Pydantic
    DTO — e.g. doc_rev-service's `CompareAuditOutput.model_validate(result)`
    — so what's audited is declared as real fields, not filtered out of the
    full result by key name.
    """
    unknown = set(min_slots_ready) - set(upload_slots)
    if unknown:
        raise ValueError(f"min_slots_ready names unknown slot(s): {sorted(unknown)}")

    slots = {
        slot: {s.lower() if s.startswith(".") else f".{s.lower()}" for s in suffixes}
        for slot, suffixes in upload_slots.items()
    }

    router = APIRouter(prefix="/cases", tags=["cases"])

    def _require_user(user: dict = Depends(require_scope(service_scope))) -> str:
        sub = user.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Token has no subject")
        return sub

    @router.get("")
    def list_cases(user_sub: str = Depends(_require_user), db: Session = Depends(get_db)) -> dict:
        rows = (
            db.execute(select(Case).where(Case.created_by == user_sub).order_by(Case.created_at.desc()))
            .scalars()
            .all()
        )
        return {"cases": [_list_payload(c) for c in rows]}

    @router.post("", status_code=201)
    def create_case(
        body: CaseCreateBody, user_sub: str = Depends(_require_user), db: Session = Depends(get_db)
    ) -> dict:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="A case name is required")
        case = Case(name=name, status="new", uploads={}, created_by=user_sub)
        db.add(case)
        db.commit()
        # No metadata: `name` is already the Resource field above; nothing
        # else about a bare case-create is worth an audit reader's time.
        _audit(service_scope, user_sub, "case.create", resource=name)
        return _detail_payload(case)

    @router.get("/{case_id}")
    def get_case(case_id: str, user_sub: str = Depends(_require_user), db: Session = Depends(get_db)) -> dict:
        return _detail_payload(_get_owned_case(db, case_id, user_sub))

    @router.delete("/{case_id}")
    def delete_case(case_id: str, user_sub: str = Depends(_require_user), db: Session = Depends(get_db)) -> dict:
        case = _get_owned_case(db, case_id, user_sub)
        if case.status == "analyzing":
            raise HTTPException(status_code=409, detail="Analysis is running; wait for it to finish")
        shutil.rmtree(_case_dir(case_id), ignore_errors=True)
        name = case.name
        db.delete(case)
        db.commit()
        _audit(service_scope, user_sub, "case.delete", resource=name)
        return {"ok": True}

    def _invalidate_pair(case: Case, index: int) -> None:
        """Replacing a pair's document throws away that pair's stored result.

        Analysis only runs pairs without a result, so a replaced document had to
        stop counting as reviewed — otherwise the new file would sit there
        looking analyzed while every Compare silently skipped it."""
        total = len(_extra_pairs(case)) + 1
        outcomes = _outcomes_snapshot(case)
        outcomes = [dict(outcomes[i]) if i < len(outcomes) else {} for i in range(total)]
        if index < total:
            outcomes[index] = {}
        if total == 1:
            case.result = None
        elif any(o for o in outcomes):
            case.result = {PAIRS_RESULT_KEY: outcomes}
        else:
            case.result = None
        # Back to a state that invites another run, rather than claiming a
        # verdict this case no longer has for every pair.
        if case.status in ("done", "failed"):
            case.status = "ready"

    def _check_slot(slot: str) -> None:
        if slot not in slots:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown upload slot '{slot}'; expected one of: {', '.join(sorted(slots))}",
            )

    def _check_suffix(slot: str, filename: str) -> str:
        suffix = Path(filename or "").suffix.lower()
        if suffix not in slots[slot]:
            raise HTTPException(
                status_code=422,
                detail=f"File type {suffix or '(none)'!r} is not allowed for slot '{slot}'; "
                       f"expected one of: {', '.join(sorted(slots[slot]))}",
            )
        return suffix

    # --- extra pairs on this case ---------------------------------------

    @router.post("/{case_id}/pairs", status_code=201)
    def add_pair(case_id: str, user_sub: str = Depends(_require_user), db: Session = Depends(get_db)) -> dict:
        """Add another set of this service's slots to review under this case."""
        case = _get_owned_case(db, case_id, user_sub)
        if case.status == "analyzing":
            raise HTTPException(status_code=409, detail="Analysis is running; wait for it to finish")
        _set_extra_pairs(case, _extra_pairs(case) + [{}])
        db.commit()
        return _detail_payload(case)

    @router.delete("/{case_id}/pairs/{index}")
    def remove_pair(
        case_id: str, index: int,
        user_sub: str = Depends(_require_user), db: Session = Depends(get_db),
    ) -> dict:
        case = _get_owned_case(db, case_id, user_sub)
        if case.status == "analyzing":
            raise HTTPException(status_code=409, detail="Analysis is running; wait for it to finish")
        pairs = _extra_pairs(case)
        if index == 0:
            # Pair 0 is the case's own documents; deleting it means deleting the case.
            raise HTTPException(
                status_code=409,
                detail="Pair 1 is the case's own documents — delete the case instead",
            )
        if index < 1 or index > len(pairs):
            raise HTTPException(status_code=404, detail=f"No pair {index + 1} on this case")
        # Renumber the directories after the removed one, so pair i on disk keeps
        # matching pair i in `uploads`.
        shutil.rmtree(_extra_dir(case_id, index), ignore_errors=True)
        for i in range(index + 1, len(pairs) + 1):
            src = _case_dir(case_id) / "extra" / str(i)
            if src.exists():
                src.rename(_case_dir(case_id) / "extra" / f"{i - 1}")
        pairs.pop(index - 1)
        _set_extra_pairs(case, pairs)
        # Stored outcomes are indexed by pair, so drop this pair's too.
        outcomes = _stored_pair_outcomes(case)
        if outcomes and index < len(outcomes):
            kept = [o for i, o in enumerate(outcomes) if i != index]
            case.result = {PAIRS_RESULT_KEY: kept}
        db.commit()
        return _detail_payload(case)

    # index 0 = the case's own slots (an alias for /uploads/{slot}), so callers
    # can address every pair the same way.
    @router.post("/{case_id}/pairs/{index}/uploads/{slot}")
    async def upload_pair_slot(
        case_id: str, index: int, slot: str,
        file: UploadFile = File(...),
        user_sub: str = Depends(_require_user),
        db: Session = Depends(get_db),
    ) -> dict:
        case = _get_owned_case(db, case_id, user_sub)
        _check_slot(slot)
        if case.status == "analyzing":
            raise HTTPException(
                status_code=409, detail="Analysis is running; wait for it to finish before replacing files"
            )
        pairs = _extra_pairs(case)
        if index < 0 or index > len(pairs):
            raise HTTPException(status_code=404, detail=f"No pair {index + 1} on this case")
        suffix = _check_suffix(slot, file.filename or "")
        content = await file.read()

        # Pair 0 is the case's own slots, which live in the case directory and in
        # `uploads` directly — /pairs/0/uploads/{slot} is accepted as an alias for
        # /uploads/{slot} so that "pair index" means the same thing everywhere.
        pair_dir = _case_dir(case_id) if index == 0 else _extra_dir(case_id, index)
        dest = pair_dir / f"{slot}{suffix}"
        dest.write_bytes(content)
        for stale in pair_dir.glob(f"{slot}.*"):
            if stale != dest and stale.is_file():
                stale.unlink(missing_ok=True)

        filename = file.filename or dest.name
        if index == 0:
            uploads = {**_main_uploads(case), slot: filename}
            _set_extra_pairs(case, pairs)   # keep the bookkeeping key
            case.uploads = {**uploads, **({EXTRA_PAIRS_KEY: pairs} if pairs else {})}
        else:
            pairs[index - 1] = {**pairs[index - 1], slot: filename}
            _set_extra_pairs(case, pairs)
        _invalidate_pair(case, index)
        if index == 0 and all(s in _main_uploads(case) for s in min_slots_ready):
            case.status = "ready"
        db.commit()

        attachment_id = audit_client.upload_attachment(filename, content)
        attachments = [{"filename": filename, "attachment_id": attachment_id}] if attachment_id else []
        label = f"{case.name}:{slot}" if index == 0 else f"{case.name}:pair{index + 1}:{slot}"
        _audit(service_scope, user_sub, "case.upload", resource=label,
               metadata={"input": {"attachments": attachments}})
        return _detail_payload(case)

    @router.post("/{case_id}/uploads/{slot}")
    async def upload_slot(
        case_id: str,
        slot: str,
        file: UploadFile = File(...),
        user_sub: str = Depends(_require_user),
        db: Session = Depends(get_db),
    ) -> dict:
        case = _get_owned_case(db, case_id, user_sub)
        if slot not in slots:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown upload slot '{slot}'; expected one of: {', '.join(sorted(slots))}",
            )
        if case.status == "analyzing":
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

        case_dir = _case_dir(case_id)
        dest = case_dir / f"{slot}{suffix}"
        content = await file.read()
        dest.write_bytes(content)
        # Replace semantics: one file per slot — drop other-suffix leftovers
        # from a previous upload to this same slot.
        for stale in case_dir.glob(f"{slot}.*"):
            if stale != dest and stale.is_file():
                stale.unlink(missing_ok=True)

        uploads = dict(case.uploads or {})
        filename = file.filename or dest.name
        uploads[slot] = filename
        case.uploads = uploads
        _invalidate_pair(case, 0)
        if all(s in uploads for s in min_slots_ready):
            case.status = "ready"
        db.commit()

        attachment_id = audit_client.upload_attachment(filename, content)
        attachments = [{"filename": filename, "attachment_id": attachment_id}] if attachment_id else []
        # `slot` is already part of the Resource field above; the file itself
        # is the only thing worth showing again here.
        _audit(service_scope, user_sub, "case.upload", resource=f"{case.name}:{slot}",
               metadata={"input": {"attachments": attachments}})
        return _detail_payload(case)

    def _paths_for(case_id: str) -> dict[str, Path]:
        found = {}
        for s in slots:
            p = _slot_file(case_id, s)
            if p is not None:
                found[s] = p
        return found

    def _pair_paths(case: Case) -> list[dict[str, Path]]:
        """One slot->file mapping per pair on this case: pair 0 is the case's own
        uploads, then each extra pair in order."""
        out = [_paths_for(case.id)]
        for i in range(1, len(_extra_pairs(case)) + 1):
            found = {}
            for slot in slots:
                f = _extra_slot_file(case.id, i, slot)
                if f is not None:
                    found[slot] = f
            out.append(found)
        return out

    def _outcomes_snapshot(case: Case) -> list[dict]:
        """The per-pair outcomes as they stand right now.

        Must be taken before analysis flips the case to `analyzing`: a
        single-pair case stores the bare engine result, so telling a result from
        a failure relies on the status, and re-deriving it later would silently
        drop pair 0's stored result.
        """
        stored = _stored_pair_outcomes(case)
        if stored is not None:
            return [dict(o) for o in stored]
        if case.status == "done" and case.result is not None:
            return [{"result": case.result}]
        if case.status == "failed" and isinstance(case.result, dict) and "error" in case.result:
            return [{"error": case.result["error"]}]
        return [{}]

    def _reviewed(case: Case, index: int) -> bool:
        """Has this pair already produced a result? A stored error counts as not
        reviewed, so pressing Compare again retries what failed."""
        outcomes = _outcomes_snapshot(case)
        return index < len(outcomes) and outcomes[index].get("result") is not None

    def _resolve_targets(case: Case, scope: str | None) -> list[int]:
        """Which pairs this request should analyze.

        Default ("pending") is every pair without a result yet — so adding a
        second pair to a reviewed case costs one engine pass, not two. "all"
        re-runs everything; a bare index re-runs that one pair.
        """
        total = len(_extra_pairs(case)) + 1
        scope = (scope or "pending").strip().lower()
        if scope == "all":
            return list(range(total))
        if scope != "pending":
            if not scope.isdigit() or int(scope) >= total:
                raise HTTPException(
                    status_code=400,
                    detail=f"pairs must be 'pending', 'all', or a pair index below {total}",
                )
            return [int(scope)]
        pending = [i for i in range(total) if not _reviewed(case, i)]
        if not pending:
            raise HTTPException(
                status_code=409,
                detail="Every pair on this case has already been reviewed — "
                       "re-run one from its tab, or pass pairs=all",
            )
        return pending

    def _prepare_analyze(
        case: Case, user_sub: str, db: Session, targets: list[int]
    ) -> tuple[dict[int, dict[str, Path]], dict[int, dict]]:
        """Flip the case to `analyzing` and build the targeted pairs' paths +
        audit `input`. Only the targets are validated: a half-filled pair you
        aren't running shouldn't block the one you are."""
        if case.status not in ANALYZABLE_STATUSES:
            hint = f"; upload {', '.join(min_slots_ready)} first" if case.status == "new" else ""
            raise HTTPException(status_code=409, detail=f"Cannot analyze from status '{case.status}'{hint}")

        all_paths = _pair_paths(case)
        pairs = {}
        for i in targets:
            paths = all_paths[i]
            missing = [s for s in min_slots_ready if s not in paths]
            if missing:
                where = "this case" if i == 0 else f"pair {i + 1}"
                raise HTTPException(
                    status_code=409,
                    detail=f"Missing required upload(s) on {where}: {', '.join(missing)}",
                )
            pairs[i] = paths

        case.status = "analyzing"
        db.commit()

        # Re-read from disk (not the case.upload-time copies) so the audit
        # attachment reflects exactly what's fed to the model even after a slot
        # replace. Uploads' filenames are already visible on the attachments
        # themselves, so that's all `input` needs.
        inputs = {}
        for i, paths in pairs.items():
            attachments = []
            for path in paths.values():
                attachment_id = audit_client.upload_attachment(path.name, path.read_bytes())
                if attachment_id:
                    attachments.append({"filename": path.name, "attachment_id": attachment_id})
            inputs[i] = {"attachments": attachments}
        return pairs, inputs

    def _merge_outcomes(
        case_id: str, baseline: list[dict], updates: dict[int, dict], status: str | None
    ) -> list[dict]:
        """Write the analyzed pairs' outcomes onto the case, leaving every other
        pair's stored outcome alone — re-running one pair must not blank the
        results of the pairs that weren't re-run.

        A single-pair case keeps the bare engine result it always had, so nothing
        downstream sees a new shape unless extra pairs are actually in play."""
        persist_db = SessionLocal()
        try:
            row = persist_db.get(Case, case_id)
            if row is None:
                return []
            total = len(_extra_pairs(row)) + 1
            outcomes = [dict(baseline[i]) if i < len(baseline) else {} for i in range(total)]
            for i, outcome in updates.items():
                if i < total:
                    outcomes[i] = outcome
            if total == 1:
                only = outcomes[0]
                row.result = only.get("result") if "result" in only else {"error": only.get("error")}
            else:
                row.result = {PAIRS_RESULT_KEY: outcomes}
            if status is not None:
                row.status = status
            persist_db.commit()
            return outcomes
        finally:
            persist_db.close()

    @router.post("/{case_id}/analyze")
    async def start_analyze(
        case_id: str,
        pairs: str | None = Query(
            None,
            description="Which pairs to analyze: 'pending' (default — those without a "
                        "result yet), 'all', or a single pair index.",
        ),
        user_sub: str = Depends(_require_user),
        db: Session = Depends(get_db),
    ):
        """Analyze this case's pairs, one after another, streaming each pair's
        outcome as it lands and persisting it on the case.

        By default only pairs without a result are run, so adding a pair to a
        case that's already been reviewed costs one engine pass rather than
        re-running work that's already paid for.

        On top of streaming.py's contract, every `event` frame produced inside a
        pair carries `pair` (0-based index), and three stages bracket each one:

            {"stage": "pair_start",  "pair": 0}
            {"stage": "pair_result", "pair": 0, "result": {...}}
            {"stage": "pair_error",  "pair": 0, "error": "..."}

        Pairs run one at a time on purpose: each engine pipeline already fans out
        internally, and sequential runs are what make a `pair_start` frame
        unambiguous — every later frame, including `content` token chunks (bare
        strings by contract, with nowhere to put a tag), belongs to that pair
        until the next `pair_start`.
        """
        case = _get_owned_case(db, case_id, user_sub)
        targets = _resolve_targets(case, pairs)
        # Snapshot before _prepare_analyze flips the status (see _outcomes_snapshot).
        baseline = _outcomes_snapshot(case)
        pair_paths, pair_inputs = _prepare_analyze(case, user_sub, db, targets)
        name = case.name

        def run(emit: EmitFn) -> dict:
            # Runs on sse_stream()'s worker thread — never reuse the request's
            # `db` session here (it belongs to the request's own async context);
            # _merge_outcomes opens its own, same reasoning as docgen-service's
            # job callbacks using session_scope().
            updates: dict[int, dict] = {}
            for i in targets:
                paths, analyze_input = pair_paths[i], pair_inputs[i]
                label = name if i == 0 else f"{name} (pair {i + 1})"
                _emit_event(emit, {"stage": "pair_start", "pair": i})
                try:
                    result = analyze(paths, _scoped_emit(emit, {"pair": i}), user_sub)
                except Exception as exc:   # one bad pair must not sink the rest
                    error = f"{type(exc).__name__}: {exc}"
                    updates[i] = {"error": error}
                    _merge_outcomes(case_id, baseline, updates, "analyzing")
                    _audit(service_scope, user_sub, "case.analyze", resource=label,
                           metadata={"input": analyze_input, "output": {"error": error}})
                    _emit_event(emit, {"stage": "pair_error", "pair": i, "error": error})
                    continue
                updates[i] = {"result": result}
                # Persist as each pair finishes, so reloading mid-run shows the
                # pairs already done rather than nothing.
                _merge_outcomes(case_id, baseline, updates, "analyzing")
                audit_output = to_audit_output(result) if to_audit_output else result
                _audit(service_scope, user_sub, "case.analyze", resource=label,
                       metadata={"input": analyze_input, "output": audit_output})
                _emit_event(emit, {"stage": "pair_result", "pair": i, "result": result})

            # Status reflects the whole case, not just this request's pairs.
            outcomes = _merge_outcomes(case_id, baseline, updates, None)
            failed = bool(outcomes) and all(o.get("result") is None for o in outcomes)
            _merge_outcomes(case_id, baseline, updates, "failed" if failed else "done")
            if failed:
                # Every pair failed — surface it the way a single-case failure
                # always was, as an SSE error rather than a result.
                first_error = next((o.get("error") for o in outcomes if o.get("error")), "analysis failed")
                raise RuntimeError(first_error)
            return {"pairs": [updates[i] for i in targets if i in updates]}

        return await sse_stream(run)

    @router.get("/{case_id}/result")
    def get_result(case_id: str, user_sub: str = Depends(_require_user), db: Session = Depends(get_db)) -> dict:
        case = _get_owned_case(db, case_id, user_sub)
        if case.status != "done" or case.result is None:
            raise HTTPException(status_code=404, detail="Result not available until analysis completes")
        return case.result

    return router
