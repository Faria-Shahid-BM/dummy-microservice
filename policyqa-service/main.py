import asyncio
import os, re, tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

import audit_client
import outbox
from engines import policy_qa, extraction
import config_client
from provider import Provider
from security import get_raw_token, require_scope
from streaming import sse_stream

_provider = Provider()

# Persistent per-user index storage (mounted as a Docker volume).
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
INDEXES_DIR = DATA_DIR / "indexes"
ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}
_EXTRACTED = {".pdf", ".docx"}   # these go through text/OCR extraction first

# This service has no database of its own — its state is entirely files on
# disk (indexes/, above). It gets a small dedicated SQLite DB purely to hold
# the audit outbox (see outbox.py), so an audit event still survives
# audit-service being briefly unreachable. Unlike case_store.py's services,
# there's no surrounding DB transaction for the outbox write to be atomic
# WITH — the index build/delete itself isn't transactional either — so this
# gets "delivery can't be silently lost" but not "atomic with the mutation".
DATA_DIR.mkdir(parents=True, exist_ok=True)
_outbox_engine = create_engine(
    f"sqlite:///{DATA_DIR / 'outbox.db'}", connect_args={"check_same_thread": False}
)
_OutboxSessionLocal = sessionmaker(bind=_outbox_engine, autoflush=False, expire_on_commit=False)


class _OutboxBase(DeclarativeBase):
    pass


_OUTBOX = outbox.outbox_table(_OutboxBase.metadata)
_OutboxBase.metadata.create_all(_outbox_engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(outbox.run_relay(_OutboxSessionLocal, _OUTBOX))
    yield


app = FastAPI(lifespan=lifespan)


def audit(token, action, resource=None, metadata=None):
    db = _OutboxSessionLocal()
    try:
        outbox.enqueue(db, _OUTBOX, service="policyqa-service", action=action, token=token,
                        resource=resource, detail=metadata)
        db.commit()
    finally:
        db.close()

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

# Fallback for when config-service can't answer — the values this service used
# to read directly. See config_client.models_for.
def _default_models() -> dict:
    return {
        "chat": os.environ["MODEL_CHAT"],
        "embedding": os.environ["MODEL_EMBEDDING"],
        "vision": os.environ["MODEL_VISION"],
    }


def _models(token: str | None) -> dict:
    return config_client.models_for("policy_qa", token, _default_models())


# ── chat: use the user's own index if present, else the bundled one ─
class ChatBody(BaseModel):
    query: str
    history: list[dict] = []

@app.post("/chat")
def chat(body: ChatBody, user=Depends(require_scope("policy_qa")), token: str | None = Depends(get_raw_token)):
    username = user.get("sub", "unknown")
    idx = _user_index_dir(username)
    models = _models(token)
    result = policy_qa.answer(
        body.query, body.history,
        index_dir=idx if policy_qa.has_index(idx) else None,   # own index, else bundled
        provider=_provider,
        chat_model=models["chat"],
        embed_model=models["embedding"],
    )
    audit(token, "chat", metadata={
        "input": {"query": body.query, "history_turns": len(body.history)},
        "output": result,
    })
    return result

# ── chat/stream: same answer, but the reply streams live as SSE ─────
@app.post("/chat/stream")
async def chat_stream(
    body: ChatBody, user=Depends(require_scope("policy_qa")), token: str | None = Depends(get_raw_token)
):
    """Same contract as /chat, but the model's reply streams token-by-token
    (event: content) instead of arriving as one blocking response. See
    streaming.py for the SSE event contract; the final `answer`/`sources`
    payload still arrives as a single `result` event at the end."""
    username = user.get("sub", "unknown")
    idx = _user_index_dir(username)

    models = _models(token)

    def run(emit):
        result = policy_qa.answer(
            body.query, body.history,
            index_dir=idx if policy_qa.has_index(idx) else None,
            provider=_provider,
            chat_model=models["chat"],
            embed_model=models["embedding"],
            emit=emit,
        )
        audit(token, "chat_stream", metadata={
            "input": {"query": body.query, "history_turns": len(body.history)},
            "output": result,
        })
        return result

    return await sse_stream(run)

# ── ingest: upload a policy → build THIS user's index ─────────────
@app.post("/ingest")
async def ingest(
    file: UploadFile = File(...), user=Depends(require_scope("policy_qa")), token: str | None = Depends(get_raw_token)
):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(422, f"Unsupported type {suffix!r}; allowed: {sorted(ALLOWED_SUFFIXES)}")
    username = user.get("sub", "unknown")
    idx = _user_index_dir(username)
    idx.mkdir(parents=True, exist_ok=True)

    models = _models(token)          # one lookup for both steps below
    raw = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=idx) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        if suffix in _EXTRACTED:                       # pdf/docx → text (OCR if scanned)
            text = extraction.extract_document(tmp_path, _provider, models["vision"]).text
        else:                                          # txt/md → read as-is
            text = tmp_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            raise HTTPException(422, "No text could be extracted from the document.")
        source = idx / "source.txt"
        source.write_text(text, encoding="utf-8")
        info = policy_qa.build_index(source, idx, _provider, models["embedding"])
    finally:
        tmp_path.unlink(missing_ok=True)               # never leave the raw upload behind
    attachment_id = audit_client.upload_attachment(file.filename or tmp_path.name, raw)
    attachments = [{"filename": file.filename, "attachment_id": attachment_id}] if attachment_id else []
    audit(token, "ingest", resource=file.filename, metadata={
        "input": {"attachments": attachments},
        "output": info,
    })
    return {"ok": True, **info}

# ── delete: remove this user's index (chat falls back to bundled) ─
@app.delete("/index")
def delete_index(user=Depends(require_scope("policy_qa")), token: str | None = Depends(get_raw_token)):
    username = user.get("sub", "unknown")
    idx = _user_index_dir(username)
    deleted = [n for n in ("chunks.json", "vectors.bin", "meta.json", "source.txt")
               if (idx / n).exists() and ((idx / n).unlink() or True)]
    audit(token, "delete_index", metadata={"input": {}, "output": {"deleted": deleted}})
    return {"ok": True, "deleted": deleted}
