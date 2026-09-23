"""Audit trail: every mutating action records who did what, where, to what.

``record()`` enqueues into this service's own local audit_outbox table (see
outbox.py), in the SAME transaction as the caller's business write — commit
it together, before ``db.commit()``, not after. A background relay (started
in app/main.py's lifespan) delivers queued events to the central
audit-service, the same one collateral/insurance/valuation/doc_rev/policyqa
report into (see POC_TO_PRODUCTION.md #14 — this used to be a separate,
local-only trail; now it's the same store everyone else uses).
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

import outbox
from audit_client import AUDIT_BASE

from app.auth.deps import current_user
from app.models import AUDIT_OUTBOX, User

router = APIRouter(prefix="/api/audit", tags=["audit"])

SERVICE_NAME = "docgen-service"


def _raw_token(request: Request | None) -> str | None:
    """The caller's bearer token, unparsed — forwarded so audit-service can
    re-verify identity itself rather than trust a passed-in user_id."""
    if request is None:
        return None
    auth = request.headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip()


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
    outbox.enqueue(
        db, AUDIT_OUTBOX,
        service=SERVICE_NAME,
        action=action,
        token=_raw_token(request),
        subject_type=subject_type,
        subject_id=subject_id,
        profile_id=profile_id,
        detail=detail,
    )


@router.get("")
def list_audit(
    profile_id: str | None = None,
    action: str | None = None,
    limit: int = 200,
    offset: int = 0,
    user: User = Depends(current_user),
) -> dict:
    """Proxy to the central audit-service, scoped to this service's own
    events — docgen no longer keeps a separate trail (see module docstring).
    Single organization: any authenticated user may list the trail — it is
    an internal control surface."""
    params: dict[str, str] = {"service": SERVICE_NAME}
    if profile_id:
        params["profile_id"] = profile_id
    if action:
        params["action"] = action
    try:
        resp = httpx.get(f"{AUDIT_BASE}/audit", params=params, timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"audit-service unreachable: {exc}")
    rows = resp.json()
    return {"entries": rows[offset: offset + limit]}
