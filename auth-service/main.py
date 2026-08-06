import os
import json
import hmac
import hashlib
import secrets
import sqlite3
import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import jwt

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared with Kong (kong.yml consumer) and the downstream services (security.py).
SECRET = os.environ.get("JWT_SECRET", "mysecret123")
ISSUER = os.environ.get("JWT_ISSUER", "poc-issuer")

DB_PATH = Path(os.environ.get("USERS_DB", "/app/users.db"))

# The scopes an admin may assign. "admin" unlocks this user-management API;
ALLOWED_SCOPES = ["collateral", "docdiff", "valuation", "insurance", "policy_qa", "docgen", "docgen_check", "admin"]


# Seed data: the POC users and the scopes each may hold.
SEED_USERS = {
    "admin":   {"password": "password123",  "scopes": ["admin", "collateral", "docdiff", "valuation", "insurance", "policy_qa", "docgen"]},
    "checker": {"password": "checkerpass",   "scopes": ["docgen", "docgen_check"]},   # ← approves docgen work
    "carol":   {"password": "carolpass",     "scopes": ["collateral", "valuation"]},
    "dave":    {"password": "davepass",      "scopes": ["docdiff", "insurance"]},
}



# ── Password hashing (PBKDF2-HMAC-SHA256, stdlib only) ──────────────────────
def hash_password(password: str, salt: str | None = None) -> str:
    """Return 'salt$hexdigest'. A fresh random salt is generated when omitted."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _digest = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


# ── SQLite user store ───────────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the users table and seed it if empty. Idempotent per boot.

    Only seeds when the table is empty, so admin edits made at runtime survive a
    restart *if* the DB file is persisted; on a fresh container it re-seeds.
    """
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username      TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                scopes        TEXT NOT NULL      -- JSON array of scope strings
            )
            """
        )
        existing = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if existing == 0:
            for username, info in SEED_USERS.items():
                conn.execute(
                    "INSERT INTO users (username, password_hash, scopes) VALUES (?, ?, ?)",
                    (username, hash_password(info["password"]), json.dumps(info["scopes"])),
                )


def get_user(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT username, password_hash, scopes FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        return None
    return {
        "username": row["username"],
        "password_hash": row["password_hash"],
        "scopes": json.loads(row["scopes"]),
    }


def list_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT username, scopes FROM users ORDER BY username"
        ).fetchall()
    return [{"username": r["username"], "scopes": json.loads(r["scopes"])} for r in rows]


init_db()


# ── Admin authorization (defence in depth — the API enforces it) ────────────
def require_admin(authorization: str | None = Header(default=None)) -> dict:
    """Verify the caller's JWT and require the 'admin' scope. 401/403 otherwise."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"], issuer=ISSUER)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    if "admin" not in (payload.get("scopes") or []):
        raise HTTPException(status_code=403, detail="Admin scope required")
    return payload


def _validate_scopes(scopes: list[str]) -> list[str]:
    if not isinstance(scopes, list) or any(s not in ALLOWED_SCOPES for s in scopes):
        raise HTTPException(
            status_code=400,
            detail=f"Scopes must be a subset of {ALLOWED_SCOPES}",
        )
    # de-dup, preserve a stable order
    return [s for s in ALLOWED_SCOPES if s in scopes]


# ── Login ───────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/login")
def login(body: LoginRequest):
    user = get_user(body.username)
    if user is None or not verify_password(body.password, user["password_hash"]):
        return JSONResponse(status_code=401, content={"error": "Invalid credentials"})

    token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": user["username"],
            "scopes": user["scopes"],              # <- carried to the services
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=2),
        },
        SECRET,
        algorithm="HS256",
    )
    return {"token": token, "username": user["username"], "scopes": user["scopes"]}


# ── Admin: user management (all require the 'admin' scope) ───────────────────
class CreateUserRequest(BaseModel):
    username: str
    password: str
    scopes: list[str] = []


class ScopesRequest(BaseModel):
    scopes: list[str]


@app.get("/users")
def get_users(_admin=Depends(require_admin)):
    return list_users()


@app.post("/users", status_code=201)
def create_user(body: CreateUserRequest, _admin=Depends(require_admin)):
    username = body.username.strip()
    if not username or not body.password:
        raise HTTPException(status_code=400, detail="username and password are required")
    if get_user(username) is not None:
        raise HTTPException(status_code=409, detail=f"User '{username}' already exists")
    scopes = _validate_scopes(body.scopes)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, scopes) VALUES (?, ?, ?)",
            (username, hash_password(body.password), json.dumps(scopes)),
        )
    return {"username": username, "scopes": scopes}


@app.put("/users/{username}/scopes")
def update_scopes(username: str, body: ScopesRequest, admin=Depends(require_admin)):
    if get_user(username) is None:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    scopes = _validate_scopes(body.scopes)
    # Don't let an admin lock themselves out of the admin API.
    if username == admin.get("sub") and "admin" not in scopes:
        raise HTTPException(status_code=400, detail="You cannot revoke your own admin scope")
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET scopes = ? WHERE username = ?",
            (json.dumps(scopes), username),
        )
    return {"username": username, "scopes": scopes}


@app.delete("/users/{username}")
def delete_user(username: str, admin=Depends(require_admin)):
    if username == admin.get("sub"):
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    if get_user(username) is None:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
    return {"deleted": username}
