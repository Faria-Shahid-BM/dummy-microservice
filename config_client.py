"""config_client.py — how a service asks config-service what to run with.

The sibling of audit_client.py, and the same shape: direct container DNS,
bypassing Kong, forwarding the caller's own bearer token so config-service can
re-verify it and answer for the right person.

The one rule worth stating loudly: **config being unreachable must not stop a
review.** A reviewer with documents in front of them should not be blocked
because a settings service is restarting, so every failure path here falls back
to the caller's own environment defaults — the same values config-service
would have served anyway for a user who has overridden nothing. The cost of
that choice is that a user's override is silently not applied during an outage;
`models_for` returns the source it used so the caller can record which it was
in the audit trail rather than leave it ambiguous.
"""
from __future__ import annotations

import os

import httpx

CONFIG_BASE = os.environ.get("CONFIG_SERVICE_URL", "http://config-service:8000")

# A review takes minutes, so this lookup only needs to be quick enough not to
# be noticed — and when config-service is unreachable this timeout IS the
# delay added to every review before the fallback kicks in. Measured at ~4s
# with the service stopped (DNS failure dominates), so keep the cap tight.
TIMEOUT_SECONDS = 2.0


def models_for(scope: str, token: str | None, defaults: dict[str, str]) -> dict[str, str]:
    """The models to run for this caller, keyed by role.

    `defaults` is what this service would have used on its own — pass the
    environment values it used to read directly. Anything config-service
    doesn't answer for keeps its default, so adding a role to the registry
    before the service knows about it can't blank a model mid-review.
    """
    resolved = dict(defaults)
    if not token:
        # No caller token to forward (an internal or unauthenticated path):
        # there's no user to look up, so the deployment default is the answer.
        return resolved
    try:
        response = httpx.get(
            f"{CONFIG_BASE}/api/config/effective/{scope}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        overrides = response.json().get("models") or {}
    except (httpx.HTTPError, ValueError):
        return resolved

    for role, model in overrides.items():
        if isinstance(model, str) and model.strip():
            resolved[role] = model.strip()
    return resolved
