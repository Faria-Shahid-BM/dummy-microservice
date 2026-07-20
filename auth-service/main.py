from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import jwt, datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET = "mysecret123"
USERS = {
    "admin": "password123",
    "alice": "alicepass",
}

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/login")
def login(body: LoginRequest):
    if USERS.get(body.username) != body.password:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": "Invalid credentials"})
    token = jwt.encode(
        {
            "iss": "poc-issuer",
            "sub": body.username,
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=2)
        },
        SECRET,
        algorithm="HS256"
    )
    return {"token": token, "username": body.username}