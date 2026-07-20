from fastapi import FastAPI, Request
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

@app.get("/hello")
def hello(request: Request):
    # The only place your code touches any of 
    # this is reading the header Kong injected 
    user = request.headers.get("x-consumer-username", "anonymous")
    audit(user, "service-a", "hello.called")  # ← this triggers POST /audit
    return {"service": "a", "user": user}

@app.get("/health")
def health():
    return {"status": "ok"}