"""Shared client for audit-service, used by every service that records
audit events (see audit-service/main.py). Centralized so each service
doesn't hand-roll its own httpx.post + attachment-upload plumbing.
"""
import httpx

AUDIT_BASE = "http://audit-service:8000"


def audit(user_id: str, service: str, action: str, resource: str | None = None,
          metadata: dict | None = None) -> None:
    try:
        httpx.post(
            f"{AUDIT_BASE}/audit",
            json={
                "user_id": user_id,
                "service": service,
                "action": action,
                "resource": resource,
                "metadata": metadata,
            },
            timeout=1.0,
        )
    except Exception:
        pass  # audit failure must never break the actual request


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


def upload_attachment(filename: str, content: bytes) -> str | None:
    """Copy a file into audit-service's durable storage so it can be opened
    later from the audit trail. Returns the attachment_id, or None if the
    upload failed (never raises — same never-break-the-request rule)."""
    try:
        r = httpx.post(
            f"{AUDIT_BASE}/audit/attachments",
            files={"file": (filename, content)},
            timeout=10.0,
        )
        r.raise_for_status()
        return r.json()["attachment_id"]
    except Exception:
        return None
