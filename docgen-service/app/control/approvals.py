"""Maker-checker approvals: the dual-control state machine.

State machine: draft → pending → approved | rejected; rejected → pending
(resubmit). The one rule that matters is enforced here and only here:
**the decider can never be the maker of the same approval** — not even an
admin. Subject-specific behavior plugs in via two registries so this module
stays generic:

- ``SUBJECT_RESOLVERS[subject_type](db, subject_id) -> dict`` — display info.
- ``APPROVAL_EFFECTS[subject_type](db, approval) -> None`` — applied exactly
  once when the approval transitions to ``approved`` (e.g. a template version
  becoming current).
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.auth.deps import current_user, effective_role, require_profile_member
from app.core.db import db_session
from app.models import Approval, ROLE_CHECKER, ROLE_MAKER, User, utcnow
from app.notify import notify, notify_profile_role

router = APIRouter(prefix="/api", tags=["approvals"])

SUBJECT_RESOLVERS: dict[str, Callable[[Session, str], dict]] = {}
APPROVAL_EFFECTS: dict[str, Callable[[Session, Approval], None]] = {}


def ensure_approval(
    db: Session, *, profile_id: str, subject_type: str, subject_id: str, maker_id: str
) -> Approval:
    """Get or create the approval record for a subject (draft)."""
    approval = db.execute(
        select(Approval).where(
            Approval.subject_type == subject_type, Approval.subject_id == subject_id
        )
    ).scalar_one_or_none()
    if approval is None:
        approval = Approval(
            profile_id=profile_id,
            subject_type=subject_type,
            subject_id=subject_id,
            maker_id=maker_id,
        )
        db.add(approval)
        db.flush()
    return approval


def submit_approval(
    db: Session, approval: Approval, user: User, *, title: str, link: str | None = None
) -> Approval:
    """draft/rejected → pending; notifies the organization's checkers."""
    if approval.state not in ("draft", "rejected"):
        raise HTTPException(status_code=409, detail=f"Cannot submit from state '{approval.state}'")
    approval.state = "pending"
    approval.maker_id = user.id
    approval.submitted_at = utcnow()
    approval.checker_id = None
    approval.decided_at = None
    notify_profile_role(
        db,
        approval.profile_id,
        ROLE_CHECKER,
        type="approval.pending",
        title=f"Review requested: {title}",
        link=link,
        exclude_user_id=user.id,
    )
    return approval


def _payload(db: Session, a: Approval) -> dict:
    resolver = SUBJECT_RESOLVERS.get(a.subject_type)
    subject = resolver(db, a.subject_id) if resolver else {}
    maker = db.get(User, a.maker_id)
    checker = db.get(User, a.checker_id) if a.checker_id else None
    return {
        "id": a.id,
        "profile_id": a.profile_id,
        "subject_type": a.subject_type,
        "subject_id": a.subject_id,
        "subject": subject,
        "state": a.state,
        "maker": maker.display_name or maker.username if maker else a.maker_id,
        "maker_id": a.maker_id,
        "checker": (checker.display_name or checker.username) if checker else None,
        "comment": a.comment,
        "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
    }


# ----------------------------------------------------------------------- routes


@router.get("/profiles/{profile_id}/approvals")
def list_approvals(
    profile_id: str,
    state: str | None = None,
    subject_type: str | None = None,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    q = select(Approval).where(Approval.profile_id == profile_id).order_by(
        Approval.submitted_at.desc().nulls_last()
    )
    if state:
        q = q.where(Approval.state == state)
    if subject_type:
        q = q.where(Approval.subject_type == subject_type)
    return {"approvals": [_payload(db, a) for a in db.execute(q).scalars().all()]}


@router.post("/approvals/{approval_id}/submit")
def submit(
    approval_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    a = db.get(Approval, approval_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    role = effective_role(user)
    if role not in ("admin", ROLE_MAKER):
        raise HTTPException(status_code=403, detail="Only makers can submit for review")
    resolver = SUBJECT_RESOLVERS.get(a.subject_type)
    subject = resolver(db, a.subject_id) if resolver else {}
    submit_approval(db, a, user, title=subject.get("name", a.subject_type), link=subject.get("link"))
    audit.record(db, user, "approval.submit", profile_id=a.profile_id,
                 subject_type=a.subject_type, subject_id=a.subject_id, request=request)
    db.commit()
    return _payload(db, a)


class DecideBody(BaseModel):
    approve: bool
    comment: str = ""


@router.post("/approvals/{approval_id}/decide")
def decide(
    approval_id: str,
    body: DecideBody,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    a = db.get(Approval, approval_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    role = effective_role(user)
    if role not in ("admin", ROLE_CHECKER):
        raise HTTPException(status_code=403, detail="Only checkers can decide reviews")
    if a.state != "pending":
        raise HTTPException(status_code=409, detail=f"Cannot decide from state '{a.state}'")
    # The maker-checker rule. No exception for admins: four eyes means four eyes.
    if a.maker_id == user.id:
        raise HTTPException(
            status_code=403, detail="Maker-checker control: you cannot review your own work"
        )
    if not body.approve and not body.comment.strip():
        raise HTTPException(status_code=422, detail="A comment is required when rejecting")

    a.state = "approved" if body.approve else "rejected"
    a.checker_id = user.id
    a.comment = body.comment.strip()
    a.decided_at = utcnow()
    if body.approve:
        effect = APPROVAL_EFFECTS.get(a.subject_type)
        if effect:
            effect(db, a)

    resolver = SUBJECT_RESOLVERS.get(a.subject_type)
    subject = resolver(db, a.subject_id) if resolver else {}
    notify(
        db,
        a.maker_id,
        type="approval.decided",
        title=f"{'Approved' if body.approve else 'Rejected'}: {subject.get('name', a.subject_type)}",
        body=a.comment,
        link=subject.get("link"),
    )
    audit.record(db, user, "approval.approve" if body.approve else "approval.reject",
                 profile_id=a.profile_id, subject_type=a.subject_type,
                 subject_id=a.subject_id, detail={"comment": a.comment}, request=request)
    db.commit()
    return _payload(db, a)
