"""Create the first administrator account.

    python -m app.bootstrap --username admin.name --password '...'

Refuses to run if any user already exists (use the admin UI afterwards).
There are no default credentials anywhere in this application.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.core.db import Base, engine, session_scope
from app.core.security import hash_password
from app.models import User


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the first admin user")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--display-name", default="")
    parser.add_argument("--email", default=None)
    args = parser.parse_args()

    if len(args.password) < 10:
        print("Password must be at least 10 characters.", file=sys.stderr)
        return 2

    Base.metadata.create_all(engine)
    with session_scope() as db:
        if db.execute(select(User)).first() is not None:
            print("Users already exist — refusing to bootstrap. Use the admin UI.", file=sys.stderr)
            return 1
        db.add(
            User(
                username=args.username,
                password_hash=hash_password(args.password),
                display_name=args.display_name or args.username,
                email=args.email,
                auth_provider="local",
                is_admin=True,
            )
        )
    print(f"Admin user '{args.username}' created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
