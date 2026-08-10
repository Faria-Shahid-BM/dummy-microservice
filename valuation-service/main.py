import os, tempfile
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, Depends
from batch import collect_items, run_batch
from engines.valuation import review_valuation
from provider import Provider
from security import require_scope
from streaming import sse_stream
import httpx

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


@app.post("/review")                     # Kong: POST /api/valuation/review
async def review(
    report: UploadFile = File(...),
    user=Depends(require_scope("valuation")),   # 403 unless the JWT carries scope "valuation"
):
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / (report.filename or "report.pdf")
        report_path.write_bytes(await report.read())
        audit(user=user.get("sub", "unknown"), service="Valuation-service", action="review")
        return review_valuation(
            report_path,
            None,                        # panel_path=None → bundled default_panel.xlsx
            _provider,
            models={
                "extraction": os.environ["MODEL_EXTRACTION"],
                "vision":     os.environ["MODEL_VISION"],
            },
            emit=None,
        )


@app.post("/review/stream")               # Kong: POST /api/valuation/review/stream
async def review_stream(
    report: UploadFile = File(...),
    user=Depends(require_scope("valuation")),
):
    """Same review pipeline, streamed as SSE stage events (extract_text,
    extract_fields, panel_check, done) so the client can show live progress
    instead of blocking on one long request. See streaming.py for the event
    contract. review_valuation() has no live content-token streaming (unlike
    collateral's observations step) — only discrete stage events."""
    audit(user=user.get("sub", "unknown"), service="Valuation-service", action="review_stream")

    report_name = report.filename or "report.pdf"
    report_bytes = await report.read()

    def run(emit):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / report_name
            report_path.write_bytes(report_bytes)
            return review_valuation(
                report_path,
                None,
                _provider,
                models={
                    "extraction": os.environ["MODEL_EXTRACTION"],
                    "vision":     os.environ["MODEL_VISION"],
                },
                emit=emit,
            )

    return await sse_stream(run)


# ──────────────────────────────────────────────────────────────────────────────
# Batch review: N valuation reports in one request
# ──────────────────────────────────────────────────────────────────────────────

_SLOTS = {"report": "report.pdf"}


def _review_one(paths, emit=None):
    """One item of a batch: the same pipeline as /review, over batch.py's
    slot -> path mapping."""
    return review_valuation(
        paths["report"],
        None,                            # panel_path=None → bundled default_panel.xlsx
        _provider,
        models={
            "extraction": os.environ["MODEL_EXTRACTION"],
            "vision":     os.environ["MODEL_VISION"],
        },
        emit=emit,
    )


@app.post("/review/batch")               # Kong: POST /api/valuation/review/batch
async def review_batch(
    report: list[UploadFile] = File(...),
    item_ids: list[str] | None = Form(None),
    user=Depends(require_scope("valuation")),
):
    """N reports in one request, each reviewed independently.
    Returns {"results": [{item, item_id, result|error}, ...]}."""
    items = await collect_items({"report": report}, item_ids, fallback_names=_SLOTS)
    audit(user=user.get("sub", "unknown"), service="Valuation-service", action="review_batch")
    return run_batch(items, _review_one)


@app.post("/review/batch/stream")        # Kong: POST /api/valuation/review/batch/stream
async def review_batch_stream(
    report: list[UploadFile] = File(...),
    item_ids: list[str] | None = Form(None),
    user=Depends(require_scope("valuation")),
):
    """Streaming batch — each report's result is streamed as it lands, so the
    client can show it without waiting for the rest. See batch.py for the
    item_start/item_result/item_error event contract."""
    items = await collect_items({"report": report}, item_ids, fallback_names=_SLOTS)
    audit(user=user.get("sub", "unknown"), service="Valuation-service", action="review_batch_stream")

    return await sse_stream(lambda emit: run_batch(items, _review_one, emit))
