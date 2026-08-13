"""Service-level JWT scope enforcement — the real authorization boundary.

Defence in depth: Kong already verifies the JWT signature at the edge, but each
service ALSO re-verifies the token (against auth-service's public key) and
checks that the caller holds the scope the endpoint requires. The service
never blindly trusts that something upstream checked.

RS256: only auth-service holds the private key that signs tokens; every
verifier here just needs the public key, which isn't sensitive — a leaked
copy of it lets nobody forge a token, unlike a shared HS256 secret.

Kong's jwt plugin forwards the ``Authorization`` header upstream unchanged, so
the same Bearer token is available here.

Usage in a service::

    from fastapi import Depends
    from security import require_scope

    @app.post("/review")
    async def review(..., user=Depends(require_scope("collateral"))):
        ...

Returns 401 for a missing/invalid/expired token, 403 for a valid token that
lacks the required scope.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import jwt
from fastapi import Header, HTTPException

JWT_ISSUER = os.environ.get("JWT_ISSUER", "poc-issuer")
JWT_PUBLIC_KEY_PATH = os.environ.get("JWT_PUBLIC_KEY_PATH", "/app/keys/jwt-public.pem")


def _load_public_key() -> str:
    """Read the verification key, failing loudly at startup rather than at
    the first request."""
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


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    return authorization.split(" ", 1)[1].strip()


def _decode(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, JWT_PUBLIC_KEY, algorithms=["RS256"], issuer=JWT_ISSUER)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")


def require_scope(scope: str) -> Callable[..., dict[str, Any]]:
    """Build a FastAPI dependency that enforces ``scope`` on the request."""

    def dependency(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        payload = _decode(_bearer_token(authorization))

        scopes = payload.get("scopes") or []
        if not isinstance(scopes, list) or scope not in scopes:
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient scope: '{scope}' required for this service",
            )
        return payload

    return dependency


def require_any_token(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Like ``require_scope``, but for a service (audit-service) whose callers
    legitimately hold many different scopes — there's no single scope to
    check here, only that the token is genuinely signed by auth-service."""
    return _decode(_bearer_token(authorization))


def get_raw_token(authorization: str | None = Header(default=None)) -> str | None:
    """The caller's raw bearer token, unparsed, for forwarding to a service
    (audit-service) that needs to independently re-verify the original
    caller's identity itself rather than trust a second-hand string."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip()
