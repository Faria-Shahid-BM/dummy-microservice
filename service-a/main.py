from fastapi import FastAPI, HTTPException, Depends
from authlib import get_claims, audit

app = FastAPI()

@app.get("/hello")
def hello(claims: dict = Depends(get_claims)):
    user = claims.get("username", "anonymous")
    audit(user, "service-a", "hello.called")
    return {"service": "a", "message": "Hello from Service A!", "user": user}

@app.get("/admin-only")
def admin_only(claims: dict = Depends(get_claims)):
    user = claims.get("username", "anonymous")
    role = claims.get("role", "none")
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    audit(user, "service-a", "admin_only.called")
    return {"service": "a", "message": "Hello, admin!", "user": user, "role": role}

@app.get("/health-a")
def health():
    return {"status": "ok", "service": "a"}
