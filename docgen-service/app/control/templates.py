"""Versioned template library.

Every template version is immutable once uploaded; a version only becomes
*current* (used for generation) when a checker approves it — uploading is
submitting for review. Superseded versions are kept forever (audit trail);
templates are archived, never deleted.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, storage
from app.auth.deps import current_user, require_profile_maker, require_profile_member
from app.control.approvals import (
    APPROVAL_EFFECTS,
    SUBJECT_RESOLVERS,
    ensure_approval,
    submit_approval,
)
from app.core.db import db_session, session_scope
from app.jobs.runner import JobConflict, runner
from app.llm.registry import get_provider
from app.models import Approval, Template, TemplateVersion, User

router = APIRouter(prefix="/api", tags=["templates"])

LANGUAGES = ("en", "ar", "bilingual")


# ------------------------------------------------------------ approval plumbing


def _resolve_version_subject(db: Session, version_id: str) -> dict:
    v = db.get(TemplateVersion, version_id)
    if v is None:
        return {"name": "(deleted version)"}
    t = db.get(Template, v.template_id)
    return {
        "name": f"{t.name if t else '?'} — v{v.version_no}",
        "link": f"/p/{t.profile_id}/templates/{t.id}" if t else None,
        "template_id": v.template_id,
        "version_no": v.version_no,
    }


def _apply_version_approval(db: Session, approval: Approval) -> None:
    """Approved version becomes current; previous current is superseded."""
    v = db.get(TemplateVersion, approval.subject_id)
    if v is None:
        return
    for other in db.execute(
        select(TemplateVersion).where(TemplateVersion.template_id == v.template_id)
    ).scalars():
        other.is_current = other.id == v.id


SUBJECT_RESOLVERS["template_version"] = _resolve_version_subject
APPROVAL_EFFECTS["template_version"] = _apply_version_approval


# -------------------------------------------------------------------- payloads


def _approval_state(db: Session, version_id: str) -> str | None:
    a = db.execute(
        select(Approval).where(
            Approval.subject_type == "template_version", Approval.subject_id == version_id
        )
    ).scalar_one_or_none()
    return a.state if a else None


def _version_payload(db: Session, v: TemplateVersion) -> dict:
    return {
        "id": v.id,
        "version_no": v.version_no,
        "file_name": v.file_name,
        "note": v.note,
        "has_descriptor": bool(v.descriptor_md),
        "is_current": v.is_current,
        "approval_state": _approval_state(db, v.id),
        "created_at": v.created_at.isoformat(),
    }


def _template_payload(db: Session, t: Template, with_versions: bool = False) -> dict:
    current = next((v for v in t.versions if v.is_current), None)
    out = {
        "id": t.id,
        "name": t.name,
        "language": t.language,
        "status": t.status,
        "created_at": t.created_at.isoformat(),
        "current_version_no": current.version_no if current else None,
        "version_count": len(t.versions),
    }
    if with_versions:
        out["versions"] = [_version_payload(db, v) for v in reversed(t.versions)]
    return out


def current_version(db: Session, template: Template) -> TemplateVersion | None:
    return next((v for v in template.versions if v.is_current), None)


def usable_templates(db: Session, profile_id: str) -> list[tuple[Template, TemplateVersion]]:
    """Active templates with an approved current version — what docgen may use."""
    out: list[tuple[Template, TemplateVersion]] = []
    for t in db.execute(
        select(Template).where(Template.profile_id == profile_id, Template.status == "active")
    ).scalars():
        v = current_version(db, t)
        if v is not None:
            out.append((t, v))
    return out


# ----------------------------------------------------------------------- routes


@router.get("/profiles/{profile_id}/templates")
def list_templates(
    profile_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    rows = db.execute(
        select(Template).where(Template.profile_id == profile_id).order_by(Template.name)
    ).scalars().all()
    return {"templates": [_template_payload(db, t) for t in rows]}


async def _add_version(
    db: Session,
    user: User,
    request: Request,
    template: Template,
    file: UploadFile,
    note: str,
) -> TemplateVersion:
    version_no = max((v.version_no for v in template.versions), default=0) + 1
    v = TemplateVersion(
        template_id=template.id,
        version_no=version_no,
        file_name=storage.sanitize_display_name(file.filename or f"v{version_no}.docx"),
        note=note,
        uploaded_by=user.id,
    )
    db.add(v)
    db.flush()
    dest = storage.template_version_path(template.profile_id, template.id, version_no)
    await storage.save_upload(file, dest, allowed_suffixes={".docx"})
    approval = ensure_approval(
        db,
        profile_id=template.profile_id,
        subject_type="template_version",
        subject_id=v.id,
        maker_id=user.id,
    )
    submit_approval(
        db, approval, user,
        title=f"{template.name} — v{version_no}",
        link=f"/p/{template.profile_id}/templates/{template.id}",
    )
    audit.record(db, user, "template.version_upload", profile_id=template.profile_id,
                 subject_type="template_version", subject_id=v.id,
                 detail={"template": template.name, "version_no": version_no,
                         "file_name": v.file_name}, request=request)
    return v


@router.post("/profiles/{profile_id}/templates", status_code=201)
async def create_template(
    profile_id: str,
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(""),
    language: str = Form("en"),
    note: str = Form(""),
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    if language not in LANGUAGES:
        raise HTTPException(status_code=422, detail=f"language must be one of {LANGUAGES}")
    display_name = name.strip() or storage.sanitize_display_name(file.filename or "Template").rsplit(".", 1)[0]
    clash = db.execute(
        select(Template).where(Template.profile_id == profile_id, Template.name == display_name)
    ).scalar_one_or_none()
    if clash:
        raise HTTPException(status_code=409, detail="A template with that name already exists")
    t = Template(profile_id=profile_id, name=display_name, language=language, created_by=user.id)
    db.add(t)
    db.flush()
    await _add_version(db, user, request, t, file, note)
    db.commit()
    db.refresh(t)
    return _template_payload(db, t, with_versions=True)


@router.get("/profiles/{profile_id}/templates/{template_id}")
def get_template(
    profile_id: str,
    template_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    t = db.get(Template, template_id)
    if t is None or t.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Template not found")
    return _template_payload(db, t, with_versions=True)


class TemplatePatch(BaseModel):
    name: str | None = None
    language: str | None = None
    status: str | None = None  # active | archived


@router.patch("/profiles/{profile_id}/templates/{template_id}")
def patch_template(
    profile_id: str,
    template_id: str,
    body: TemplatePatch,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    t = db.get(Template, template_id)
    if t is None or t.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Template not found")
    changes: dict = {}
    if body.name and body.name != t.name:
        changes["name"] = {"from": t.name, "to": body.name}
        t.name = body.name
    if body.language:
        if body.language not in LANGUAGES:
            raise HTTPException(status_code=422, detail=f"language must be one of {LANGUAGES}")
        changes["language"] = body.language
        t.language = body.language
    if body.status:
        if body.status not in ("active", "archived"):
            raise HTTPException(status_code=422, detail="status must be active or archived")
        changes["status"] = body.status
        t.status = body.status
    if changes:
        audit.record(db, user, "template.update", profile_id=profile_id,
                     subject_type="template", subject_id=t.id, detail=changes, request=request)
    db.commit()
    return _template_payload(db, t, with_versions=True)


@router.post("/profiles/{profile_id}/templates/{template_id}/versions", status_code=201)
async def upload_version(
    profile_id: str,
    template_id: str,
    request: Request,
    file: UploadFile = File(...),
    note: str = Form(""),
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    t = db.get(Template, template_id)
    if t is None or t.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Template not found")
    if t.status != "active":
        raise HTTPException(status_code=409, detail="Template is archived")
    v = await _add_version(db, user, request, t, file, note)
    db.commit()
    return _version_payload(db, v)


@router.get("/profiles/{profile_id}/templates/{template_id}/versions/{version_id}/file")
def download_version(
    profile_id: str,
    template_id: str,
    version_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
):
    v = db.get(TemplateVersion, version_id)
    if v is None or v.template_id != template_id:
        raise HTTPException(status_code=404, detail="Version not found")
    t = db.get(Template, template_id)
    if t is None or t.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Template not found")
    path = storage.template_version_path(profile_id, template_id, v.version_no)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing from storage")
    return FileResponse(path, filename=v.file_name)


# ------------------------------------------------------------------ descriptor


@router.post("/profiles/{profile_id}/templates/{template_id}/versions/{version_id}/analyze")
def analyze_version(
    profile_id: str,
    template_id: str,
    version_id: str,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    v = db.get(TemplateVersion, version_id)
    t = db.get(Template, template_id)
    if v is None or t is None or v.template_id != template_id or t.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Version not found")
    path = storage.template_version_path(profile_id, template_id, v.version_no)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing from storage")

    dk_path = storage.profile_dir(profile_id) / "domain_knowledge.md"
    domain_knowledge = dk_path.read_text(encoding="utf-8") if dk_path.exists() else ""

    def run(emit):
        from app.control.profile_config import effective_model, prompt_override
        from app.engines.docgen import meta_analyzer

        # Per-profile config resolved at run time (fresh session).
        with session_scope() as jdb:
            analysis_model = effective_model(jdb, profile_id, "analysis")
            analyzer_prompt = prompt_override(
                jdb, profile_id, "docgen.meta_analyzer.prompt")
        descriptor = meta_analyzer.analyze_template(
            path,
            get_provider(),
            analysis_model,
            domain_knowledge=domain_knowledge,
            emit=emit,
            prompt=analyzer_prompt,
        )
        with session_scope() as jdb:
            row = jdb.get(TemplateVersion, version_id)
            if row is not None:
                row.descriptor_md = descriptor
        return {"descriptor_chars": len(descriptor)}

    try:
        job = runner.submit(
            "template.analyze",
            f"template-analyze:{version_id}",
            run,
            profile_id=profile_id,
            subject_id=version_id,
            user_id=user.id,
        )
    except JobConflict:
        raise HTTPException(status_code=409, detail="Analysis already running for this version")
    audit.record(db, user, "template.analyze", profile_id=profile_id,
                 subject_type="template_version", subject_id=version_id, request=request)
    db.commit()
    return job


@router.get("/profiles/{profile_id}/templates/{template_id}/versions/{version_id}/descriptor")
def get_descriptor(
    profile_id: str,
    template_id: str,
    version_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    v = db.get(TemplateVersion, version_id)
    if v is None or v.template_id != template_id:
        raise HTTPException(status_code=404, detail="Version not found")
    return {"descriptor": v.descriptor_md}


class DescriptorBody(BaseModel):
    descriptor: str


@router.put("/profiles/{profile_id}/templates/{template_id}/versions/{version_id}/descriptor")
def put_descriptor(
    profile_id: str,
    template_id: str,
    version_id: str,
    body: DescriptorBody,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    v = db.get(TemplateVersion, version_id)
    t = db.get(Template, template_id)
    if v is None or t is None or v.template_id != template_id or t.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Version not found")
    v.descriptor_md = body.descriptor
    audit.record(db, user, "template.descriptor_edit", profile_id=profile_id,
                 subject_type="template_version", subject_id=version_id,
                 detail={"chars": len(body.descriptor)}, request=request)
    db.commit()
    return {"ok": True}
