#!/usr/bin/env python3
"""Migration: rehash existing users.db rows from the old PBKDF2-HMAC-SHA256
format ('salt$hexdigest') to argon2id, now that main.py's
hash_password()/verify_password() only understand argon2id.

main.py calls main() here on every startup, right after init_db() — so an
`auth-data` volume left over from before the argon2id switch heals itself the
next time the container starts, with no manual step. It's cheap and safe to
run on every boot: already-argon2id rows are skipped immediately, and a seed
row is only rewritten after re-verifying it against the known plaintext (so a
seed account whose password was since changed by an admin is left alone too,
not clobbered).

Only handles the accounts whose plaintext this repo actually knows — the
seeded demo accounts below. Any password created later through POST /users or
admin-set changes is only known to that user; there is no way to recover a
plaintext from a hash to rehash it, so those rows are reported, not touched.
Their owners need a new password set for them the normal way.

Can still be run by hand the same way if you want to check its output without
restarting the service:

    docker compose exec auth-service python rehash_seed_passwords.py
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from pathlib import Path

from argon2 import PasswordHasher

# Must match auth-service/main.py's SEED_USERS plaintexts.
SEED_PASSWORDS = {
    "admin": "password123",
    "checker": "checkerpass",
    "carol": "carolpass",
    "dave": "davepass",
}

DB_PATH = Path(os.environ.get("USERS_DB", "/app/users.db"))
_hasher = PasswordHasher()


def _is_argon2(stored: str) -> bool:
    return stored.startswith("$argon2")


def _old_pbkdf2_verify(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
    return hmac.compare_digest(dk.hex(), digest)


def main() -> int:
    if not DB_PATH.exists():
        print(f"{DB_PATH} does not exist — nothing to migrate.")
        return 0

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT username, password_hash FROM users").fetchall()

    migrated, already_ok, mismatched, unknown = [], [], [], []

    for row in rows:
        username, stored = row["username"], row["password_hash"]

        if _is_argon2(stored):
            already_ok.append(username)
            continue

        plaintext = SEED_PASSWORDS.get(username)
        if plaintext is None:
            unknown.append(username)
            continue

        if not _old_pbkdf2_verify(plaintext, stored):
            # Someone changed this seed account's password since seeding —
            # the known plaintext no longer applies, don't guess.
            mismatched.append(username)
            continue

        new_hash = _hasher.hash(plaintext)
        assert _hasher.verify(new_hash, plaintext)  # paranoia before committing
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (new_hash, username))
        migrated.append(username)

    conn.commit()
    conn.close()

    print(f"migrated to argon2id ({len(migrated)}): {migrated}")
    print(f"already argon2id, skipped ({len(already_ok)}): {already_ok}")
    if mismatched:
        print(f"! seed password no longer matches, left as-is ({len(mismatched)}): {mismatched}")
        print("  these users kept a PBKDF2 hash — they'll need their password reset.")
    if unknown:
        print(f"! not a seed account, plaintext unknown, left as-is ({len(unknown)}): {unknown}")
        print("  these users kept a PBKDF2 hash and can no longer log in — recreate their account "
              "or add a password-reset path; their old password cannot be recovered to rehash it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
