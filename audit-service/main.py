from fastapi import FastAPI, Depends
from pydantic import BaseModel
from datetime import datetime
import json, os
from claims import require_role

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

@app.get("/")
def get_logs(claims: dict = Depends(require_role("admin")), limit: int = 20, offset: int = 0):
    if not os.path.exists(LOG_FILE):
        return {"items": [], "total": 0}
    with open(LOG_FILE) as f:
        entries = [json.loads(line) for line in f.readlines()]
    entries.reverse()  # most recent first
    return {"items": entries[offset:offset + limit], "total": len(entries)}
