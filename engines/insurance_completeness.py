
from __future__ import annotations

import logging
from typing import Any, Mapping

from engines.completeness import (
    FieldGroup,
    is_filled,
    resolve,
    score,
    shortfall_detail,
)

logger = logging.getLogger(__name__)

__all__ = [
    "IDENTITY_FIELDS",
    "MIN_IDENTITY_FIELDS",
    "InsufficientPolicyDetailError",
    "identity_fields",
    "is_filled",
    "require_insurance_document",
]

# One entry per point. A tuple of paths means any one of them scores it, which
# is how policy_type and policy_class count once between them rather than
# twice - they are the same fact read off two different labels.
IDENTITY_FIELDS: tuple[FieldGroup, ...] = (
    ("insurer name", ("basic_info.insurer_name",)),
    ("policy number", ("basic_info.policy_number",)),
    ("policy type", ("basic_info.policy_type", "basic_info.policy_class")),
    ("policy start date", ("dates.policy_start_date",)),
    ("policy end date", ("dates.policy_end_date",)),
)

# Deliberately not in config.py, for the reason the collateral gate's thresholds
# are not either: this decides whether a review happens at all, and a setting
# a deployment can lower to zero is a gate that silently stops working.
MIN_IDENTITY_FIELDS: int = 2

class InsufficientPolicyDetailError(Exception):
    """Too little policy detail was read to treat this as an insurance policy."""


def identity_fields(extraction: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """``(found, missing)`` names from :data:`IDENTITY_FIELDS`, in order."""
    return score(extraction, IDENTITY_FIELDS)


def require_insurance_document(extraction: Mapping[str, Any]) -> None:
    """Raise :class:`InsufficientPolicyDetailError` if this is not a policy.

    Called between the two model calls, so a document that fails costs the
    vision read alone and never reaches the analysis call.
    """
    found, missing = identity_fields(extraction or {})
    if len(found) >= MIN_IDENTITY_FIELDS:
        return

    detail = shortfall_detail(
        found, missing, len(IDENTITY_FIELDS),
        what="policy detail",
        advice="Check that the upload is an insurance policy and that the "
               "scan is legible.",
    )
    logger.info("Insurance completeness check rejected a document: %s", detail)
    raise InsufficientPolicyDetailError(detail)
