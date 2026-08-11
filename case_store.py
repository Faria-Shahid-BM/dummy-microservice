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

    def _analyze(paths, emit):
        return review_collateral(paths["legal"], paths["property"], _provider, models=_models(), emit=emit)

    app.include_router(make_case_router(
        service_scope="collateral",
        upload_slots={"legal": {".pdf", ".docx"}, "property": {".pdf", ".docx"}},
        min_slots_ready=["legal", "property"],
        analyze=_analyze,
    ))
"""
from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import audit_client
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
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


def _audit(service: str, user: str, action: str, resource: str | None = None,
           metadata: dict | None = None) -> None:
    audit_client.audit(user, service, action, resource=resource, metadata=metadata)


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
        "uploads": c.uploads or {},
        "created_at": c.created_at.isoformat(),
    }


def _detail_payload(c: Case) -> dict[str, Any]:
    out = _list_payload(c)
    out["result"] = c.result if c.status == "done" else None
    out["error"] = (c.result or {}).get("error") if c.status == "failed" else None
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

    @router.post("/{case_id}/analyze")
    async def start_analyze(case_id: str, user_sub: str = Depends(_require_user), db: Session = Depends(get_db)):
        case = _get_owned_case(db, case_id, user_sub)
        if case.status not in ANALYZABLE_STATUSES:
            hint = f"; upload {', '.join(min_slots_ready)} first" if case.status == "new" else ""
            raise HTTPException(status_code=409, detail=f"Cannot analyze from status '{case.status}'{hint}")

        paths: dict[str, Path] = {}
        for s in slots:
            p = _slot_file(case_id, s)
            if p is not None:
                paths[s] = p
        missing = [s for s in min_slots_ready if s not in paths]
        if missing:
            raise HTTPException(status_code=409, detail=f"Missing required upload(s): {', '.join(missing)}")

        case.status = "analyzing"
        db.commit()

        # Re-read from disk (not the case.upload-time copies) so the audit
        # attachment reflects exactly what's fed to the model even after a
        # slot replace. Uploads' filenames are already visible on the
        # attachments themselves, so that's all `input` needs.
        analyze_attachments = []
        for s, p in paths.items():
            attachment_id = audit_client.upload_attachment(p.name, p.read_bytes())
            if attachment_id:
                analyze_attachments.append({"filename": p.name, "attachment_id": attachment_id})
        analyze_input = {"attachments": analyze_attachments}

        def run(emit: EmitFn) -> dict:
            # Runs on sse_stream()'s worker thread — never reuse the
            # request's `db` session here (it belongs to the request's own
            # async context); open a fresh one instead, same reasoning as
            # docgen-service's job callbacks (app/jobs/runner.py) using
            # session_scope() rather than the request session.
            try:
                result = analyze(paths, emit, user_sub)
            except Exception as exc:
                error = {"error": f"{type(exc).__name__}: {exc}"}
                persist_db = SessionLocal()
                try:
                    row = persist_db.get(Case, case_id)
                    if row is not None:
                        row.status = "failed"
                        row.result = error
                        persist_db.commit()
                finally:
                    persist_db.close()
                _audit(service_scope, user_sub, "case.analyze", resource=case.name,
                       metadata={"input": analyze_input, "output": error})
                raise  # sse_stream() still surfaces this as an "error" SSE event
            persist_db = SessionLocal()
            try:
                row = persist_db.get(Case, case_id)
                if row is not None:
                    row.status = "done"
                    row.result = result
                    persist_db.commit()
            finally:
                persist_db.close()
            audit_output = to_audit_output(result) if to_audit_output else result
            _audit(service_scope, user_sub, "case.analyze", resource=case.name,
                   metadata={"input": analyze_input, "output": audit_output})
            return result

        return await sse_stream(run)

    @router.get("/{case_id}/result")
    def get_result(case_id: str, user_sub: str = Depends(_require_user), db: Session = Depends(get_db)) -> dict:
        case = _get_owned_case(db, case_id, user_sub)
        if case.status != "done" or case.result is None:
            raise HTTPException(status_code=404, detail="Result not available until analysis completes")
        return case.result

    return router
