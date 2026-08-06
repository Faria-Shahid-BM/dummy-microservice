"""Authentication provider contract.

A provider's only job is to answer "who is this?" — it returns an existing or
newly provisioned :class:`app.models.User`. Sessions, roles, and everything
after identification belong to the platform, so swapping how a bank
authenticates never touches authorization or business code.
"""
from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from app.models import User


class AuthProvider(Protocol):
    name: str

    def authenticate(self, db: Session, **credentials) -> User | None:
        """Return the user for valid credentials, else None. Must not raise
        on bad credentials (timing-safe failure paths where practical)."""
        ...
