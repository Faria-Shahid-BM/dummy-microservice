"""Request-scoped auth dependencies: session resolution and RBAC guards.

Single-organization model: ``User.org_role`` (maker | checker) applies across
all profiles; ``User.can_edit_config`` gates configuration changes; admins
pass everything. The seeded Default profile is read-only — mutation guards
reject it so work and config changes happen in user-created profiles.
"""

import os
from datetime import timedelta

import jwt
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import db_session
from app.core.security import SESSION_COOKIE, token_digest
from app.models import AuthSession, Profile, User, utcnow

# --- Kong JWT (A1: this service trusts the gateway's token) ---
JWT_SECRET = os.environ.get("JWT_SECRET", "mysecret123")
JWT_ISSUER = os.environ.get("JWT_ISSUER", "poc-issuer")
# A token needs one of these to use docgen. "docgen_check" counts on its own:
# it designates the checker who approves docgen work, so a token carrying only
# it (issued before auth-service started implying "docgen") must still get in —
# otherwise the checker is locked out of the very thing they approve.
DOCGEN_SCOPES = {"docgen", "docgen_check", "admin"}


# last_seen writes are throttled to once a minute to avoid write amplification
_LAST_SEEN_GRANULARITY = timedelta(minutes=1)

DEFAULT_PROFILE_READONLY = (
    "The Default profile is read-only — create a profile to work in"
)


def resolve_session(request: Request, db: Session) -> tuple[AuthSession, User] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    sess = db.execute(
        select(AuthSession).where(AuthSession.token_hash == token_digest(token))
    ).scalar_one_or_none()
    if sess is None:
        return None
    now = utcnow()
    expires = sess.expires_at
    if expires.tzinfo is None:  # SQLite round-trips naive datetimes
        from datetime import timezone

        expires = expires.replace(tzinfo=timezone.utc)
    if now >= expires:
        db.delete(sess)
        db.commit()
        return None
    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        return None
    last_seen = sess.last_seen_at
    if last_seen.tzinfo is None:
        from datetime import timezone

        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if now - last_seen > _LAST_SEEN_GRANULARITY:
        sess.last_seen_at = now
        db.commit()
    return sess, user


def current_session(
    request: Request, db: Session = Depends(db_session)
) -> tuple[AuthSession, User]:
    resolved = resolve_session(request, db)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return resolved

def current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(db_session),
) -> User:
    """Resolve the caller from Kong's JWT and map it to a local User row."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], issuer=JWT_ISSUER)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")

    scopes = set(claims.get("scopes") or [])
    if not (DOCGEN_SCOPES & scopes):
        raise HTTPException(status_code=403, detail="Insufficient scope: 'docgen' required")

    username = claims.get("sub") or "unknown"
    is_admin = "admin" in scopes
    org_role = "checker" if "docgen_check" in scopes else "maker"

    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None:
        user = User(username=username, display_name=username, auth_provider="jwt",
                    org_role=org_role, is_admin=is_admin, can_edit_config=is_admin, is_active=True)
        db.add(user); db.commit(); db.refresh(user)
    elif (user.is_admin, user.org_role) != (is_admin, org_role):
        user.is_admin, user.org_role = is_admin, org_role
        if is_admin: user.can_edit_config = True
        db.commit()
    return user



def effective_role(user: User) -> str:
    """'admin' | 'maker' | 'checker' — the user's organization-wide role."""
    return "admin" if user.is_admin else user.org_role


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def require_config_editor(user: User = Depends(current_user)) -> User:
    if not (user.is_admin or user.can_edit_config):
        raise HTTPException(status_code=403, detail="Configuration access required")
    return user


def get_profile_or_404(db: Session, profile_id: str) -> Profile:
    profile = db.get(Profile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


def require_profile_role(*roles: str):
    """Guard factory for routes with a ``profile_id`` path parameter.

    Verifies the profile exists; admins always pass. With role arguments the
    guard is a MUTATION guard: it additionally rejects the read-only Default
    profile. ``require_profile_role()`` (no args) = any signed-in user (view).
    Returns the effective role.
    """

    def guard(
        profile_id: str,
        user: User = Depends(current_user),
        db: Session = Depends(db_session),
    ) -> str:
        profile = get_profile_or_404(db, profile_id)
        role = effective_role(user)
        if roles:
            if profile.is_default:
                raise HTTPException(status_code=403, detail=DEFAULT_PROFILE_READONLY)
            if not user.is_admin and role not in roles:
                raise HTTPException(
                    status_code=403, detail="Insufficient role for this action"
                )
        return role

    return guard


require_profile_member = require_profile_role()
require_profile_maker = require_profile_role("maker")
require_profile_checker = require_profile_role("checker")
