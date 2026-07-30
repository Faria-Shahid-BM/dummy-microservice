from fastapi import Request, HTTPException, Depends
import base64
import json


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


def require_role(role: str):
    def checker(claims: dict = Depends(get_claims)) -> dict:
        if claims.get("role") != role:
            raise HTTPException(status_code=403, detail=f"{role} role required")
        return claims

    return checker
