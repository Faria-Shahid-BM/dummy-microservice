from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from pathlib import Path
import os
import tempfile

from claims import get_claims
from audit import audit
from engine import review_collateral
from llm_provider import OpenAICompatProvider

app = FastAPI()

ALLOWED_SUFFIXES = {".docx", ".pdf"}

_provider: OpenAICompatProvider | None = None


def get_provider() -> OpenAICompatProvider:
    global _provider
    if _provider is None:
        _provider = OpenAICompatProvider()
    return _provider


def _save_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"unsupported file type: {suffix or '(none)'}")
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(upload.file.read())
    return Path(tmp_path)


@app.post("/compare")
def compare(
    legal: UploadFile = File(...),
    property: UploadFile = File(...),
    claims: dict = Depends(get_claims),
):
    legal_path = _save_upload(legal)
    property_path = _save_upload(property)
    try:
        result = review_collateral(
            legal_path,
            property_path,
            get_provider(),
            models={
                "extraction": os.environ.get("LLM_MODEL_EXTRACTION", "gpt-4o-mini"),
                "vision": os.environ.get("LLM_MODEL_VISION", "gpt-4o-mini"),
            },
        )
    finally:
        legal_path.unlink(missing_ok=True)
        property_path.unlink(missing_ok=True)

    user = claims.get("username", "anonymous")
    audit(user, "collateral-reviewer", "compare.called")
    return result


@app.get("/health-collateral")
def health():
    return {"status": "ok", "service": "collateral-reviewer"}
