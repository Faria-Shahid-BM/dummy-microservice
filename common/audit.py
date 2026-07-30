import httpx


def audit(user: str, service: str, action: str, resource: str = None):
    try:
        httpx.post(
            "http://audit-service:8000/audit",
            json={"user_id": user, "service": service, "action": action, "resource": resource},
            timeout=1.0,
        )
    except Exception:
        pass
