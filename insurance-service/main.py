import json, os, re, tempfile
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException
from batch import collect_items, run_batch
from engines import extraction
from engines.insurance import review_insurance
from provider import Provider
from security import require_scope
from streaming import sse_stream
import httpx

app = FastAPI()
_provider = Provider()

# Persistent per-user bank-policy storage (mounted as a Docker volume). A bank
# that uploads its own policy is reviewed against that text; everyone else falls
# back to the bundled engines/data/policy.txt (see engines/insurance.py's
# review_insurance(policy_rules_text=None)).
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
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

def audit(user: str, service: str, action: str, resource: str = None):
    try:
        httpx.post(
            "http://audit-service:8000/audit",  # ← POST /audit
            json={
                "user_id": user,
                "service": service,
                "action": action,
                "resource": resource
            },
            timeout=1.0
        )
    except Exception:
        pass  # audit failure must never break the actual request


@app.post("/review")                     # Kong: POST /api/insurance/review
async def review(
    policy: UploadFile = File(...),
    user=Depends(require_scope("insurance")),   # 403 unless the JWT carries scope "insurance"
):
    username = user.get("sub", "unknown")
    rules = _policy_text(username)              # None → bundled data/policy.txt
    with tempfile.TemporaryDirectory() as tmp:
        policy_path = Path(tmp) / (policy.filename or "policy.pdf")
        policy_path.write_bytes(await policy.read())
        audit(user=username, service="Insurance-service", action="review")
        return review_insurance(
            policy_path,
            _provider,
            models={
                "extraction": os.environ["MODEL_EXTRACTION"],  # analysis-grade model
                "vision":     os.environ["MODEL_VISION"],
            },
            policy_rules_text=rules,
            emit=None,
        )


@app.post("/review/stream")               # Kong: POST /api/insurance/review/stream
async def review_stream(
    policy: UploadFile = File(...),
    user=Depends(require_scope("insurance")),
):
    """Same review pipeline, streamed as SSE stage events (extract, structure,
    analyze — each with status start/done) so the client can show live
    progress instead of blocking on one long request. See streaming.py for
    the event contract. review_insurance() has no live content-token
    streaming (unlike collateral's observations step) — only discrete stage
    events."""
    username = user.get("sub", "unknown")
    audit(user=username, service="Insurance-service", action="review_stream")

    policy_name = policy.filename or "policy.pdf"
    policy_bytes = await policy.read()
    rules = _policy_text(username)            # None → bundled data/policy.txt

    def run(emit):
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / policy_name
            policy_path.write_bytes(policy_bytes)
            return review_insurance(
                policy_path,
                _provider,
                models={
                    "extraction": os.environ["MODEL_EXTRACTION"],
                    "vision":     os.environ["MODEL_VISION"],
                },
                policy_rules_text=rules,
                emit=emit,
            )

    return await sse_stream(run)


# ──────────────────────────────────────────────────────────────────────────────
# Batch review: N insurance policies in one request
# ──────────────────────────────────────────────────────────────────────────────

_SLOTS = {"policy": "policy.pdf"}


def _review_one(paths, emit=None, *, rules=None):
    """One item of a batch: the same pipeline as /review, over batch.py's
    slot -> path mapping. `rules` is the reviewing user's own bank policy text,
    or None for the bundled one."""
    return review_insurance(
        paths["policy"],
        _provider,
        models={
            "extraction": os.environ["MODEL_EXTRACTION"],
            "vision":     os.environ["MODEL_VISION"],
        },
        policy_rules_text=rules,
        emit=emit,
    )


@app.post("/review/batch")               # Kong: POST /api/insurance/review/batch
async def review_batch(
    policy: list[UploadFile] = File(...),
    item_ids: list[str] | None = Form(None),
    user=Depends(require_scope("insurance")),
):
    """N policies in one request, each reviewed independently.
    Returns {"results": [{item, item_id, result|error}, ...]}."""
    items = await collect_items({"policy": policy}, item_ids, fallback_names=_SLOTS)
    username = user.get("sub", "unknown")
    audit(user=username, service="Insurance-service", action="review_batch")
    rules = _policy_text(username)           # read once for the whole batch
    return run_batch(items, lambda paths, emit=None: _review_one(paths, emit, rules=rules))


@app.post("/review/batch/stream")        # Kong: POST /api/insurance/review/batch/stream
async def review_batch_stream(
    policy: list[UploadFile] = File(...),
    item_ids: list[str] | None = Form(None),
    user=Depends(require_scope("insurance")),
):
    """Streaming batch — each policy's result is streamed as it lands, so the
    client can show it without waiting for the rest. See batch.py for the
    item_start/item_result/item_error event contract."""
    items = await collect_items({"policy": policy}, item_ids, fallback_names=_SLOTS)
    username = user.get("sub", "unknown")
    audit(user=username, service="Insurance-service", action="review_batch_stream")
    rules = _policy_text(username)           # read once for the whole batch

    def review_item(paths, emit=None):
        return _review_one(paths, emit, rules=rules)

    return await sse_stream(lambda emit: run_batch(items, review_item, emit))


# ──────────────────────────────────────────────────────────────────────────────
# Bank policy: upload the policy the review is graded against
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/policy")                      # Kong: GET /api/insurance/policy
def policy_status(user=Depends(require_scope("insurance"))):
    """Which bank policy this user's reviews are graded against.

    `has_own_policy` false → the bundled engines/data/policy.txt is used."""
    username = user.get("sub", "unknown")
    meta = _policy_meta(username)
    has_own = _policy_text(username) is not None
    return {
        "has_own_policy": has_own,
        "file_name": meta.get("file_name") if has_own else None,
        "chars": meta.get("chars") if has_own else None,
        "uploaded_at": meta.get("uploaded_at") if has_own else None,
    }


@app.post("/policy")                     # Kong: POST /api/insurance/policy
async def upload_policy(
    file: UploadFile = File(...),
    user=Depends(require_scope("insurance")),
):
    """Replace this user's bank policy. PDFs/DOCX go through the same
    text/vision-OCR extractor as the documents under review; .txt/.md are read
    as-is. Only the extracted text is kept — the raw upload is discarded."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in POLICY_SUFFIXES:
        raise HTTPException(
            422, f"Unsupported type {suffix or '(none)'!r}; allowed: {sorted(POLICY_SUFFIXES)}"
        )
    username = user.get("sub", "unknown")
    policy_dir = _user_policy_dir(username)
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

    audit(user=username, service="Insurance-service", action="policy_upload",
          resource=meta["file_name"])
    return {"ok": True, "has_own_policy": True, **meta}


@app.delete("/policy")                   # Kong: DELETE /api/insurance/policy
def delete_policy(user=Depends(require_scope("insurance"))):
    """Drop this user's policy — reviews fall back to the bundled policy.txt."""
    username = user.get("sub", "unknown")
    policy_dir = _user_policy_dir(username)
    deleted = []
    for name in ("policy.txt", "meta.json"):
        path = policy_dir / name
        if path.is_file():
            path.unlink(missing_ok=True)
            deleted.append(name)
    audit(user=username, service="Insurance-service", action="policy_delete")
    return {"ok": True, "has_own_policy": False, "deleted": deleted}
