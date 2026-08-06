"""Profiles (configuration sets) and their domain-knowledge policy text.

Single organization: every signed-in user sees every profile. Creating and
editing profiles — including the domain-knowledge policy, which is
configuration — requires the config-editor capability. The seeded Default
profile is the read-only factory baseline and rejects all mutations.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, storage
from app.auth import deps
from app.auth.deps import (
    current_user,
    effective_role,
    get_profile_or_404,
    require_config_editor,
    require_profile_member,
)
from app.core.db import db_session
from app.models import Profile, User

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


def _profile_payload(p: Profile, role: str | None = None) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "is_default": p.is_default,
        "created_at": p.created_at.isoformat(),
        "role": role,
    }


@router.get("")
def list_profiles(user: User = Depends(current_user), db: Session = Depends(db_session)) -> dict:
    rows = db.execute(select(Profile).order_by(Profile.name)).scalars().all()
    role = effective_role(user)
    return {"profiles": [_profile_payload(p, role) for p in rows]}


class ProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""


@router.post("", status_code=201)
def create_profile(
    body: ProfileBody,
    request: Request,
    user: User = Depends(require_config_editor),
    db: Session = Depends(db_session),
) -> dict:
    if db.execute(select(Profile).where(Profile.name == body.name)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A profile with that name already exists")
    p = Profile(name=body.name, description=body.description)
    db.add(p)
    db.flush()
    audit.record(db, user, "profile.create", profile_id=p.id, subject_type="profile",
                 subject_id=p.id, detail={"name": p.name}, request=request)
    db.commit()
    return _profile_payload(p, effective_role(user))


@router.get("/{profile_id}")
def get_profile(
    profile_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    p = get_profile_or_404(db, profile_id)
    return _profile_payload(p, role)


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


@router.patch("/{profile_id}")
def update_profile(
    profile_id: str,
    body: ProfilePatch,
    request: Request,
    user: User = Depends(require_config_editor),
    db: Session = Depends(db_session),
) -> dict:
    p = get_profile_or_404(db, profile_id)
    if p.is_default:
        raise HTTPException(status_code=403, detail=deps.DEFAULT_PROFILE_READONLY)
    changes: dict = {}
    if body.name is not None and body.name != p.name:
        clash = db.execute(select(Profile).where(Profile.name == body.name)).scalar_one_or_none()
        if clash:
            raise HTTPException(status_code=409, detail="A profile with that name already exists")
        changes["name"] = {"from": p.name, "to": body.name}
        p.name = body.name
    if body.description is not None and body.description != p.description:
        changes["description"] = "updated"
        p.description = body.description
    if changes:
        audit.record(db, user, "profile.update", profile_id=p.id, subject_type="profile",
                     subject_id=p.id, detail=changes, request=request)
    db.commit()
    return _profile_payload(p, effective_role(user))


# ------------------------------------------------------- domain knowledge (policy)


@router.get("/{profile_id}/policy")
def get_policy(
    profile_id: str,
    role: str = Depends(require_profile_member),
) -> dict:
    path = storage.profile_dir(profile_id) / "domain_knowledge.md"
    return {"content": path.read_text(encoding="utf-8") if path.exists() else ""}


class PolicyBody(BaseModel):
    content: str


@router.put("/{profile_id}/policy")
def put_policy(
    profile_id: str,
    body: PolicyBody,
    request: Request,
    user: User = Depends(require_config_editor),
    db: Session = Depends(db_session),
) -> dict:
    p = get_profile_or_404(db, profile_id)
    if p.is_default:
        raise HTTPException(status_code=403, detail=deps.DEFAULT_PROFILE_READONLY)
    path = storage.profile_dir(profile_id) / "domain_knowledge.md"
    path.write_text(body.content, encoding="utf-8")
    audit.record(db, user, "profile.policy_update", profile_id=profile_id,
                 detail={"chars": len(body.content)}, request=request)
    db.commit()
    return {"ok": True}
