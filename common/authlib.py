from fastapi import Request
import base64
import httpx
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


def audit(user: str, service: str, action: str, resource: str = None):
    try:
        httpx.post(
            "http://audit-service:8000/audit",
            json={"user_id": user, "service": service, "action": action, "resource": resource},
            timeout=1.0,
        )
    except Exception:
        pass
