"""OIDC authorization-code flow (Entra ID, Keycloak, ADFS 2019+, any
conformant IdP).

Discovery, JWKS, code exchange, and id_token validation via Authlib.
Users are matched by (issuer, subject) and auto-provisioned on first login
with NO roles — an admin must assign profile memberships before the user can
see anything. That keeps "connect the bank's IdP" a pure configuration step
without opening data to everyone in the directory.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from authlib.jose import JsonWebKey, jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import User


class OidcError(Exception):
    pass


class OidcAuthProvider:
    name = "oidc"

    def __init__(self) -> None:
        self._metadata: dict[str, Any] | None = None
        self._jwks: Any = None
        self._jwks_fetched_at = 0.0

    # -- discovery -----------------------------------------------------------

    def _discover(self) -> dict[str, Any]:
        if self._metadata is None:
            url = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
            resp = httpx.get(url, timeout=15)
            resp.raise_for_status()
            self._metadata = resp.json()
        return self._metadata

    def _keys(self):
        # refresh JWKS at most hourly (also on unknown-kid retry below)
        if self._jwks is None or time.time() - self._jwks_fetched_at > 3600:
            resp = httpx.get(self._discover()["jwks_uri"], timeout=15)
            resp.raise_for_status()
            self._jwks = JsonWebKey.import_key_set(resp.json())
            self._jwks_fetched_at = time.time()
        return self._jwks

    # -- flow ----------------------------------------------------------------

    def authorize_url(self, redirect_uri: str, state: str, nonce: str) -> str:
        meta = self._discover()
        params = httpx.QueryParams(
            response_type="code",
            client_id=settings.oidc_client_id,
            redirect_uri=redirect_uri,
            scope=settings.oidc_scopes,
            state=state,
            nonce=nonce,
        )
        return f"{meta['authorization_endpoint']}?{params}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        meta = self._discover()
        resp = httpx.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            raise OidcError(f"token endpoint returned {resp.status_code}")
        return resp.json()

    def validate_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        claims = jwt.decode(
            id_token,
            self._keys(),
            claims_options={
                "iss": {"essential": True, "value": settings.oidc_issuer.rstrip("/")},
                "aud": {"essential": True, "value": settings.oidc_client_id},
            },
        )
        claims.validate(leeway=60)
        if nonce and claims.get("nonce") != nonce:
            raise OidcError("nonce mismatch")
        return dict(claims)

    # -- provisioning ----------------------------------------------------------

    def find_or_create_user(self, db: Session, claims: dict[str, Any]) -> User:
        subject = f"{settings.oidc_issuer.rstrip('/')}|{claims['sub']}"
        user = db.execute(
            select(User).where(
                User.auth_provider == "oidc", User.external_subject == subject
            )
        ).scalar_one_or_none()
        if user is None:
            preferred = (
                claims.get("preferred_username")
                or claims.get("email")
                or f"oidc-{claims['sub'][:12]}"
            )
            username = preferred
            n = 1
            while db.execute(select(User).where(User.username == username)).scalar_one_or_none():
                n += 1
                username = f"{preferred}-{n}"
            user = User(
                username=username,
                email=claims.get("email"),
                display_name=claims.get("name") or preferred,
                auth_provider="oidc",
                external_subject=subject,
                password_hash=None,
            )
            db.add(user)
            db.flush()
        if not user.is_active:
            raise OidcError("account disabled")
        return user


oidc_provider = OidcAuthProvider()
