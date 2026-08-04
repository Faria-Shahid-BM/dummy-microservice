# collateral-service/main.py
import os, tempfile
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Depends
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
