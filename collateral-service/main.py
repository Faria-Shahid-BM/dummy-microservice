# collateral-service/main.py
import os, tempfile
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, Depends
from batch import collect_items, run_batch
from engines.collateral import review_collateral
from provider import Provider
from security import require_scope
from streaming import sse_stream
import httpx

def _models() -> dict:
    return {
        "extraction": os.environ["MODEL_EXTRACTION"],
        "vision":     os.environ["MODEL_VISION"],
    }

app = FastAPI()
_provider = Provider()

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

@app.post("/review")                    # Kong exposes this as POST /api/collateral/review
async def review(
    legal: UploadFile = File(...),
    property: UploadFile = File(...),
    user=Depends(require_scope("collateral")),   # 403 unless the JWT carries scope "collateral"
):
    audit(user=user.get("sub", "unknown"), service="collateral-service", action="review")
    with tempfile.TemporaryDirectory() as tmp:
        legal_path = Path(tmp) / (legal.filename or "legal.pdf")
        prop_path  = Path(tmp) / (property.filename or "property.pdf")
        legal_path.write_bytes(await legal.read())
        prop_path.write_bytes(await property.read())
        result = review_collateral(
            legal_path, prop_path, _provider,
            models=_models(),
            # prompts=None → engine uses its bundled .md files
            emit=None,                                          # no SSE in a simple service
        )

    return result


@app.post("/review/stream")             # Kong: POST /api/collateral/review/stream
async def review_stream(
    legal: UploadFile = File(...),
    property: UploadFile = File(...),
    user=Depends(require_scope("collateral")),   # same edge JWT + service scope check
):
    """Same review pipeline, but streamed as Server-Sent Events so the client
    sees live progress (stage/page events + observation tokens) instead of a
    single slow response. See streaming.py for the event contract."""
    audit(user=user.get("sub", "unknown"), service="collateral-service", action="review_stream")

    # Read the uploads here (async), then hand the bytes to the blocking
    # pipeline running on a worker thread inside sse_stream.
    legal_name = legal.filename or "legal.pdf"
    prop_name = property.filename or "property.pdf"
    legal_bytes = await legal.read()
    prop_bytes = await property.read()

    def run(emit):
        with tempfile.TemporaryDirectory() as tmp:
            legal_path = Path(tmp) / legal_name
            prop_path = Path(tmp) / prop_name
            legal_path.write_bytes(legal_bytes)
            prop_path.write_bytes(prop_bytes)
            return review_collateral(
                legal_path, prop_path, _provider,
                models=_models(),
                emit=emit,
            )

    return await sse_stream(run)


# ──────────────────────────────────────────────────────────────────────────────
# Batch review: N (legal opinion, property document) pairs in one request
# ──────────────────────────────────────────────────────────────────────────────

_SLOTS = {"legal": "legal.pdf", "property": "property.pdf"}


def _review_one(paths, emit=None):
    """One item of a batch: the same pipeline as /review, over batch.py's
    slot -> path mapping."""
    return review_collateral(
        paths["legal"], paths["property"], _provider,
        models=_models(),
        emit=emit,
    )


@app.post("/review/batch")              # Kong: POST /api/collateral/review/batch
async def review_batch(
    legal: list[UploadFile] = File(...),
    property: list[UploadFile] = File(...),
    item_ids: list[str] | None = Form(None),
    user=Depends(require_scope("collateral")),
):
    """N pairs in one request: legal[i] is cross-checked against property[i].
    Returns {"results": [{item, item_id, result|error}, ...]}."""
    items = await collect_items(
        {"legal": legal, "property": property}, item_ids, fallback_names=_SLOTS
    )
    audit(user=user.get("sub", "unknown"), service="collateral-service", action="review_batch")
    return run_batch(items, _review_one)


@app.post("/review/batch/stream")       # Kong: POST /api/collateral/review/batch/stream
async def review_batch_stream(
    legal: list[UploadFile] = File(...),
    property: list[UploadFile] = File(...),
    item_ids: list[str] | None = Form(None),
    user=Depends(require_scope("collateral")),
):
    """Streaming batch — each pair's result is streamed as it lands, so the
    client can show it without waiting for the rest. See batch.py for the
    item_start/item_result/item_error event contract."""
    items = await collect_items(
        {"legal": legal, "property": property}, item_ids, fallback_names=_SLOTS
    )
    audit(user=user.get("sub", "unknown"), service="collateral-service", action="review_batch_stream")

    return await sse_stream(lambda emit: run_batch(items, _review_one, emit))


