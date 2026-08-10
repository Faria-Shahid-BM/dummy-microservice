"""In-app notifications behind a Notifier abstraction.

The abstraction exists so SMTP/Exchange delivery can be added later as a
config-time adapter without touching call sites: modules call ``notify(...)``
and every configured notifier fans out.
"""
from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.deps import current_user
from app.core.db import db_session
from app.models import Notification, User, utcnow

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class Notifier(Protocol):
    def send(
        self, db: Session, user_id: str, *, type: str, title: str, body: str, link: str | None
    ) -> None: ...


class InAppNotifier:
    def send(
        self, db: Session, user_id: str, *, type: str, title: str, body: str = "", link: str | None = None
    ) -> None:
        db.add(Notification(user_id=user_id, type=type, title=title, body=body, link=link))


_notifiers: list[Notifier] = [InAppNotifier()]


def notify(
    db: Session, user_id: str, *, type: str, title: str, body: str = "", link: str | None = None
) -> None:
    for n in _notifiers:
        n.send(db, user_id, type=type, title=title, body=body, link=link)


def notify_profile_role(
    db: Session,
    profile_id: str,
    role: str,
    *,
    type: str,
    title: str,
    body: str = "",
    link: str | None = None,
    exclude_user_id: str | None = None,
) -> None:
    """Notify every active user holding org-wide ``role`` (e.g. all checkers).

    ``profile_id`` is kept for call-site compatibility and future scoping;
    roles are organization-wide now, so it does not filter recipients.
    """
    users = db.execute(
        select(User).where(User.org_role == role, User.is_active.is_(True))
    ).scalars()
    for u in users:
        if u.id != exclude_user_id:
            notify(db, u.id, type=type, title=title, body=body, link=link)


# ----------------------------------------------------------------------- routes


@router.get("")
def list_notifications(
    unread: bool = False,
    limit: int = 50,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    q = (
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.ts.desc())
        .limit(min(limit, 200))
    )
    if unread:
        q = q.where(Notification.read_at.is_(None))
    rows = db.execute(q).scalars().all()
    unread_count = len(
        db.execute(
            select(Notification.id).where(
                Notification.user_id == user.id, Notification.read_at.is_(None)
            )
        ).all()
    )
    return {
        "unread_count": unread_count,
        "notifications": [
            {
                "id": n.id,
                "ts": n.ts.isoformat(),
                "type": n.type,
                "title": n.title,
                "body": n.body,
                "link": n.link,
                "read": n.read_at is not None,
            }
            for n in rows
        ],
    }


class MarkReadBody(BaseModel):
    ids: list[str] | None = None  # None = mark all read


@router.post("/read")
def mark_read(
    body: MarkReadBody,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    q = (
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=utcnow())
    )
    if body.ids:
        q = q.where(Notification.id.in_(body.ids))
    db.execute(q)
    db.commit()
    return {"ok": True}
