from fastapi import FastAPI, Request, HTTPException, Depends
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

def get_claims(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return {}
    try:
        payload_b64 = auth.split(" ", 1)[1].split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}

def require_service_access(request: Request) -> dict:
    claims = get_claims(request)
    if "service-a" not in claims.get("services", []):
        raise HTTPException(status_code=403, detail="not entitled to service-a")
    return claims

@app.get("/hello")
def hello(claims: dict = Depends(require_service_access)):
    user = claims.get("username", "anonymous")
    audit(user, "service-a", "hello.called")
    return {"service": "a", "message": "Hello from Service A!", "user": user}

@app.get("/admin-only")
def admin_only(claims: dict = Depends(require_service_access)):
    user = claims.get("username", "anonymous")
    role = claims.get("role", "none")
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    audit(user, "service-a", "admin_only.called")
    return {"service": "a", "message": "Hello, admin!", "user": user, "role": role}

@app.get("/health-a")
def health():
    return {"status": "ok", "service": "a"}
