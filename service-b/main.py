from fastapi import FastAPI, Request, HTTPException
import base64
import httpx
import json

app = FastAPI()

def audit(user: str, service: str, action: str, resource: str = None):
    try:
        httpx.post(
            "http://audit-service:8000/audit",
            json={"user_id": user, "service": service, "action": action, "resource": resource},
            timeout=1.0
        )
    except Exception:
        pass

def get_role(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return "none"
    try:
        payload_b64 = auth.split(" ", 1)[1].split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        return claims.get("role", "none")
    except Exception:
        return "none"

@app.get("/hello")
def hello(request: Request):
    user = request.headers.get("x-consumer-username", "anonymous")
    audit(user, "service-b", "hello.called")
    return {"service": "b", "message": "Hello from Service B!", "user": user}

@app.get("/admin-only")
def admin_only(request: Request):
    user = request.headers.get("x-consumer-username", "anonymous")
    role = get_role(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    audit(user, "service-b", "admin_only.called")
    return {"service": "b", "message": "Hello, admin!", "user": user, "role": role}

@app.get("/health-b")
def health():
    return {"status": "ok", "service": "b"}
