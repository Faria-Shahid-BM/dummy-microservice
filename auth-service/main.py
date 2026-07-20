from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import jwt

app = FastAPI()

# One shared signing secret for every token this app issues — matches the
# single consumer registered in kong.yml. Kong's job is just to confirm a
# token came from us; which services/role it grants is decided here, from
# claims baked into the token, not from a separate per-user Kong secret.
JWT_SECRET = "supersecretkey"
JWT_ISSUER = "poc-app"

# In a real system this is a users table (hashed passwords, a live
# subscriptions table for `services`). Kept as a dict here since the point
# is the token shape, not the storage.
USERS = {
    "alice": {"password": "alicepw", "role": "admin", "services": ["service-a"]},
    "bob": {"password": "bobpw", "role": "viewer", "services": ["service-b"]},
}


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/login")
def login(req: LoginRequest):
    user = USERS.get(req.username)
    if not user or user["password"] != req.password:
        raise HTTPException(status_code=401, detail="invalid username or password")

    token = jwt.encode(
        {
            "iss": JWT_ISSUER,
            "username": req.username,
            "role": user["role"],
            "services": user["services"],
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    return {"token": token, "username": req.username, "role": user["role"], "services": user["services"]}
