import os, tempfile
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Depends
from engines.insurance import review_insurance
from provider import Provider
from security import require_scope
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


@app.post("/review")                     # Kong: POST /api/insurance/review
async def review(
    policy: UploadFile = File(...),
    user=Depends(require_scope("insurance")),   # 403 unless the JWT carries scope "insurance"
):
    with tempfile.TemporaryDirectory() as tmp:
        policy_path = Path(tmp) / (policy.filename or "policy.pdf")
        policy_path.write_bytes(await policy.read())
        audit(user=user.get("sub", "unknown"), service="Insurance-service", action="review")
        return review_insurance(
            policy_path,
            _provider,
            models={
                "extraction": os.environ["MODEL_EXTRACTION"],  # analysis-grade model
                "vision":     os.environ["MODEL_VISION"],
            },
            emit=None,
        )
