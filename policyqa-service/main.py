import os, re, tempfile
from pathlib import Path
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException
from pydantic import BaseModel

import audit_client
from engines import policy_qa, extraction
from provider import Provider
from security import require_scope
from streaming import sse_stream

app = FastAPI()
_provider = Provider()

# Persistent per-user index storage (mounted as a Docker volume).
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
INDEXES_DIR = DATA_DIR / "indexes"
ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}
_EXTRACTED = {".pdf", ".docx"}   # these go through text/OCR extraction first

def audit(user, action, resource=None, metadata=None):
    audit_client.audit(user, "policyqa-service", action, resource=resource, metadata=metadata)

def _user_index_dir(username: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", username) or "user"   # safe folder name
    return INDEXES_DIR / safe

# ── status: does this user have their own index? ──────────────────
@app.get("/status")
def status(user=Depends(require_scope("policy_qa"))):
    idx = _user_index_dir(user.get("sub", "unknown"))
    return {
        "has_own_index": policy_qa.has_index(idx),
        "bundled_available": policy_qa.has_index(policy_qa.BUNDLED_DIR),
    }

# ── chat: use the user's own index if present, else the bundled one ─
class ChatBody(BaseModel):
    query: str
    history: list[dict] = []

@app.post("/chat")
def chat(body: ChatBody, user=Depends(require_scope("policy_qa"))):
    username = user.get("sub", "unknown")
    idx = _user_index_dir(username)
    result = policy_qa.answer(
        body.query, body.history,
        index_dir=idx if policy_qa.has_index(idx) else None,   # own index, else bundled
        provider=_provider,
        chat_model=os.environ["MODEL_CHAT"],
        embed_model=os.environ["MODEL_EMBEDDING"],
    )
    audit(username, "chat", metadata={
        "input": {"query": body.query, "history_turns": len(body.history)},
        "output": result,
    })
    return result

# ── chat/stream: same answer, but the reply streams live as SSE ─────
@app.post("/chat/stream")
async def chat_stream(body: ChatBody, user=Depends(require_scope("policy_qa"))):
    """Same contract as /chat, but the model's reply streams token-by-token
    (event: content) instead of arriving as one blocking response. See
    streaming.py for the SSE event contract; the final `answer`/`sources`
    payload still arrives as a single `result` event at the end."""
    username = user.get("sub", "unknown")
    idx = _user_index_dir(username)

    def run(emit):
        result = policy_qa.answer(
            body.query, body.history,
            index_dir=idx if policy_qa.has_index(idx) else None,
            provider=_provider,
            chat_model=os.environ["MODEL_CHAT"],
            embed_model=os.environ["MODEL_EMBEDDING"],
            emit=emit,
        )
        audit(username, "chat_stream", metadata={
            "input": {"query": body.query, "history_turns": len(body.history)},
            "output": result,
        })
        return result

    return await sse_stream(run)

# ── ingest: upload a policy → build THIS user's index ─────────────
@app.post("/ingest")
async def ingest(file: UploadFile = File(...), user=Depends(require_scope("policy_qa"))):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(422, f"Unsupported type {suffix!r}; allowed: {sorted(ALLOWED_SUFFIXES)}")
    username = user.get("sub", "unknown")
    idx = _user_index_dir(username)
    idx.mkdir(parents=True, exist_ok=True)

    raw = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=idx) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        if suffix in _EXTRACTED:                       # pdf/docx → text (OCR if scanned)
            text = extraction.extract_document(tmp_path, _provider, os.environ["MODEL_VISION"]).text
        else:                                          # txt/md → read as-is
            text = tmp_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            raise HTTPException(422, "No text could be extracted from the document.")
        source = idx / "source.txt"
        source.write_text(text, encoding="utf-8")
        info = policy_qa.build_index(source, idx, _provider, os.environ["MODEL_EMBEDDING"])
    finally:
        tmp_path.unlink(missing_ok=True)               # never leave the raw upload behind
    attachment_id = audit_client.upload_attachment(file.filename or tmp_path.name, raw)
    attachments = [{"filename": file.filename, "attachment_id": attachment_id}] if attachment_id else []
    audit(username, "ingest", resource=file.filename, metadata={
        "input": {"attachments": attachments},
        "output": info,
    })
    return {"ok": True, **info}

# ── delete: remove this user's index (chat falls back to bundled) ─
@app.delete("/index")
def delete_index(user=Depends(require_scope("policy_qa"))):
    username = user.get("sub", "unknown")
    idx = _user_index_dir(username)
    deleted = [n for n in ("chunks.json", "vectors.bin", "meta.json", "source.txt")
               if (idx / n).exists() and ((idx / n).unlink() or True)]
    audit(username, "delete_index", metadata={"input": {}, "output": {"deleted": deleted}})
    return {"ok": True, "deleted": deleted}
