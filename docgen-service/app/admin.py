"""Administration: user management (single-organization roles)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import audit
from app.auth.deps import require_admin
from app.core.db import db_session
from app.core.security import hash_password
from app.models import AuthSession, ROLE_CHECKER, ROLE_MAKER, User

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

_ROLE_PATTERN = f"^({ROLE_MAKER}|{ROLE_CHECKER})$"


def _user_payload(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "display_name": u.display_name,
        "auth_provider": u.auth_provider,
        "org_role": u.org_role,
        "can_edit_config": u.can_edit_config,
        "is_admin": u.is_admin,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat(),
    }


@router.get("/users")
def list_users(db: Session = Depends(db_session)) -> dict:
    users = db.execute(select(User).order_by(User.username)).scalars().all()
    return {"users": [_user_payload(u) for u in users]}


class CreateUserBody(BaseModel):
    username: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=10, max_length=200)
    display_name: str = ""
    email: str | None = None
    org_role: str = Field(default=ROLE_MAKER, pattern=_ROLE_PATTERN)
    can_edit_config: bool = False
    is_admin: bool = False


@router.post("/users", status_code=201)
def create_user(
    body: CreateUserBody,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(db_session),
) -> dict:
    if db.execute(select(User).where(User.username == body.username)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")
    u = User(
        username=body.username,
        display_name=body.display_name or body.username,
        email=body.email,
        password_hash=hash_password(body.password),
        auth_provider="local",
        org_role=body.org_role,
        can_edit_config=body.can_edit_config,
        is_admin=body.is_admin,
    )
    db.add(u)
    db.flush()
    audit.record(db, admin, "admin.user_create", subject_type="user", subject_id=u.id,
                 detail={"username": u.username, "org_role": u.org_role,
                         "can_edit_config": u.can_edit_config, "is_admin": u.is_admin},
                 request=request)
    db.commit()
    return _user_payload(u)


class PatchUserBody(BaseModel):
    display_name: str | None = None
    email: str | None = None
    org_role: str | None = Field(default=None, pattern=_ROLE_PATTERN)
    can_edit_config: bool | None = None
    is_admin: bool | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=200)


@router.patch("/users/{user_id}")
def patch_user(
    user_id: str,
    body: PatchUserBody,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(db_session),
) -> dict:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    changes: dict = {}
    if body.display_name is not None:
        u.display_name = body.display_name
        changes["display_name"] = body.display_name
    if body.email is not None:
        u.email = body.email
        changes["email"] = body.email
    if body.org_role is not None and body.org_role != u.org_role:
        u.org_role = body.org_role
        changes["org_role"] = body.org_role
    if body.can_edit_config is not None and body.can_edit_config != u.can_edit_config:
        u.can_edit_config = body.can_edit_config
        changes["can_edit_config"] = body.can_edit_config
    if body.is_admin is not None and body.is_admin != u.is_admin:
        if u.id == admin.id and not body.is_admin:
            raise HTTPException(status_code=400, detail="You cannot remove your own admin role")
        u.is_admin = body.is_admin
        changes["is_admin"] = body.is_admin
    if body.is_active is not None and body.is_active != u.is_active:
        if u.id == admin.id and not body.is_active:
            raise HTTPException(status_code=400, detail="You cannot deactivate yourself")
        u.is_active = body.is_active
        changes["is_active"] = body.is_active
        if not body.is_active:  # kill live sessions on deactivation
            db.execute(delete(AuthSession).where(AuthSession.user_id == u.id))
    if body.password is not None:
        if u.auth_provider != "local":
            raise HTTPException(status_code=400, detail="SSO users have no local password")
        u.password_hash = hash_password(body.password)
        changes["password"] = "reset"
        db.execute(delete(AuthSession).where(AuthSession.user_id == u.id))
    if changes:
        audit.record(db, admin, "admin.user_update", subject_type="user", subject_id=u.id,
                     detail=changes, request=request)
    db.commit()
    return _user_payload(u)
