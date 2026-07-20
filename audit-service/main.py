from fastapi import FastAPI, Request, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime
import json, os
import jwt

app = FastAPI()

os.makedirs("logs", exist_ok=True)
LOG_FILE = "logs/audit.log"

class AuditEvent(BaseModel):
    user_id: str
    service: str
    action: str
    resource: str | None = None
    metadata: dict | None = None

@app.post("/audit")
def log_event(event: AuditEvent):
    entry = {
        **event.model_dump(),
        "timestamp": datetime.utcnow().isoformat()
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return {"status": "logged"}

def require_admin(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="admin role required")
    claims = jwt.decode(auth.split(" ", 1)[1], options={"verify_signature": False})
    if claims.get("role") != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return claims

@app.get("/")
def get_logs(claims: dict = Depends(require_admin)):
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        return [json.loads(line) for line in f.readlines()]
