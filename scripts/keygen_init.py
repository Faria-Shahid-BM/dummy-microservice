#!/usr/bin/env python3
"""Startup init: ensure the JWT keypair exists, and render kong.yml from its
template with the matching public key.

Runs once as a compose init service (everything else waits on it via
`depends_on: keygen: condition: service_completed_successfully`). On first boot
it generates the RS256 keypair into a mounted volume; on every later boot it
finds the existing keys and reuses them — so the SAME key is used across
restarts (regenerating would invalidate every issued token and log everyone
out). Keys therefore live only in the volume, never in the repo or an image,
and there is no manual pre-`docker compose up` step.

Paths come from the environment so the same script serves any layout:

    KEYS_DIR        where the keypair lives          (default /keys)
    KONG_TEMPLATE   the committed kong.yml template  (default /config/kong.template.yml)
    KONG_OUTPUT     the rendered config Kong reads   (default /out/kong.yml)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEYS_DIR = Path(os.environ.get("KEYS_DIR", "/keys"))
PRIVATE_PATH = KEYS_DIR / "jwt-private.pem"
PUBLIC_PATH = Path(os.environ.get("PUBLIC_OUT", str(KEYS_DIR / "jwt-public.pem")))
KONG_TEMPLATE = Path(os.environ.get("KONG_TEMPLATE", "/config/kong.template.yml"))
KONG_OUTPUT = Path(os.environ.get("KONG_OUTPUT", "/out/kong.yml"))

KEY_SIZE = 2048

# The `- key: <issuer>` line under the consumer's jwt_secrets; the RS256 block
# is (re)written directly beneath it.
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


def render_kong(public_pem: str) -> None:
    """Write KONG_OUTPUT from KONG_TEMPLATE, injecting the RS256 public key
    under the jwt_secrets entry. The template carries no key, so nothing
    sensitive is ever committed and there is no per-environment drift."""
    if not KONG_TEMPLATE.exists():
        sys.exit(f"kong template not found: {KONG_TEMPLATE}")
    lines = KONG_TEMPLATE.read_text(encoding="utf-8").splitlines()

    anchor = next((i for i, l in enumerate(lines) if _SECRETS_ENTRY_RE.match(l)), None)
    if anchor is None:
        sys.exit("could not find a `- key: <issuer>` line under jwt_secrets in the template")

    entry_indent = len(lines[anchor]) - len(lines[anchor].lstrip())
    body_indent = " " * (entry_indent + 2)

    # Drop anything already indented under the entry (a stale key block), then
    # write a fresh RS256 block.
    end = anchor + 1
    while end < len(lines):
        stripped = lines[end].strip()
        indent = len(lines[end]) - len(lines[end].lstrip())
        if stripped and indent <= entry_indent:
            break
        end += 1

    body = [f"{body_indent}algorithm: RS256", f"{body_indent}rsa_public_key: |"]
    body += [f"{body_indent}  {l}" for l in public_pem.strip().splitlines()]
    KONG_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    KONG_OUTPUT.write_text("\n".join(lines[: anchor + 1] + body + lines[end:]) + "\n",
                           encoding="utf-8")


def main() -> int:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    if PRIVATE_PATH.exists() and PUBLIC_PATH.exists():
        public_pem = PUBLIC_PATH.read_text(encoding="utf-8")
        print(f"[keygen] reusing existing keypair at {KEYS_DIR}")
    else:
        private_pem, public_pem = generate_keypair()
        PRIVATE_PATH.write_text(private_pem, encoding="utf-8")
        PUBLIC_PATH.write_text(public_pem, encoding="utf-8")
        try:
            PRIVATE_PATH.chmod(0o600)
        except OSError:
            pass
        print(f"[keygen] generated a new RS256 keypair at {KEYS_DIR}")

    render_kong(public_pem)
    print(f"[keygen] rendered {KONG_OUTPUT} with the matching public key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
