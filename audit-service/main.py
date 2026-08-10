from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime
import json, os

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

@app.get("/audit")
def get_logs():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        return [json.loads(line) for line in f.readlines()]