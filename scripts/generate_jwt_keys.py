#!/usr/bin/env python3
"""Generate the RS256 keypair used to sign and verify this app's JWTs.

auth-service signs tokens with the private key; every verifier (Kong, and
each service via security.py / docgen-service's deps.py) checks them with the
matching public key. Kong needs its copy inlined in kong.yml (it reads
declarative config once at startup, not a mounted file), so this script
patches that block for you. Services read the public key from a mounted
file instead (see docker-compose.yml), so nothing else needs touching.

Run once per environment, before the first `docker compose up`:

    python scripts/generate_jwt_keys.py

Re-running refuses to clobber existing keys unless --force is passed. Rotating
keys means regenerating both halves together and restarting Kong and every
service, since kong.yml is read at Kong startup and the public key file is
read once at each service's startup.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    sys.exit(
        "cryptography is not installed.\n"
        "Install this project's requirements first:  pip install -r requirements.txt"
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
KEYS_DIR = REPO_ROOT / "keys"
PRIVATE_PATH = KEYS_DIR / "jwt-private.pem"
PUBLIC_PATH = KEYS_DIR / "jwt-public.pem"
KONG_CONFIG = REPO_ROOT / "kong.yml"

KEY_SIZE = 2048

# Matches the `jwt_secrets` entry's `key:` line (the consumer's issuer key),
# however that entry currently stores its secret material — a plain HS256
# `secret:` line, a stale `algorithm:` / `rsa_public_key: |` block from a
# previous run, or nothing yet.
_SECRETS_ENTRY_RE = re.compile(r"^(?P<indent>\s*)-\s*key:\s*\S+\s*$")


def generate_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def patch_kong_config(public_pem: str) -> bool:
    """Replace the `jwt_secrets` entry's secret material with `algorithm: RS256`
    + `rsa_public_key: |` <public key>. True if changed."""
    if not KONG_CONFIG.exists():
        print(f"! {KONG_CONFIG} not found — skipping kong.yml update", file=sys.stderr)
        return False

    lines = KONG_CONFIG.read_text(encoding="utf-8").splitlines()
    anchor = None
    for i, line in enumerate(lines):
        if _SECRETS_ENTRY_RE.match(line):
            anchor = i
            break

    if anchor is None:
        print(
            "! could not find a `- key: <issuer>` line under `jwt_secrets` in kong.yml.\n"
            "  Paste this under the consumer's jwt_secrets entry manually:\n"
            "    algorithm: RS256\n    rsa_public_key: |\n",
            file=sys.stderr,
        )
        print(public_pem, file=sys.stderr)
        return False

    entry_indent = len(lines[anchor]) - len(lines[anchor].lstrip())
    body_indent = " " * (entry_indent + 2)

    # Everything indented deeper than the `- key:` line belongs to this same
    # jwt_secrets entry (its old secret/algorithm/rsa_public_key block) — drop
    # it all and write a fresh RS256 block in its place.
    end = anchor + 1
    while end < len(lines):
        stripped = lines[end].strip()
        indent = len(lines[end]) - len(lines[end].lstrip())
        if stripped and indent <= entry_indent:
            break
        end += 1

    new_body = [f"{body_indent}algorithm: RS256", f"{body_indent}rsa_public_key: |"]
    new_body += [f"{body_indent}  {l}" for l in public_pem.strip().splitlines()]
    KONG_CONFIG.write_text(
        "\n".join(lines[: anchor + 1] + new_body + lines[end:]) + "\n",
        encoding="utf-8",
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite existing keys")
    args = parser.parse_args()

    if PRIVATE_PATH.exists() and not args.force:
        print(f"{PRIVATE_PATH} already exists. Re-run with --force to replace it.")
        print("Replacing the keypair invalidates every token already issued.")
        return 1

    KEYS_DIR.mkdir(exist_ok=True)
    private_pem, public_pem = generate_keypair()

    PRIVATE_PATH.write_text(private_pem, encoding="utf-8")
    PUBLIC_PATH.write_text(public_pem, encoding="utf-8")
    try:
        PRIVATE_PATH.chmod(0o600)  # no-op on Windows, meaningful on the server
    except OSError:
        pass

    print(f"wrote {PRIVATE_PATH.relative_to(REPO_ROOT)}  (private — never commit)")
    print(f"wrote {PUBLIC_PATH.relative_to(REPO_ROOT)}   (public — safe to share)")

    if patch_kong_config(public_pem):
        print(f"updated {KONG_CONFIG.name} with the matching public key")

    print("\nNext: docker compose build && docker compose up -d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
