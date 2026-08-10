"""Audit trail: every mutating action records who did what, where, to what.

``record()`` adds to the caller's session (committed with the caller's
transaction, so audit entries never describe work that was rolled back).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import current_user
from app.core.db import db_session
from app.models import AuditEntry, User

router = APIRouter(prefix="/api/audit", tags=["audit"])


def record(
    db: Session,
    user: User | None,
    action: str,
    *,
    profile_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    detail: dict | None = None,
    request: Request | None = None,
) -> None:
    db.add(
        AuditEntry(
            user_id=user.id if user else None,
            username=user.username if user else None,
            profile_id=profile_id,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            detail=detail,
            ip=request.client.host if request and request.client else None,
        )
    )


@router.get("")
def list_audit(
    profile_id: str | None = None,
    action: str | None = None,
    limit: int = 200,
    offset: int = 0,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    """Single organization: any authenticated user may list the trail —
    it is an internal control surface."""
    q = select(AuditEntry).order_by(AuditEntry.ts.desc())
    if profile_id:
        q = q.where(AuditEntry.profile_id == profile_id)
    if action:
        q = q.where(AuditEntry.action == action)
    rows = db.execute(q.limit(min(limit, 500)).offset(offset)).scalars().all()
    return {
        "entries": [
            {
                "id": e.id,
                "ts": e.ts.isoformat(),
                "user_id": e.user_id,
                "username": e.username,
                "profile_id": e.profile_id,
                "action": e.action,
                "subject_type": e.subject_type,
                "subject_id": e.subject_id,
                "detail": e.detail,
                "ip": e.ip,
            }
            for e in rows
        ]
    }
