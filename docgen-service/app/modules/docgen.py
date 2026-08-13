"""Document Generation module — the POC's core flow rebuilt on the platform.

Per-case pipeline (ARCHITECTURE.md "docgen"):

    input upload → extract (job) → case_text.md → analyze (job) → analysis.md
    → select (job) → selected_docs.json → fill (one job per template instance)
    → output/{uuid}.docx + {uuid}.provenance.json + GeneratedDocument row
    → maker submits each document for checker review (approvals).

Stage artifacts live in storage under the case directory; the DB holds the
``Case`` row (name, progressive status, input display name) and one
``GeneratedDocument`` row per fill output. Role guards: any profile member
views, makers mutate. LLM work always runs through the job runner (never
in-request); job callbacks persist via ``session_scope`` — never the request
session. Fill templates come exclusively from the CURRENT APPROVED version in
the template library (``control.templates.usable_templates``).
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, storage
from app.auth.deps import current_user, require_profile_maker, require_profile_member
from app.control.approvals import SUBJECT_RESOLVERS, ensure_approval, submit_approval
from app.control.profile_config import effective_model, prompt_override
from app.control.templates import usable_templates
from app.core.db import db_session, session_scope
from app.engines.util import EngineParseError
from app.jobs.runner import JobConflict, runner
from app.llm.registry import get_provider
from app.models import (
    JOB_ACTIVE_STATUSES,
    Approval,
    Case,
    GeneratedDocument,
    Job,
    User,
)

router = APIRouter(prefix="/api", tags=["docgen"])

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

INPUT_SUFFIXES = {".pdf", ".docx"}

# Progressive case status — advanced as stages complete, never regressed
# (except input deletion, which drops a bare "input" case back to "new").
CASE_STATUS_ORDER = (
    "new", "input", "extracted", "analyzed", "selected", "generating", "generated",
)


# ------------------------------------------------------------ approval plumbing


def _resolve_generated_document(db: Session, doc_id: str) -> dict:
    d = db.get(GeneratedDocument, doc_id)
    if d is None:
        return {"name": "(deleted document)"}
    name = f"{d.template_name}__{d.instance_label}" if d.instance_label else d.template_name
    return {
        "name": name,
        "link": f"/p/{d.profile_id}/cases/{d.case_id}",
        "file_name": d.file_name,
    }


SUBJECT_RESOLVERS["generated_document"] = _resolve_generated_document
# No APPROVAL_EFFECTS: approving a generated document changes only its state.


# -------------------------------------------------------------------- helpers


def _advance_status(case: Case, new_status: str) -> None:
    """Move the case's progressive status forward; never backwards."""
    current = CASE_STATUS_ORDER.index(case.status) if case.status in CASE_STATUS_ORDER else -1
    if CASE_STATUS_ORDER.index(new_status) > current:
        case.status = new_status


def _get_case(db: Session, profile_id: str, case_id: str) -> Case:
    c = db.get(Case, case_id)
    if c is None or c.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Case not found")
    return c


def _get_document(
    db: Session, profile_id: str, case_id: str, doc_id: str
) -> GeneratedDocument:
    d = db.get(GeneratedDocument, doc_id)
    if d is None or d.case_id != case_id or d.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Document not found")
    return d


def _domain_knowledge(profile_id: str) -> str:
    path = storage.profile_dir(profile_id) / "domain_knowledge.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _read_case_text(profile_id: str, case_id: str) -> str | None:
    path = storage.case_dir(profile_id, case_id) / "case_text.md"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _case_payload(db: Session, case: Case) -> dict:
    """Case row + stage flags derived from storage files and document rows."""
    cd = storage.case_dir(case.profile_id, case.id)
    input_dir = cd / "input"
    has_input = input_dir.is_dir() and any(p.is_file() for p in input_dir.iterdir())
    generated_count = db.execute(
        select(func.count())
        .select_from(GeneratedDocument)
        .where(GeneratedDocument.case_id == case.id)
    ).scalar_one()
    return {
        "id": case.id,
        "name": case.name,
        "status": case.status,
        "input_file_name": case.input_file_name,
        "created_at": case.created_at.isoformat(),
        "has_input": has_input,
        "has_case_text": (cd / "case_text.md").is_file(),
        "has_analysis": (cd / "analysis.md").is_file(),
        "has_selected": (cd / "selected_docs.json").is_file(),
        "generated_count": generated_count,
    }


def _validate_selection(data: object) -> dict:
    """Schema check for hand-edited selections (PUT /selected)."""
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Selection must be a JSON object")
    docs = data.get("selected_documents")
    if not isinstance(docs, list):
        raise HTTPException(
            status_code=422, detail="'selected_documents' must be a list"
        )
    for i, entry in enumerate(docs):
        where = f"selected_documents[{i}]"
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422, detail=f"{where} must be an object")
        name = entry.get("template_name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(
                status_code=422, detail=f"{where}.template_name must be a non-empty string"
            )
        count = entry.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise HTTPException(
                status_code=422, detail=f"{where}.count must be a non-negative integer"
            )
        entities = entry.get("entities")
        if entities is not None and not isinstance(entities, list):
            raise HTTPException(
                status_code=422, detail=f"{where}.entities must be a list when present"
            )
    return data


def _expand_tasks(selection: dict) -> list[dict]:
    """Expand a selection into fill tasks exactly like the POC.

    ``count <= 1`` → a single unlabelled instance; otherwise one instance per
    entity with 1-based instance labels. ``task_key`` is
    ``"{template_name}__{label}"`` for labelled instances, else the name.
    """
    tasks: list[dict] = []
    for entry in selection.get("selected_documents") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("template_name")
        if not isinstance(name, str) or not name.strip():
            continue
        evidence = entry.get("evidence") or ""
        if not isinstance(evidence, str):
            evidence = json.dumps(evidence, ensure_ascii=False)
        count = entry.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int):
            count = 1
        entities = entry.get("entities") or []
        if not isinstance(entities, list):
            entities = []
        if count <= 1:
            instances: list[tuple[str | None, str]] = [(None, "")]
        else:
            instances = [
                (
                    str(entities[i]) if i < len(entities) and entities[i] is not None else None,
                    str(i + 1),
                )
                for i in range(count)
            ]
        for entity_scope, label in instances:
            tasks.append(
                {
                    "task_key": f"{name}__{label}" if label else name,
                    "template_name": name,
                    "evidence": evidence,
                    "entity_scope": entity_scope,
                    "instance_label": label,
                }
            )
    return tasks


# ------------------------------------------------------------- list / create


class CaseBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)


@router.get("/profiles/{profile_id}/cases")
def list_cases(
    profile_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    rows = db.execute(
        select(Case).where(Case.profile_id == profile_id).order_by(Case.created_at.desc())
    ).scalars().all()
    return {"cases": [_case_payload(db, c) for c in rows]}


@router.post("/profiles/{profile_id}/cases", status_code=201)
def create_case(
    profile_id: str,
    body: CaseBody,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Case name is required")
    c = Case(profile_id=profile_id, name=name, created_by=user.id)
    db.add(c)
    db.flush()
    storage.case_input_dir(profile_id, c.id)  # pre-create the layout
    storage.case_output_dir(profile_id, c.id)
    audit.record(db, user, "case.create", profile_id=profile_id, subject_type="case",
                 subject_id=c.id, detail={"name": name}, request=request)
    db.commit()
    return _case_payload(db, c)


# ------------------------------------------------------ detail / rename / delete


@router.get("/profiles/{profile_id}/cases/{case_id}")
def get_case(
    profile_id: str,
    case_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    c = _get_case(db, profile_id, case_id)
    payload = _case_payload(db, c)
    active = db.execute(
        select(Job)
        .where(Job.subject_id == case_id, Job.status.in_(JOB_ACTIVE_STATUSES))
        .order_by(Job.created_at)
    ).scalars().all()
    payload["active_jobs"] = [
        {"id": j.id, "kind": j.kind, "key": j.key, "status": j.status} for j in active
    ]
    return payload


@router.patch("/profiles/{profile_id}/cases/{case_id}")
def rename_case(
    profile_id: str,
    case_id: str,
    body: CaseBody,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    c = _get_case(db, profile_id, case_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Case name is required")
    if name != c.name:
        audit.record(db, user, "case.rename", profile_id=profile_id, subject_type="case",
                     subject_id=case_id, detail={"from": c.name, "to": name}, request=request)
        c.name = name
        db.commit()
    return _case_payload(db, c)


@router.delete("/profiles/{profile_id}/cases/{case_id}")
def delete_case(
    profile_id: str,
    case_id: str,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    c = _get_case(db, profile_id, case_id)
    doc_ids = db.execute(
        select(GeneratedDocument.id).where(GeneratedDocument.case_id == case_id)
    ).scalars().all()
    if doc_ids:
        for a in db.execute(
            select(Approval).where(
                Approval.subject_type == "generated_document",
                Approval.subject_id.in_(doc_ids),
            )
        ).scalars():
            db.delete(a)
        for d in db.execute(
            select(GeneratedDocument).where(GeneratedDocument.case_id == case_id)
        ).scalars():
            db.delete(d)
    storage.delete_tree(storage.case_dir(profile_id, case_id))
    audit.record(db, user, "case.delete", profile_id=profile_id, subject_type="case",
                 subject_id=case_id, detail={"name": c.name, "documents": len(doc_ids)},
                 request=request)
    db.delete(c)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- input upload


@router.post("/profiles/{profile_id}/cases/{case_id}/input")
async def upload_input(
    profile_id: str,
    case_id: str,
    request: Request,
    file: UploadFile = File(...),
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    c = _get_case(db, profile_id, case_id)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in INPUT_SUFFIXES:
        raise HTTPException(
            status_code=422, detail="Only .pdf or .docx input files are supported"
        )
    input_dir = storage.case_input_dir(profile_id, case_id)
    # One input per case: replace whatever is there (on-disk name is server-set).
    for old in input_dir.iterdir():
        if old.is_file():
            old.unlink()
    try:
        await storage.save_upload(
            file, input_dir / f"input{suffix}", allowed_suffixes=INPUT_SUFFIXES
        )
    except storage.UploadError as exc:
        c.input_file_name = None  # old file is gone; keep row and disk in sync
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc))
    c.input_file_name = storage.sanitize_display_name(file.filename or f"input{suffix}")
    _advance_status(c, "input")
    audit.record(db, user, "case.input_upload", profile_id=profile_id, subject_type="case",
                 subject_id=case_id, detail={"file_name": c.input_file_name}, request=request)
    db.commit()
    return _case_payload(db, c)


@router.delete("/profiles/{profile_id}/cases/{case_id}/input")
def delete_input(
    profile_id: str,
    case_id: str,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    c = _get_case(db, profile_id, case_id)
    input_dir = storage.case_input_dir(profile_id, case_id)
    for old in input_dir.iterdir():
        if old.is_file():
            old.unlink()
    c.input_file_name = None
    if c.status == "input":
        c.status = "new"
    audit.record(db, user, "case.input_delete", profile_id=profile_id, subject_type="case",
                 subject_id=case_id, request=request)
    db.commit()
    return _case_payload(db, c)


# ------------------------------------------------------------------ extraction


class ExtractBody(BaseModel):
    force_vision: bool = False


@router.post("/profiles/{profile_id}/cases/{case_id}/extract")
def start_extract(
    profile_id: str,
    case_id: str,
    request: Request,
    body: ExtractBody | None = None,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    c = _get_case(db, profile_id, case_id)
    cd = storage.case_dir(profile_id, case_id)
    input_dir = cd / "input"
    files = sorted(p for p in input_dir.iterdir() if p.is_file()) if input_dir.is_dir() else []
    if not files:
        raise HTTPException(
            status_code=400, detail="No input file uploaded — upload a .pdf or .docx first"
        )
    input_path = files[0]
    force_vision = bool(body.force_vision) if body else False
    case_text_path = cd / "case_text.md"

    def run(emit):
        from app.engines import extraction

        # Resolve per-profile config at run time (fresh session), so the job
        # always uses the profile's CURRENT model/prompt overrides.
        with session_scope() as jdb:
            vision_model = effective_model(jdb, profile_id, "vision")
            transcription_prompt = prompt_override(
                jdb, profile_id, "extraction.transcription.prompt")
        result = extraction.extract_document(
            input_path,
            get_provider(),
            vision_model,
            emit=emit,
            force_vision=force_vision,
            prompt=transcription_prompt,
        )
        case_text_path.write_text(result.text, encoding="utf-8")
        with session_scope() as jdb:
            row = jdb.get(Case, case_id)
            if row is not None:
                _advance_status(row, "extracted")
        return {"pages_total": result.pages_total, "pages_failed": result.pages_failed}

    try:
        job = runner.submit(
            "case.extract",
            f"case-extract:{case_id}",
            run,
            profile_id=profile_id,
            subject_id=case_id,
            user_id=user.id,
        )
    except JobConflict:
        raise HTTPException(status_code=409, detail="Extraction is already running for this case")
    audit.record(db, user, "case.extract", profile_id=profile_id, subject_type="case",
                 subject_id=case_id,
                 detail={"file_name": c.input_file_name, "force_vision": force_vision},
                 request=request)
    db.commit()
    return job


class ContentBody(BaseModel):
    content: str


@router.get("/profiles/{profile_id}/cases/{case_id}/case-text")
def get_case_text(
    profile_id: str,
    case_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    _get_case(db, profile_id, case_id)
    return {"content": _read_case_text(profile_id, case_id) or ""}


@router.put("/profiles/{profile_id}/cases/{case_id}/case-text")
def put_case_text(
    profile_id: str,
    case_id: str,
    body: ContentBody,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    c = _get_case(db, profile_id, case_id)
    path = storage.case_dir(profile_id, case_id) / "case_text.md"
    path.write_text(body.content, encoding="utf-8")
    _advance_status(c, "extracted")
    audit.record(db, user, "case.case_text_edit", profile_id=profile_id, subject_type="case",
                 subject_id=case_id, detail={"chars": len(body.content)}, request=request)
    db.commit()
    return {"ok": True}


# -------------------------------------------------------------------- analysis


@router.post("/profiles/{profile_id}/cases/{case_id}/analyze")
def start_analyze(
    profile_id: str,
    case_id: str,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    _get_case(db, profile_id, case_id)
    cd = storage.case_dir(profile_id, case_id)
    case_text_path = cd / "case_text.md"
    if not case_text_path.is_file():
        raise HTTPException(status_code=400, detail="No case text — run extraction first")
    domain_knowledge = _domain_knowledge(profile_id)
    analysis_path = cd / "analysis.md"

    def run(emit):
        from app.engines.docgen import credit_analysis

        with session_scope() as jdb:
            analysis_model = effective_model(jdb, profile_id, "analysis")
            analysis_prompt = prompt_override(
                jdb, profile_id, "docgen.credit_analysis.prompt")
        case_text = case_text_path.read_text(encoding="utf-8")
        analysis = credit_analysis.analyze_case(
            case_text,
            get_provider(),
            analysis_model,
            domain_knowledge=domain_knowledge,
            emit=emit,
            prompt=analysis_prompt,
        )
        analysis_path.write_text(analysis, encoding="utf-8")
        with session_scope() as jdb:
            row = jdb.get(Case, case_id)
            if row is not None:
                _advance_status(row, "analyzed")
        return {"analysis_chars": len(analysis)}

    try:
        job = runner.submit(
            "case.analyze",
            f"case-analyze:{case_id}",
            run,
            profile_id=profile_id,
            subject_id=case_id,
            user_id=user.id,
        )
    except JobConflict:
        raise HTTPException(status_code=409, detail="Analysis is already running for this case")
    audit.record(db, user, "case.analyze", profile_id=profile_id, subject_type="case",
                 subject_id=case_id, request=request)
    db.commit()
    return job


@router.get("/profiles/{profile_id}/cases/{case_id}/analysis")
def get_analysis(
    profile_id: str,
    case_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    _get_case(db, profile_id, case_id)
    path = storage.case_dir(profile_id, case_id) / "analysis.md"
    return {"content": path.read_text(encoding="utf-8") if path.is_file() else ""}


@router.put("/profiles/{profile_id}/cases/{case_id}/analysis")
def put_analysis(
    profile_id: str,
    case_id: str,
    body: ContentBody,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    c = _get_case(db, profile_id, case_id)
    path = storage.case_dir(profile_id, case_id) / "analysis.md"
    path.write_text(body.content, encoding="utf-8")
    _advance_status(c, "analyzed")
    audit.record(db, user, "case.analysis_edit", profile_id=profile_id, subject_type="case",
                 subject_id=case_id, detail={"chars": len(body.content)}, request=request)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------- selection


@router.post("/profiles/{profile_id}/cases/{case_id}/select")
def start_select(
    profile_id: str,
    case_id: str,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    _get_case(db, profile_id, case_id)
    cd = storage.case_dir(profile_id, case_id)
    case_text_path = cd / "case_text.md"
    if not case_text_path.is_file():
        raise HTTPException(status_code=400, detail="No case text — run extraction first")
    # Descriptors of CURRENT APPROVED versions only; skip empty descriptors.
    descriptors = {
        t.name: v.descriptor_md
        for t, v in usable_templates(db, profile_id)
        if v.descriptor_md and v.descriptor_md.strip()
    }
    if not descriptors:
        raise HTTPException(
            status_code=400,
            detail="No usable templates — a template needs an approved current "
                   "version with a descriptor before selection can run",
        )
    domain_knowledge = _domain_knowledge(profile_id)
    selected_path = cd / "selected_docs.json"
    raw_path = cd / "selector_raw.txt"

    def run(emit):
        from app.engines.docgen import selector

        with session_scope() as jdb:
            selection_model = effective_model(jdb, profile_id, "selection")
            selector_prompt = prompt_override(
                jdb, profile_id, "docgen.selector.prompt")
        case_text = case_text_path.read_text(encoding="utf-8")
        try:
            result = selector.select_documents(
                case_text,
                descriptors,
                get_provider(),
                selection_model,
                domain_knowledge=domain_knowledge,
                emit=emit,
                prompt=selector_prompt,
            )
        except EngineParseError as exc:
            raw_path.write_text(exc.raw, encoding="utf-8")
            raise RuntimeError(
                f"could not parse the selector response ({exc}); "
                f"raw model output saved to {raw_path.name}"
            ) from exc
        selected_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        with session_scope() as jdb:
            row = jdb.get(Case, case_id)
            if row is not None:
                _advance_status(row, "selected")
        return {
            "selected": len(result.get("selected_documents") or []),
            "ambiguous": len(result.get("ambiguous_documents") or []),
        }

    try:
        job = runner.submit(
            "case.select",
            f"case-select:{case_id}",
            run,
            profile_id=profile_id,
            subject_id=case_id,
            user_id=user.id,
        )
    except JobConflict:
        raise HTTPException(status_code=409, detail="Selection is already running for this case")
    audit.record(db, user, "case.select", profile_id=profile_id, subject_type="case",
                 subject_id=case_id, detail={"templates": sorted(descriptors)}, request=request)
    db.commit()
    return job


class SelectedBody(BaseModel):
    content: str | dict


@router.get("/profiles/{profile_id}/cases/{case_id}/selected")
def get_selected(
    profile_id: str,
    case_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    _get_case(db, profile_id, case_id)
    path = storage.case_dir(profile_id, case_id) / "selected_docs.json"
    return {"content": path.read_text(encoding="utf-8") if path.is_file() else ""}


@router.put("/profiles/{profile_id}/cases/{case_id}/selected")
def put_selected(
    profile_id: str,
    case_id: str,
    body: SelectedBody,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    c = _get_case(db, profile_id, case_id)
    if isinstance(body.content, str):
        try:
            data = json.loads(body.content)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}")
    else:
        data = body.content
    _validate_selection(data)
    path = storage.case_dir(profile_id, case_id) / "selected_docs.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _advance_status(c, "selected")
    audit.record(db, user, "case.selected_edit", profile_id=profile_id, subject_type="case",
                 subject_id=case_id,
                 detail={"documents": len(data.get("selected_documents") or [])},
                 request=request)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------------ fill


class FillBody(BaseModel):
    tasks: list[str] | None = None  # precise task_keys; null/omitted = all


def _make_fill_fn(
    *,
    profile_id: str,
    case_id: str,
    template_id: str | None,
    version_no: int | None,
    template_name: str,
    descriptor: str,
    evidence: str,
    entity_scope: str | None,
    instance_label: str,
    user_id: str,
    missing_reason: str | None,
):
    """Bind one fill task's arguments into a job callback (no late binding)."""
    display_name = (
        f"{template_name}__{instance_label}.docx" if instance_label else f"{template_name}.docx"
    )

    def run(emit):
        from app.engines.docgen import fill_agent

        if missing_reason:
            raise RuntimeError(missing_reason)
        case_text = _read_case_text(profile_id, case_id)
        if case_text is None:
            raise RuntimeError("case_text.md not found — run extraction first")
        template_path = storage.template_version_path(profile_id, template_id, version_no)
        if not template_path.is_file():
            raise RuntimeError(
                f"template file for '{template_name}' v{version_no} is missing from storage"
            )
        out_dir = storage.case_output_dir(profile_id, case_id)
        with session_scope() as jdb:
            fill_model = effective_model(jdb, profile_id, "fill")
            fill_prompt = prompt_override(jdb, profile_id, "docgen.fill_agent.prompt")
        try:
            result = fill_agent.fill_document(
                template_path,
                descriptor,
                evidence,
                entity_scope,
                case_text,
                get_provider(),
                fill_model,
                domain_knowledge=_domain_knowledge(profile_id),
                emit=emit,
                prompt=fill_prompt,
            )
        except EngineParseError as exc:
            raw_name = f"{uuid4().hex}.raw.txt"
            (out_dir / raw_name).write_text(exc.raw, encoding="utf-8")
            raise RuntimeError(
                f"could not parse the fill response ({exc}); "
                f"raw model output saved to {raw_name}"
            ) from exc

        doc_id = uuid4().hex
        result.document.save(str(out_dir / f"{doc_id}.docx"))
        provenance = {
            "template": template_name,
            "instance": instance_label or None,
            "entity_scope": entity_scope,
            "applied": result.applied,
            "failed": result.failed,
            "unfilled_fields": result.unfilled_fields,
            "file_name": display_name,
        }
        (out_dir / f"{doc_id}.provenance.json").write_text(
            json.dumps(provenance, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        with session_scope() as jdb:
            jdb.add(
                GeneratedDocument(
                    id=doc_id,
                    case_id=case_id,
                    profile_id=profile_id,
                    template_id=template_id,
                    template_name=template_name,
                    instance_label=instance_label,
                    file_name=display_name,
                    applied_ops=len(result.applied),
                    failed_ops=len(result.failed),
                    unfilled_fields=len(result.unfilled_fields),
                )
            )
            # Approval record starts (and stays) draft until the maker submits.
            ensure_approval(
                jdb,
                profile_id=profile_id,
                subject_type="generated_document",
                subject_id=doc_id,
                maker_id=user_id,
            )
            row = jdb.get(Case, case_id)
            if row is not None:
                active_fills = jdb.execute(
                    select(func.count()).select_from(Job).where(
                        Job.kind == "case.fill",
                        Job.subject_id == case_id,
                        Job.status.in_(JOB_ACTIVE_STATUSES),
                    )
                ).scalar_one()
                # This job's own row is still 'running' here, so <= 1 means
                # it is the last active fill for the case.
                if active_fills <= 1:
                    _advance_status(row, "generated")
        return {
            "document_id": doc_id,
            "file_name": display_name,
            "applied": len(result.applied),
            "failed": len(result.failed),
            "unfilled": len(result.unfilled_fields),
        }

    return run


@router.post("/profiles/{profile_id}/cases/{case_id}/fill")
def start_fill(
    profile_id: str,
    case_id: str,
    request: Request,
    body: FillBody | None = None,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    c = _get_case(db, profile_id, case_id)
    cd = storage.case_dir(profile_id, case_id)
    selected_path = cd / "selected_docs.json"
    if not selected_path.is_file():
        raise HTTPException(status_code=400, detail="No selection — run template selection first")
    try:
        selection = json.loads(selected_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"selected_docs.json is not valid JSON: {exc}")
    if not isinstance(selection, dict):
        raise HTTPException(status_code=400, detail="selected_docs.json must be a JSON object")
    tasks = _expand_tasks(selection)
    if not tasks:
        raise HTTPException(status_code=400, detail="The selection contains no documents to generate")

    skipped: list[dict] = []
    if body and body.tasks is not None:
        known = {t["task_key"] for t in tasks}
        for key in body.tasks:
            if key not in known:
                skipped.append({"task_key": key, "reason": "not in the current selection"})
        wanted = set(body.tasks)
        tasks = [t for t in tasks if t["task_key"] in wanted]

    usable = {t.name: (t, v) for t, v in usable_templates(db, profile_id)}

    submitted: list[dict] = []
    for task in tasks:
        name = task["template_name"]
        template_id: str | None = None
        version_no: int | None = None
        descriptor = ""
        missing_reason: str | None = None
        pair = usable.get(name)
        if pair is None:
            missing_reason = (
                f"template '{name}' is not usable — it needs to be active "
                f"with an approved current version"
            )
        else:
            tmpl, ver = pair
            template_id, version_no = tmpl.id, ver.version_no
            descriptor = ver.descriptor_md or ""
            if not descriptor.strip():
                missing_reason = (
                    f"template '{name}' has no descriptor on its current version — "
                    f"analyze the version or write a descriptor first"
                )
        fn = _make_fill_fn(
            profile_id=profile_id,
            case_id=case_id,
            template_id=template_id,
            version_no=version_no,
            template_name=name,
            descriptor=descriptor,
            evidence=task["evidence"],
            entity_scope=task["entity_scope"],
            instance_label=task["instance_label"],
            user_id=user.id,
            missing_reason=missing_reason,
        )
        try:
            job = runner.submit(
                "case.fill",
                f"case-fill:{case_id}:{task['task_key']}",
                fn,
                profile_id=profile_id,
                subject_id=case_id,
                user_id=user.id,
            )
        except JobConflict:
            skipped.append({"task_key": task["task_key"], "reason": "already running"})
            continue
        submitted.append(
            {"task_key": task["task_key"], "job_id": job["id"], "status": job["status"]}
        )

    if submitted:
        _advance_status(c, "generating")
    audit.record(db, user, "case.fill", profile_id=profile_id, subject_type="case",
                 subject_id=case_id,
                 detail={"submitted": [s["task_key"] for s in submitted],
                         "skipped": [s["task_key"] for s in skipped]},
                 request=request)
    db.commit()
    return {"jobs": submitted, "skipped": skipped}


# ------------------------------------------------------------------- documents


@router.get("/profiles/{profile_id}/cases/{case_id}/documents")
def list_documents(
    profile_id: str,
    case_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    """Generated documents merged with live fill-job state and approval state.

    ``applied_ops`` / ``failed_ops`` / ``unfilled_fields`` are ALWAYS included
    so the UI can flag under-filled documents loudly (ARCHITECTURE bug #8).
    """
    _get_case(db, profile_id, case_id)
    docs = db.execute(
        select(GeneratedDocument)
        .where(GeneratedDocument.case_id == case_id)
        .order_by(GeneratedDocument.created_at)
    ).scalars().all()
    approvals: dict[str, Approval] = {}
    if docs:
        for a in db.execute(
            select(Approval).where(
                Approval.subject_type == "generated_document",
                Approval.subject_id.in_([d.id for d in docs]),
            )
        ).scalars():
            approvals[a.subject_id] = a
    documents = []
    for d in docs:
        a = approvals.get(d.id)
        task_key = (
            f"{d.template_name}__{d.instance_label}" if d.instance_label else d.template_name
        )
        documents.append(
            {
                "id": d.id,
                "task_key": task_key,
                "template_name": d.template_name,
                "instance_label": d.instance_label,
                "file_name": d.file_name,
                "applied_ops": d.applied_ops,
                "failed_ops": d.failed_ops,
                "unfilled_fields": d.unfilled_fields,
                "needs_attention": d.failed_ops > 0 or d.unfilled_fields > 0,
                "approval_state": a.state if a else "draft",
                "approval_id": a.id if a else None,
                "approval_comment": a.comment if a else "",
                "created_at": d.created_at.isoformat(),
            }
        )
    # Fill jobs still queued/running for this case (runner-submitted).
    prefix = f"case-fill:{case_id}:"
    active = db.execute(
        select(Job)
        .where(
            Job.kind == "case.fill",
            Job.subject_id == case_id,
            Job.status.in_(JOB_ACTIVE_STATUSES),
        )
        .order_by(Job.created_at)
    ).scalars().all()
    active_jobs = [
        {
            "job_id": j.id,
            "task_key": j.key[len(prefix):] if j.key.startswith(prefix) else j.key,
            "status": j.status,
            "created_at": j.created_at.isoformat(),
        }
        for j in active
    ]
    return {"documents": documents, "active_jobs": active_jobs}


@router.get("/documents/mine")
def list_my_documents(
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    """This user's own *approved* generated documents, latest approved version
    only — cross-profile by design, for services (e.g. Document Reviewer)
    that mirror a user's own output rather than a profile's. A template
    instance with no approved version yet is omitted entirely, so a
    downstream mirror never sees a document that hasn't cleared maker-checker
    review.

    "Latest version" is per (case_id, template_id, instance_label): a
    regenerated document is a brand-new GeneratedDocument row (no versioning
    exists on this table), so grouping on that triple and keeping the newest
    created_at among the *approved* rows is what tells two runs of the same
    template instance apart from two different template instances, without
    resurfacing a regenerated draft that's still pending its own approval.
    """
    docs = db.execute(
        select(GeneratedDocument, Case.name)
        .join(Case, Case.id == GeneratedDocument.case_id)
        .where(Case.created_by == user.id)
        .order_by(GeneratedDocument.created_at)
    ).all()
    approved_ids = set(
        db.execute(
            select(Approval.subject_id).where(
                Approval.subject_type == "generated_document",
                Approval.subject_id.in_([d.id for d, _ in docs]),
                Approval.state == "approved",
            )
        ).scalars()
    ) if docs else set()
    latest: dict[tuple[str, str | None, str], tuple[GeneratedDocument, str]] = {}
    for d, case_name in docs:
        if d.id not in approved_ids:
            continue
        key = (d.case_id, d.template_id, d.instance_label)
        latest[key] = (d, case_name)  # rows are created_at-ordered, so last write wins
    documents = [
        {
            "logical_key": f"{d.case_id}:{d.template_id or ''}:{d.instance_label}",
            "doc_id": d.id,
            "case_id": d.case_id,
            "profile_id": d.profile_id,
            "case_name": case_name,
            "template_name": d.template_name,
            "instance_label": d.instance_label,
            "file_name": d.file_name,
            "created_at": d.created_at.isoformat(),
        }
        for d, case_name in latest.values()
    ]
    return {"documents": documents}


@router.get("/profiles/{profile_id}/cases/{case_id}/documents/download-all")
def download_all_documents(
    profile_id: str,
    case_id: str,
    approved_only: bool = True,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> Response:
    """In-memory zip of the case's generated documents (approved-only default)."""
    c = _get_case(db, profile_id, case_id)
    docs = db.execute(
        select(GeneratedDocument)
        .where(GeneratedDocument.case_id == case_id)
        .order_by(GeneratedDocument.created_at)
    ).scalars().all()
    if approved_only and docs:
        approved_ids = set(
            db.execute(
                select(Approval.subject_id).where(
                    Approval.subject_type == "generated_document",
                    Approval.subject_id.in_([d.id for d in docs]),
                    Approval.state == "approved",
                )
            ).scalars()
        )
        docs = [d for d in docs if d.id in approved_ids]
    out_dir = storage.case_output_dir(profile_id, case_id)
    entries = [(d, out_dir / f"{d.id}.docx") for d in docs]
    entries = [(d, p) for d, p in entries if p.is_file()]
    if not entries:
        raise HTTPException(
            status_code=404,
            detail="No approved documents to download" if approved_only
                   else "No generated documents to download",
        )
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for d, p in entries:
            arcname = d.file_name or f"{d.id}.docx"
            if arcname in used:  # re-runs can share a display name
                stem = arcname[:-5] if arcname.lower().endswith(".docx") else arcname
                n = 2
                while f"{stem} ({n}).docx" in used:
                    n += 1
                arcname = f"{stem} ({n}).docx"
            used.add(arcname)
            zf.write(p, arcname=arcname)
    safe = "".join(ch if (ch.isalnum() or ch in " -_") else "_" for ch in c.name).strip() or case_id
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe} - documents.zip"'},
    )


@router.post("/profiles/{profile_id}/cases/{case_id}/documents/{doc_id}/submit")
def submit_document(
    profile_id: str,
    case_id: str,
    doc_id: str,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    _get_case(db, profile_id, case_id)
    d = _get_document(db, profile_id, case_id, doc_id)
    approval = ensure_approval(
        db,
        profile_id=profile_id,
        subject_type="generated_document",
        subject_id=doc_id,
        maker_id=user.id,
    )
    submit_approval(
        db,
        approval,
        user,
        title=f"{d.template_name} ({d.file_name})",
        link=f"/p/{profile_id}/cases/{case_id}",
    )
    audit.record(db, user, "case.document_submit", profile_id=profile_id,
                 subject_type="generated_document", subject_id=doc_id,
                 detail={"file_name": d.file_name,
                         "failed_ops": d.failed_ops,
                         "unfilled_fields": d.unfilled_fields},
                 request=request)
    db.commit()
    return {"ok": True, "approval_id": approval.id, "state": approval.state}


@router.get("/profiles/{profile_id}/cases/{case_id}/documents/{doc_id}/download")
def download_document(
    profile_id: str,
    case_id: str,
    doc_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
):
    _get_case(db, profile_id, case_id)
    d = _get_document(db, profile_id, case_id, doc_id)
    path = storage.case_output_dir(profile_id, case_id) / f"{d.id}.docx"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File missing from storage")
    return FileResponse(path, filename=d.file_name, media_type=DOCX_MEDIA_TYPE)


@router.get("/profiles/{profile_id}/cases/{case_id}/documents/{doc_id}/provenance")
def get_provenance(
    profile_id: str,
    case_id: str,
    doc_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    _get_case(db, profile_id, case_id)
    d = _get_document(db, profile_id, case_id, doc_id)
    path = storage.case_output_dir(profile_id, case_id) / f"{d.id}.provenance.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No provenance recorded for this document")
    return json.loads(path.read_text(encoding="utf-8"))
