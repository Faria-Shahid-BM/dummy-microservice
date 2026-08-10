# document-diff-service/main.py
import tempfile
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from engines import extraction, document_diff
from security import require_scope
import httpx

app = FastAPI()

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

def _read(slot, path):
    if path.suffix.lower() == ".pdf" and extraction.is_scanned_pdf(path):
        raise HTTPException(422, f"'{slot}' is a scanned PDF; this service needs text documents")
    text = extraction.extract_text(path)
    if not text.strip():
        raise HTTPException(422, f"No text extracted from '{slot}'")
    return text

@app.post("/compare")                   # Kong: POST /api/docdiff/compare
async def compare(
    original: UploadFile = File(...),
    returned: UploadFile = File(...),
    user=Depends(require_scope("docdiff")),   # 403 unless the JWT carries scope "docdiff"
):
    with tempfile.TemporaryDirectory() as tmp:
        o = Path(tmp) / (original.filename or "original.docx"); o.write_bytes(await original.read())
        r = Path(tmp) / (returned.filename or "returned.docx"); r.write_bytes(await returned.read())
        audit(user=user.get("sub", "unknown"), service="docdiff-service", action="compare")
        return document_diff.compare_documents(_read("original", o), _read("returned", r))
