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
import time
from pathlib import Path
from typing import Any, Callable

import jwt
from fastapi import Header, HTTPException

JWT_ISSUER = os.environ.get("JWT_ISSUER", "poc-issuer")
JWT_PUBLIC_KEY_PATH = os.environ.get("JWT_PUBLIC_KEY_PATH", "/app/keys/jwt-public.pem")

# How long to wait for the keygen init service to write the key on a cold
# start before giving up. Generation takes ~1s; this is slack for a race.
KEY_WAIT_SECONDS = 30


def _load_public_key() -> str:
    """Read the verification key, waiting briefly for it to appear.

    The key is generated into a shared volume by the `keygen` init service at
    startup. A service may boot a moment before keygen has written the file, so
    poll for it rather than crashing on a harmless startup race. Still fails
    loudly (at startup, not at the first request) if it never appears."""
    deadline = time.time() + KEY_WAIT_SECONDS
    while True:
        try:
            pem = Path(JWT_PUBLIC_KEY_PATH).read_text(encoding="utf-8")
            break
        except OSError as exc:
            if time.time() >= deadline:
                raise RuntimeError(
                    f"cannot read the JWT public key at {JWT_PUBLIC_KEY_PATH} after "
                    f"{KEY_WAIT_SECONDS}s: {exc}. The keygen init service should have "
                    "written it to the shared volume before this service started."
                ) from exc
            time.sleep(0.5)
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


# How long past `exp` a token is still accepted on the audit-ingest path, to
# cover outbox delivery lag without accepting an indefinitely-old token.
ALLOW_EXPIRED_GRACE_SECONDS = 24 * 3600


def require_any_token_allow_expired(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Like ``require_any_token``, but doesn't reject an expired token.

    For audit-service's ingestion endpoint specifically: outbox.py's relay
    (see outbox.py) delivers events some time after they were enqueued —
    seconds normally, but potentially longer than a token's TTL if
    audit-service was briefly unreachable. The signature and issuer are still
    verified (so identity can't be forged), just not the expiry — a stale
    token proves the event's identity just as validly as a fresh one; it
    only stops being useful for making a NEW request, which this isn't."""
    token = _bearer_token(authorization)
    try:
        claims = jwt.decode(
            token, JWT_PUBLIC_KEY, algorithms=["RS256"], issuer=JWT_ISSUER,
            options={"verify_exp": False},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")

    # Allow a stale token, but not an ancient one. Delivery lag is seconds to
    # (rarely) hours; a token expired for days is far likelier to be one lifted
    # from a database file and replayed than a legitimately-delayed event. Bound
    # the grace to a day past expiry so a leaked token can't forge audit entries
    # indefinitely.
    exp = claims.get("exp")
    if exp is not None:
        if time.time() - float(exp) > ALLOW_EXPIRED_GRACE_SECONDS:
            raise HTTPException(status_code=401, detail="Token expired beyond the delivery grace window")
    return claims


def get_raw_token(authorization: str | None = Header(default=None)) -> str | None:
    """The caller's raw bearer token, unparsed, for forwarding to a service
    (audit-service) that needs to independently re-verify the original
    caller's identity itself rather than trust a second-hand string."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip()
