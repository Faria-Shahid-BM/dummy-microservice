"""Password hashing, session tokens, and CSRF primitives.

- Passwords: argon2id via argon2-cffi.
- Session tokens: 256-bit urlsafe secrets; only the SHA-256 digest is stored,
  so a database leak does not leak usable cookies, and lookup by digest gives
  constant-time comparison for free.
- CSRF: double-submit cookie following Angular's convention (cookie
  ``XSRF-TOKEN`` echoed back as header ``X-XSRF-TOKEN``), enforced by
  middleware in app.main.
"""
from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id defaults

SESSION_COOKIE = "cw_session"
CSRF_COOKIE = "XSRF-TOKEN"
CSRF_HEADER = "x-xsrf-token"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, candidate: str) -> bool:
    try:
        return _hasher.verify(password_hash, candidate)
    except (VerifyMismatchError, VerificationError):
        return False


def new_token() -> str:
    """Opaque session/CSRF token (256-bit)."""
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
