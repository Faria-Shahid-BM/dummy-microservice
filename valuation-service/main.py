import os, tempfile
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Depends
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
