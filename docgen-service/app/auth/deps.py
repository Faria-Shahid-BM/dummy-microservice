"""Request-scoped auth dependencies: RBAC guards over a centrally-issued JWT.

Single-organization model: ``User.org_role`` (maker | checker) applies across
all profiles; admins pass everything. Both are recomputed from the token's
``scopes`` on every request (see ``current_user()``) — auth-service is the
one place either is actually granted or revoked. The seeded Default profile
is read-only — mutation guards reject it so work and config changes happen
in user-created profiles.
"""

import os
from pathlib import Path

import jwt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import db_session
from app.models import Profile, User

# --- Kong JWT (A1: this service trusts the gateway's token) ---
# RS256: auth-service signs with its private key; this service only ever
# needs the public key to verify, matching every other service's
# security.py — no shared secret to leak.
JWT_ISSUER = os.environ.get("JWT_ISSUER", "poc-issuer")
JWT_ALGORITHM = "RS256"
JWT_PUBLIC_KEY_PATH = os.environ.get("JWT_PUBLIC_KEY_PATH", "/app/keys/jwt-public.pem")


def _load_public_key() -> str:
    try:
        pem = Path(JWT_PUBLIC_KEY_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"cannot read the JWT public key at {JWT_PUBLIC_KEY_PATH}: {exc}. "
            "Generate a keypair with `python scripts/generate_jwt_keys.py` and mount "
            "keys/jwt-public.pem into this service before starting."
        ) from exc
    if "PUBLIC KEY" not in pem:
        raise RuntimeError(f"{JWT_PUBLIC_KEY_PATH} is not a PEM public key")
    return pem


JWT_PUBLIC_KEY = _load_public_key()
# A token needs one of these to use docgen. "docgen_check" counts on its own:
# it designates the checker who approves docgen work, so a token carrying only
# it (issued before auth-service started implying "docgen") must still get in —
# otherwise the checker is locked out of the very thing they approve.
DOCGEN_SCOPES = {"docgen", "docgen_check", "admin"}

DEFAULT_PROFILE_READONLY = (
    "The Default profile is read-only — create a profile to work in"
)


def current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(db_session),
) -> User:
    """Resolve the caller from Kong's JWT and map it to a local User row."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = jwt.decode(token, JWT_PUBLIC_KEY, algorithms=[JWT_ALGORITHM], issuer=JWT_ISSUER)
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
        user = User(username=username, display_name=username,
                    org_role=org_role, is_admin=is_admin, is_active=True)
        db.add(user); db.commit(); db.refresh(user)
    elif (user.is_admin, user.org_role) != (is_admin, org_role):
        user.is_admin, user.org_role = is_admin, org_role
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
    # No separate "config editor" tier exists any more — folded into admin,
    # since nothing centrally-issued distinguishes it from is_admin anyway.
    if not user.is_admin:
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
