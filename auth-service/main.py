import os
import json
import sqlite3
import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
import jwt

import rehash_seed_passwords

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# RS256: this service is the only one that holds the private key and can mint
# a token. Kong and every downstream service (security.py) verify with the
# matching public key — a public key isn't a secret, so nothing sensitive
# needs to be shared with them, unlike an HS256 secret copied into every
# service's env (a leak of any one container would forge tokens for all).
ISSUER = os.environ.get("JWT_ISSUER", "poc-issuer")
ALGORITHM = "RS256"
JWT_PRIVATE_KEY_PATH = os.environ.get("JWT_PRIVATE_KEY_PATH", "/app/keys/jwt-private.pem")
JWT_PUBLIC_KEY_PATH = os.environ.get("JWT_PUBLIC_KEY_PATH", "/app/keys/jwt-public.pem")


def _load_key(path: str, marker: str) -> str:
    """Read a signing/verification key, failing loudly at startup rather than
    at the first login."""
    try:
        pem = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"cannot read the JWT key at {path}: {exc}. "
            "Generate a keypair with `python scripts/generate_jwt_keys.py` before starting."
        ) from exc
    if marker not in pem:
        raise RuntimeError(f"{path} is not a PEM {marker.lower()}")
    return pem


PRIVATE_KEY = _load_key(JWT_PRIVATE_KEY_PATH, "PRIVATE KEY")
PUBLIC_KEY = _load_key(JWT_PUBLIC_KEY_PATH, "PUBLIC KEY")

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



# ── Password hashing (argon2id via argon2-cffi) ─────────────────────────────
_hasher = PasswordHasher()  # argon2id defaults


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError):
        return False


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
rehash_seed_passwords.main()  # idempotent: no-ops once every row is already argon2id


# ── Admin authorization (defence in depth — the API enforces it) ────────────
def require_admin(authorization: str | None = Header(default=None)) -> dict:
    """Verify the caller's JWT and require the 'admin' scope. 401/403 otherwise."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, PUBLIC_KEY, algorithms=[ALGORITHM], issuer=ISSUER)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    if "admin" not in (payload.get("scopes") or []):
        raise HTTPException(status_code=403, detail="Admin scope required")
    return payload


def _normalize_scopes(scopes: list[str]) -> list[str]:
    """De-dup into a stable order and apply scope implications.

    "docgen_check" only says *how* someone uses docgen (as the checker who
    approves work) — it is not access to docgen on its own. Granting it without
    "docgen" produced a checker who couldn't reach docgen at all: no sidebar
    entry, no notification bell, and a 403 from docgen-service (see its
    DOCGEN_SCOPES). So imply the access scope here, once, for every caller —
    both new grants and logins by accounts granted before this rule existed.
    """
    scopes = list(scopes)
    if "docgen_check" in scopes and "docgen" not in scopes:
        scopes.append("docgen")
    return [s for s in ALLOWED_SCOPES if s in scopes]


def _validate_scopes(scopes: list[str]) -> list[str]:
    if not isinstance(scopes, list) or any(s not in ALLOWED_SCOPES for s in scopes):
        raise HTTPException(
            status_code=400,
            detail=f"Scopes must be a subset of {ALLOWED_SCOPES}",
        )
    return _normalize_scopes(scopes)


# ── Login ───────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


# The JWT lives only in this cookie now — never in a JS-readable response
# field or client-side storage, so an XSS bug can't exfiltrate it. Kong's
# global pre-function plugin (see kong.yml) reads this cookie on every
# request and sets it as the Authorization header before proxying upstream,
# so every service's existing require_scope()/require_admin() (which read
# that header) needed no changes.
COOKIE_NAME = "access_token"
TOKEN_TTL = datetime.timedelta(hours=2)


@app.post("/login")
def login(body: LoginRequest, response: Response):
    user = get_user(body.username)
    if user is None or not verify_password(body.password, user["password_hash"]):
        return JSONResponse(status_code=401, content={"error": "Invalid credentials"})

    # Normalize on the way out too, so accounts whose scopes were stored before
    # the implication rule existed still get a usable token.
    scopes = _normalize_scopes(user["scopes"])
    token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": user["username"],
            "scopes": scopes,                      # <- carried to the services
            "exp": datetime.datetime.utcnow() + TOKEN_TTL,
        },
        PRIVATE_KEY,
        algorithm=ALGORITHM,
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=int(TOKEN_TTL.total_seconds()),
        path="/",
        # secure=True once the stack is served over TLS (see
        # POC_TO_PRODUCTION.md #4) — omitted for now since this deploys over
        # plain HTTP, where a Secure cookie would just never get sent.
    )
    return {"username": user["username"], "scopes": scopes}


@app.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


# Lets the frontend re-derive "who is this?" after a page refresh, when the
# in-memory session state is gone but the httpOnly cookie (and thus the
# Authorization header Kong builds from it) is still valid.
@app.get("/me")
def me(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, PUBLIC_KEY, algorithms=[ALGORITHM], issuer=ISSUER)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    # Normalized like /login, so a cookie minted before the implication rule
    # still tells the UI what the services will actually honour.
    return {
        "username": payload.get("sub"),
        "scopes": _normalize_scopes(payload.get("scopes") or []),
    }


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
