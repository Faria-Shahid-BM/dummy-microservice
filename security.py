"""Service-level JWT scope enforcement — the real authorization boundary.

Defence in depth: Kong already verifies the JWT signature at the edge, but each
service ALSO re-verifies the token (with the shared secret) and checks that the
caller holds the scope the endpoint requires. The service never blindly trusts
that something upstream checked.

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
from typing import Any, Callable

import jwt
from fastapi import Header, HTTPException

# Must match auth-service (kong.yml consumer uses the same secret + issuer key).
JWT_SECRET = os.environ.get("JWT_SECRET", "mysecret123")
JWT_ISSUER = os.environ.get("JWT_ISSUER", "poc-issuer")


def require_scope(scope: str) -> Callable[..., dict[str, Any]]:
    """Build a FastAPI dependency that enforces ``scope`` on the request."""

    def dependency(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

        token = authorization.split(" ", 1)[1].strip()
        try:
            payload = jwt.decode(
                token,
                JWT_SECRET,
                algorithms=["HS256"],
                issuer=JWT_ISSUER,
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")

        scopes = payload.get("scopes") or []
        if not isinstance(scopes, list) or scope not in scopes:
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient scope: '{scope}' required for this service",
            )
        return payload

    return dependency
