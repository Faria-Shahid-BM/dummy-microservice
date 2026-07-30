from fastapi import FastAPI, HTTPException, Depends
from pathlib import Path
from pydantic import BaseModel
import hashlib
import json
import os
import sqlite3
import time
import jwt

from claims import require_role
from audit import audit

app = FastAPI()

# RS256: this service holds the private key and is the only thing that can
# mint a token. Kong verifies with the matching public key inlined in kong.yml
# — a public key is not a secret, so nothing sensitive lives in that file.
# Kong's job is still just to confirm a token came from us; which services and
# role it grants is decided here, from claims baked into the token.
JWT_ISSUER = "poc-app"
JWT_ALGORITHM = "RS256"

# Tokens carry an expiry so a leaked one stops working. Kong is what actually
# enforces it — the jwt plugin only checks `exp` when the route lists it in
# `claims_to_verify` (see kong.yml), so both halves are required.
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", "3600"))
JWT_PRIVATE_KEY_PATH = os.environ.get("JWT_PRIVATE_KEY_PATH", "/app/keys/jwt-private.pem")


def _load_private_key() -> str:
    """Read the signing key, failing loudly at startup rather than at first login."""
    try:
        pem = Path(JWT_PRIVATE_KEY_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"cannot read the JWT signing key at {JWT_PRIVATE_KEY_PATH}: {exc}. "
            "Generate a keypair with `python scripts/generate_jwt_keys.py` before starting."
        ) from exc
    if "PRIVATE KEY" not in pem:
        raise RuntimeError(f"{JWT_PRIVATE_KEY_PATH} is not a PEM private key")
    return pem


JWT_PRIVATE_KEY = _load_private_key()

os.makedirs("data", exist_ok=True)
DB_PATH = "data/auth.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    _, digest = hash_password(password, salt)
    return digest == hash_hex


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            services TEXT NOT NULL
        )
        """
    )
    row_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if row_count == 0:
        # seed the same demo accounts the POC always had, now hashed and in the DB
        for username, password, role, services in [
            ("alice", "alicepw", "admin", ["document-reviewer", "collateral-reviewer"]),
            ("bob", "bobpw", "viewer", ["collateral-reviewer"]),
        ]:
            salt, pw_hash = hash_password(password)
            conn.execute(
                "INSERT INTO users (username, salt, password_hash, role, services) VALUES (?, ?, ?, ?, ?)",
                (username, salt, pw_hash, role, json.dumps(services)),
            )
        conn.commit()
    conn.close()


init_db()


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str
    services: list[str] = []


class ServicesUpdateRequest(BaseModel):
    services: list[str]


def public_user(row: sqlite3.Row) -> dict:
    return {
        "username": row["username"],
        "role": row["role"],
        "services": json.loads(row["services"]),
    }


@app.post("/login")
def login(req: LoginRequest):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (req.username,)).fetchone()
    conn.close()

    if not row or not verify_password(req.password, row["salt"], row["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid username or password")

    role = row["role"]
    services = json.loads(row["services"])
    issued_at = int(time.time())
    token = jwt.encode(
        {
            "iss": JWT_ISSUER,
            "username": req.username,
            "role": role,
            "services": services,
            "iat": issued_at,
            "exp": issued_at + JWT_TTL_SECONDS,
        },
        JWT_PRIVATE_KEY,
        algorithm=JWT_ALGORITHM,
    )
    return {"token": token, "username": req.username, "role": role, "services": services}


# Everything below is admin-only user management. Kong's jwt plugin (on the
# /api/auth/users route) only confirms the token is genuine — this service
# still has to decide whether the caller is actually an admin, same as any
# other role-gated endpoint in this project.


@app.post("/")
def create_user(req: CreateUserRequest, claims: dict = Depends(require_role("admin"))):
    conn = get_db()
    existing = conn.execute("SELECT 1 FROM users WHERE username = ?", (req.username,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail="user already exists")

    salt, pw_hash = hash_password(req.password)
    conn.execute(
        "INSERT INTO users (username, salt, password_hash, role, services) VALUES (?, ?, ?, ?, ?)",
        (req.username, salt, pw_hash, req.role, json.dumps(req.services)),
    )
    conn.commit()
    conn.close()

    audit(claims.get("username", "unknown"), "auth-service", "user.created", req.username)
    return {"username": req.username, "role": req.role, "services": req.services}


@app.get("/")
def list_users(claims: dict = Depends(require_role("admin"))):
    conn = get_db()
    rows = conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    return [public_user(r) for r in rows]


@app.put("/{username}/services")
def update_services(
    username: str,
    req: ServicesUpdateRequest,
    claims: dict = Depends(require_role("admin")),
):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="user not found")

    conn.execute(
        "UPDATE users SET services = ? WHERE username = ?",
        (json.dumps(req.services), username),
    )
    conn.commit()
    conn.close()

    audit(claims.get("username", "unknown"), "auth-service", "user.services_updated", username)
    return {"username": username, "role": row["role"], "services": req.services}
