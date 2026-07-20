# API Gateway POC

A small proof-of-concept showing Kong as a single API gateway in front of two dummy microservices, with JWT authentication, per-service ACL entitlement gating, in-app role checks, and centralized audit logging.

## Containers

- **kong** — the gateway. Routes traffic, verifies JWTs, enforces which consumer can reach which service (ACL).
- **service-a** / **service-b** — dummy FastAPI services. No auth code of their own; they trust that anything reaching them already passed Kong.
- **audit-service** — internal-only FastAPI service that records events from service-a/b to `audit-logs/audit.log`. Not exposed through Kong, no `ports:` mapping — unreachable from outside Docker.

## Request flow

```
                              CLIENT
                                │
                 Authorization: Bearer <JWT>
                                │
                                ▼
                  ┌───────────────────────────┐
                  │           KONG            │
                  │  JWT plugin (global)      │
                  │  - verify signature       │
                  │  - resolve consumer via   │
                  │    `iss` claim             │
                  └───────────────────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              │                                    │
      no / invalid token                     valid token
              │                                    │
              ▼                                    ▼
        401 Unauthorized              route-specific ACL check
       "who even are you"                          │
                          ┌───────────────────────┴───────────────────────┐
                          │                                               │
                   route: /api/a                                  route: /api/b
              requires group:                                requires group:
              service-a-access                               service-b-access
                          │                                               │
              ┌───────────┴───────────┐                       ┌───────────┴───────────┐
              │                       │                       │                       │
        consumer IS              consumer NOT            consumer IS              consumer NOT
        in group                 in group                in group                 in group
              │                       │                       │                       │
              ▼                       ▼                       ▼                       ▼
      forward to service-a      403 Forbidden          forward to service-b      403 Forbidden
                              "wrong plan for this"                           "wrong plan for this"
              │                                                       │
              └───────────────────────────┬───────────────────────────┘
                                           │
                                (fire-and-forget, background)
                                           ▼
                                ┌───────────────────────┐
                                │     audit-service      │
                                │  (no Kong route, no    │
                                │   exposed port —        │
                                │   internal-only)        │
                                └───────────────────────┘
                                           │
                                           ▼
                                 audit-logs/audit.log
```

401 vs 403 is the key distinction: **401** means Kong doesn't know who's asking (missing/invalid token). **403** from Kong's ACL means it knows exactly who's asking, they just haven't bought that service. A **403** from inside a service (e.g. `/admin-only`) means something different again — they bought the service, they just don't have the right role for that specific action.

## Running it

```powershell
docker compose up --build
```

Kong listens on `localhost:80` (proxy) and `localhost:8001` (admin API). `service-a`, `service-b`, and `audit-service` have no exposed ports — they're only reachable through Kong or from each other over the internal Docker network.

## Auth

Two consumers are pre-configured in `kong.yml`:

| Consumer | `iss` claim | secret | ACL group | can reach |
|---|---|---|---|---|
| `poc-user` | `poc-issuer` | `mysecret123` | `service-a-access` | `/api/a/*` |
| `poc-user-b` | `poc-issuer-b` | `mysecret456` | `service-b-access` | `/api/b/*` |

Tokens are signed HS256 JWTs. Generate one at [jwt.io](https://jwt.io) with the matching `iss`/secret pair, plus an optional `role` claim (e.g. `"role": "admin"`) to pass the `/admin-only` endpoints.

```powershell
$TOKEN = "paste your token here"
curl -UseBasicParsing -Headers @{"Authorization"="Bearer $TOKEN"} http://localhost/api/a/hello
```

## Endpoints

Each service exposes:
- `GET /hello` — returns identity info, fires an audit event
- `GET /health-a` (or `/health-b`) — health check
- `GET /admin-only` — requires the JWT's `role` claim to be `"admin"`, otherwise `403`
