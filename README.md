# API Gateway POC — Kong + FastAPI

A proof-of-concept microservices setup that puts **[Kong Gateway](https://konghq.com/)** in front of several FastAPI services. It demonstrates a realistic API-gateway pattern: centralized routing, **JWT authentication** enforced at the edge, **CORS**, identity propagation to upstream services, and a simple **audit trail** — all wired together with Docker Compose, plus a single-page frontend to drive it.

---

## Architecture

```
                        ┌──────────────────────────────┐
   Browser / Client     │            Kong (:80)         │
   (frontend/index.html)│   routing · JWT · CORS        │
          │             └──────────────────────────────┘
          │                    │        │        │        │
          ▼                    ▼        ▼        ▼        ▼
     http://localhost    /api/auth  /api/a   /api/b   /api/audit
                            │         │        │         │
                            ▼         ▼        ▼         ▼
                       auth-service service-a service-b audit-service
                       (issue JWT)  (protected, JWT)   (log store)
                                        │        │         ▲
                                        └────────┴─────────┘
                                       fire-and-forget audit events
```

- All traffic enters through **Kong** on port `80`.
- Kong validates the **JWT** for protected routes (`/api/a`, `/api/b`) and injects the caller's identity as the `X-Consumer-Username` header for upstreams.
- **service-a** and **service-b** read that header and emit a best-effort audit event to **audit-service**.
- **audit-service** appends events to a log file mounted on the host.

---

## Services

| Service         | Route (via Kong)        | Auth (JWT) |Description                                                        |
|-----------------|-------------------------|:----------:|--------------------------------------------------------------------|
| `auth-service`  | `POST /api/auth/login`  | ❌         | Validates credentials and issues an HS256 JWT.                     |
| `service-a`     | `GET /api/a/hello`      | ✅         | Returns a greeting; emits an audit event.                          |
| `service-b`     | `GET /api/b/hello`      | ✅         | Same as service-a, tagged as `service-b`.                          |
| `audit-service` | `GET/POST /api/audit/audit` | ❌     | Stores and returns audit events (JSON lines).                      |
| `kong`          | `:80` (proxy), `:8001` (admin) | —   | API gateway. Declarative config, DB-less mode.                     |

> **Note:** `strip_path: true` means the route prefix is removed before Kong forwards. So `GET /api/a/hello` reaches service-a as `GET /hello`, and `POST /api/auth/login` reaches auth-service as `POST /login`.

---

## Tech stack

- **Kong 3** (DB-less / declarative config via `kong.yml`)
- **FastAPI** + **Uvicorn** (Python 3.12)
- **PyJWT** for token signing/verification
- **Docker Compose** for orchestration
- Vanilla **HTML/CSS/JS** frontend (no build step)

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- A modern web browser (to use the frontend)

---

## Getting started

### 1. Start the stack

```bash
docker compose up --build
```

This launches Kong and all four services. Kong waits until it's healthy before the services start.

### 2. Open the frontend

Open `frontend/index.html` directly in your browser (double-click, or serve it with any static server). It talks to Kong at `http://localhost`.

The UI lets you:
- **Sign in** to get a JWT (`admin` / `password123` by default)
- **Call service A / B** through the gateway (requires the token)
- **View the audit log** updating live

### 3. Or use the API directly

```bash
# 1. Log in to get a JWT
TOKEN=$(curl -s -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"password123"}' | jq -r .token)

# 2. Call a protected service through Kong
curl http://localhost/api/a/hello -H "Authorization: Bearer $TOKEN"
# -> {"service":"a","user":"poc-user"}

# 3. Calling without a token is rejected by Kong (401)
curl -i http://localhost/api/a/hello

# 4. Read the audit log
curl http://localhost/api/audit/audit
```

---

## Demo credentials

Defined in [`auth-service/main.py`](auth-service/main.py):

| Username | Password      |
|----------|---------------|
| `admin`  | `password123` |
| `alice`  | `alicepass`   |

---

## How the JWT flow works

1. The client posts credentials to `auth-service`, which returns a JWT signed with the shared secret `mysecret123` and issuer `poc-issuer`.
2. The client sends that token as `Authorization: Bearer <token>` on subsequent requests.
3. **Kong's JWT plugin** verifies the signature against the consumer's `jwt_secret` (matched by the `iss` claim `poc-issuer`) — configured in [`kong.yml`](kong.yml).
4. On success, Kong forwards the request upstream and adds `X-Consumer-Username: poc-user`. Services trust this header for identity — they never see or verify the raw token themselves.

---


## Configuration reference

Key values (hard-coded for the POC — change before any real use):

| Setting          | Value           | Where                         |
|------------------|-----------------|-------------------------------|
| JWT secret       | `mysecret123`   | `kong.yml`, `auth-service/main.py` |
| JWT issuer (`iss`)| `poc-issuer`   | `kong.yml`, `auth-service/main.py` |
| Kong consumer    | `poc-user`      | `kong.yml`                    |
| Token expiry     | 2 hours         | `auth-service/main.py`        |
| Kong proxy port  | `80`            | `docker-compose.yml`          |
| Kong admin port  | `8001`          | `docker-compose.yml`          |

---
