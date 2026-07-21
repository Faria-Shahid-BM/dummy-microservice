from fastapi import FastAPI, HTTPException, Depends
from authlib import get_claims, audit

app = FastAPI()

@app.get("/hello")
def hello(claims: dict = Depends(get_claims)):
    user = claims.get("username", "anonymous")
    audit(user, "service-b", "hello.called")
    return {"service": "b", "message": "Hello from Service B!", "user": user}

@app.get("/admin-only")
def admin_only(claims: dict = Depends(get_claims)):
    user = claims.get("username", "anonymous")
    role = claims.get("role", "none")
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    audit(user, "service-b", "admin_only.called")
    return {"service": "b", "message": "Hello, admin!", "user": user, "role": role}

@app.get("/health-b")
def health():
    return {"status": "ok", "service": "b"}
