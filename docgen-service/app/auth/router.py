"""Auth endpoints: local login/logout, session info, OIDC code flow."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import audit
from app.core.config import settings
from app.core.db import db_session
from app.core.security import CSRF_COOKIE, SESSION_COOKIE, new_token, token_digest
from app.auth.deps import current_session
from app.auth.providers.local import LocalAuthProvider
from app.auth.providers.oidc import OidcError, oidc_provider
from app.models import AuthSession, User, utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])
_local = LocalAuthProvider()


def _state_serializer() -> URLSafeTimedSerializer:
    if not settings.secret_key:
        raise HTTPException(status_code=500, detail="SECRET_KEY is required for OIDC")
    return URLSafeTimedSerializer(settings.secret_key, salt="oidc-state")


# --------------------------------------------------------------------- sessions


def establish_session(response: Response, db: Session, user: User, request: Request) -> None:
    token = new_token()
    csrf = new_token()
    sess = AuthSession(
        token_hash=token_digest(token),
        csrf_token=csrf,
        user_id=user.id,
        expires_at=utcnow() + timedelta(hours=settings.session_absolute_hours),
        ip=request.client.host if request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:400],
    )
    db.add(sess)
    db.commit()
    common = {
        "secure": settings.cookie_secure,
        "samesite": "lax",
        "max_age": settings.session_absolute_hours * 3600,
        "path": "/",
    }
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)
    # readable by Angular's HttpClient XSRF interceptor
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)


def _me_payload(db: Session, user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "is_admin": user.is_admin,
        "auth_provider": user.auth_provider,
        "org_role": user.org_role,
        "can_edit_config": user.can_edit_config,
    }


# ----------------------------------------------------------------------- routes


@router.get("/methods")
def auth_methods() -> dict:
    return {
        "local": "local" in settings.auth_provider_list,
        "oidc": settings.oidc_enabled,
        "oidc_display_name": settings.oidc_display_name,
    }


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(
    body: LoginBody, request: Request, response: Response, db: Session = Depends(db_session)
) -> dict:
    if "local" not in settings.auth_provider_list:
        raise HTTPException(status_code=400, detail="Local login is disabled")
    user = _local.authenticate(db, username=body.username, password=body.password)
    if user is None:
        audit.record(
            db, None, "auth.login_failed", detail={"username": body.username}, request=request
        )
        db.commit()  # persist the audit entry despite the 401 (as oidc_callback does)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    establish_session(response, db, user, request)
    audit.record(db, user, "auth.login", request=request)
    db.commit()
    return _me_payload(db, user)


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    pair: tuple[AuthSession, User] = Depends(current_session),
    db: Session = Depends(db_session),
) -> dict:
    sess, user = pair
    db.delete(sess)
    audit.record(db, user, "auth.logout", request=request)
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(
    pair: tuple[AuthSession, User] = Depends(current_session),
    db: Session = Depends(db_session),
) -> dict:
    return _me_payload(db, pair[1])


# ------------------------------------------------------------------------- OIDC


@router.get("/oidc/start")
def oidc_start(request: Request) -> RedirectResponse:
    if not settings.oidc_enabled:
        raise HTTPException(status_code=400, detail="OIDC is not configured")
    nonce = new_token()
    state = _state_serializer().dumps({"nonce": nonce})
    redirect_uri = str(request.url_for("oidc_callback"))
    return RedirectResponse(oidc_provider.authorize_url(redirect_uri, state, nonce))


@router.get("/oidc/callback", name="oidc_callback")
def oidc_callback(
    request: Request,
    code: str = "",
    state: str = "",
    db: Session = Depends(db_session),
) -> RedirectResponse:
    if not settings.oidc_enabled:
        raise HTTPException(status_code=400, detail="OIDC is not configured")
    try:
        payload = _state_serializer().loads(state, max_age=600)
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid or expired login state")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    redirect_uri = str(request.url_for("oidc_callback"))
    try:
        tokens = oidc_provider.exchange_code(code, redirect_uri)
        claims = oidc_provider.validate_id_token(tokens["id_token"], payload.get("nonce", ""))
        user = oidc_provider.find_or_create_user(db, claims)
    except OidcError as exc:
        audit.record(db, None, "auth.oidc_failed", detail={"error": str(exc)}, request=request)
        db.commit()
        raise HTTPException(status_code=401, detail=f"SSO login failed: {exc}")
    response = RedirectResponse("/", status_code=303)
    establish_session(response, db, user, request)
    audit.record(db, user, "auth.login_oidc", request=request)
    db.commit()
    return response
