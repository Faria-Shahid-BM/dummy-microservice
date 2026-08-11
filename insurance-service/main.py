# insurance-service/main.py
import json, os, re, tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from engines import extraction
from engines.insurance import review_insurance
from provider import Provider
from security import require_scope
from case_store import init_db, make_case_router

app = FastAPI()
_provider = Provider()
init_db()

# ──────────────────────────────────────────────────────────────────────────────
# Bank policy: the rulebook a review is graded against
#
# Per user (the JWT's `sub`), alongside the cases DB on the same volume, and
# standing configuration rather than per-case input: a bank uploads its policy
# once and every case it reviews afterwards is graded against it. With none
# uploaded, engines/insurance.py falls back to its bundled data/policy.txt —
# that's what review_insurance(policy_rules_text=None) means.
# ──────────────────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
POLICIES_DIR = DATA_DIR / "policies"
POLICY_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}
_EXTRACTED = {".pdf", ".docx"}   # these go through text/OCR extraction first


def _user_policy_dir(username: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", username) or "user"   # safe folder name
    return POLICIES_DIR / safe


def _policy_meta(username: str) -> dict:
    """What's on record for this user's uploaded policy, if any."""
    meta_path = _user_policy_dir(username) / "meta.json"
    if not meta_path.is_file():
        return {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _policy_text(username: str) -> str | None:
    """This user's uploaded bank policy text, or None to use the bundled one."""
    path = _user_policy_dir(username) / "policy.txt"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return text if text.strip() else None


def _require_user(user: dict = Depends(require_scope("insurance"))) -> str:
    sub = user.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token has no subject")
    return sub


@app.get("/policy")                      # Kong: GET /api/insurance/policy
def policy_status(user_sub: str = Depends(_require_user)) -> dict:
    """Which bank policy this user's reviews are graded against.

    `has_own_policy` false → the bundled engines/data/policy.txt is used."""
    meta = _policy_meta(user_sub)
    has_own = _policy_text(user_sub) is not None
    return {
        "has_own_policy": has_own,
        "file_name": meta.get("file_name") if has_own else None,
        "chars": meta.get("chars") if has_own else None,
        "uploaded_at": meta.get("uploaded_at") if has_own else None,
    }


@app.post("/policy")                     # Kong: POST /api/insurance/policy
async def upload_policy(
    file: UploadFile = File(...),
    user_sub: str = Depends(_require_user),
) -> dict:
    """Replace this user's bank policy. PDFs/DOCX go through the same
    text/vision-OCR extractor as the documents under review; .txt/.md are read
    as-is. Only the extracted text is kept — the raw upload is discarded."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in POLICY_SUFFIXES:
        raise HTTPException(
            422, f"Unsupported type {suffix or '(none)'!r}; allowed: {sorted(POLICY_SUFFIXES)}"
        )
    policy_dir = _user_policy_dir(user_sub)
    policy_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=policy_dir) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        if suffix in _EXTRACTED:              # pdf/docx → text (OCR if scanned)
            text = extraction.extract_document(
                tmp_path, _provider, os.environ["MODEL_VISION"]).text
        else:                                 # txt/md → read as-is
            text = tmp_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            raise HTTPException(422, "No text could be extracted from the document.")
        # Write the text first: a failed meta write must not leave a policy
        # claiming to be a file it isn't, and vice versa.
        (policy_dir / "policy.txt").write_text(text, encoding="utf-8")
        meta = {
            "file_name": Path(file.filename or f"policy{suffix}").name,
            "chars": len(text),
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (policy_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    finally:
        tmp_path.unlink(missing_ok=True)      # never leave the raw upload behind

    return {"ok": True, "has_own_policy": True, **meta}


@app.delete("/policy")                   # Kong: DELETE /api/insurance/policy
def delete_policy(user_sub: str = Depends(_require_user)) -> dict:
    """Drop this user's policy — reviews fall back to the bundled policy.txt."""
    policy_dir = _user_policy_dir(user_sub)
    deleted = []
    for name in ("policy.txt", "meta.json"):
        path = policy_dir / name
        if path.is_file():
            path.unlink(missing_ok=True)
            deleted.append(name)
    return {"ok": True, "has_own_policy": False, "deleted": deleted}


# ──────────────────────────────────────────────────────────────────────────────
# Cases
# ──────────────────────────────────────────────────────────────────────────────

def _analyze(paths: dict[str, Path], emit: Callable[[str, str], None], user_sub: str) -> dict:
    return review_insurance(
        paths["policy"],
        _provider,
        models={
            "extraction": os.environ["MODEL_EXTRACTION"],  # analysis-grade model
            "vision":     os.environ["MODEL_VISION"],
        },
        # This account's own bank policy, or None for the engine's bundled one.
        policy_rules_text=_policy_text(user_sub),
        emit=emit,
    )


app.include_router(make_case_router(          # Kong exposes this as /api/insurance/cases...
    service_scope="insurance",                # 403 unless the JWT carries scope "insurance"
    upload_slots={"policy": {".pdf", ".docx"}},
    min_slots_ready=["policy"],
    analyze=_analyze,
))
