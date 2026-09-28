"""Shared client for audit-service, used by every service that records
audit events (see audit-service/main.py). Centralized so each service
doesn't hand-roll its own httpx.post + attachment-upload plumbing.

The direct, fire-and-forget ``audit()`` call that used to live here is gone —
every producer now goes through outbox.py's transactional outbox instead
(same delivery target, AUDIT_BASE, just durable against audit-service being
briefly unreachable instead of silently dropping the event).
"""
import os

import httpx

AUDIT_BASE = os.environ.get("AUDIT_SERVICE_URL", "http://audit-service:8000")


def strip_keys(value, keys: set[str]):
    """Recursively drop the given keys from a dict/list before it goes into
    an audit `metadata` payload. Only the producer knows which of its own
    fields are internal noise (e.g. a redline engine's full segment dump) —
    audit-service stores and displays whatever it's sent as-is, so that
    decision has to happen here, not there."""
    if isinstance(value, list):
        return [strip_keys(v, keys) for v in value]
    if isinstance(value, dict):
        return {k: strip_keys(v, keys) for k, v in value.items() if k not in keys}
    return value


def upload_attachment(filename: str, content: bytes, token: str | None = None) -> str | None:
    """Copy a file into audit-service's durable storage so it can be opened
    later from the audit trail. Returns the attachment_id, or None if the
    upload failed (never raises — same never-break-the-request rule).

    ``token`` is the caller's forwarded bearer token; audit-service now requires
    it (a valid signature) so a random process on the network can't write into
    the store. A missing token means the write is rejected there — which is the
    intended fail-closed behaviour, not a crash here."""
    try:
        r = httpx.post(
            f"{AUDIT_BASE}/audit/attachments",
            files={"file": (filename, content)},
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=10.0,
        )
        r.raise_for_status()
        return r.json()["attachment_id"]
    except Exception:
        return None
