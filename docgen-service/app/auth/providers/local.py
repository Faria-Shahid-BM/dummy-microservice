"""Local username/password authentication against the users table."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models import User

# Verified against when the username doesn't exist, so both failure paths
# cost one argon2 verification (no username-enumeration timing signal).
_DUMMY_HASH = hash_password("not-a-real-password")


class LocalAuthProvider:
    name = "local"

    def authenticate(self, db: Session, *, username: str = "", password: str = "", **_) -> User | None:
        user = db.execute(
            select(User).where(User.username == username, User.auth_provider == "local")
        ).scalar_one_or_none()
        if user is None or not user.password_hash:
            verify_password(_DUMMY_HASH, password)
            return None
        if not verify_password(user.password_hash, password):
            return None
        if not user.is_active:
            return None
        return user
