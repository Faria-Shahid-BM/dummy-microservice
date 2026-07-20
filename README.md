# API Gateway POC

A proof-of-concept showing Kong as a single API gateway in front of a set of dummy microservices, with real login-issued JWTs, claims-based service entitlement, in-app role checks, and centralized audit logging — plus an Angular frontend to drive the whole thing.

## What this demonstrates

- API gateway pattern — one entry point, no service reachable directly
- JWT authentication vs. claims-based authorization as two distinct, separable concerns
- Defense-in-depth reasoning (why a service might re-verify a signature even though Kong already did)
- Role-based permission checks living inside a service, separate from "can you reach this service at all"
- Docker multi-container orchestration with Docker Compose
- A frontend (Angular) driving a real login flow against the above, not hardcoded tokens

## Containers

- **kong** — the gateway. Every external request goes through it first. Verifies JWTs are genuine; does *not* decide who can do what.
- **auth-service** — issues tokens. `POST /login` checks a username/password and signs a JWT containing that user's `role` and `services` (what they're entitled to).
- **service-a** / **service-b** — dummy FastAPI services. No login logic of their own, but each one *does* read the token's claims to decide whether it's the right destination and whether the caller's role allows a given action.
- **audit-service** — internal-only. Receives events from service-a/b directly over the Docker network (never through Kong) and writes them to `audit-logs/audit.log`. Exposes exactly one thing through Kong: a `GET` route to read the log back, gated to admin-role tokens only.
- **frontend** (`frontend/`, Angular, not in `docker-compose.yml`) — a demo console: log in, call an endpoint, watch the response, view the audit trail.

## Why one shared secret instead of one-secret-per-user

Kong's JWT plugin can be set up with a separate secret per user (its `consumers` model), which was the first version of this POC. It got replaced with a single shared secret because it maps better to how real systems actually work: **one issuer signs every token**, and *what that token grants* — which services, what role — is data carried inside the token's claims, decided at login time by checking a real subscriptions table (a `USERS` dict, here). Kong's job shrinks to one question: *is this signature genuine?* Every other decision — "can this token reach service-b," "does this role allow admin actions" — happens inside the service that receives the request, by reading the claims itself.

## Flow 1 — Logging in (issuing a token)

```
   BROWSER (Angular, localhost:4200)
        │
        │ POST /api/auth/login  { username, password }
        ▼
   ┌──────────────────────────┐
   │           KONG            │   route: /api/auth — no jwt plugin here.
   │   (just forwards it)      │   Can't require a token to get your first one.
   └──────────────────────────┘
        │
        ▼
   ┌──────────────────────────┐
   │       auth-service        │
   │  check username/password  │
   │  against USERS            │
   │                            │
   │  sign a JWT:               │
   │    iss:      "poc-app"     │
   │    username: "alice"       │
   │    role:     "admin"       │
   │    services: ["service-a"] │
   └──────────────────────────┘
        │
        ▼
   { token, username, role, services } → stored in the browser (in memory)
```

## Flow 2 — Calling a service (using the token)

```
   BROWSER
        │  Authorization: Bearer <token>
        ▼
   ┌──────────────────────────┐
   │           KONG            │
   │   jwt plugin (per-route)  │
   │   - verify signature      │
   │     against the ONE       │
   │     registered secret     │
   └──────────────────────────┘
        │
   ┌────┴─────┐
   │           │
 missing/    valid
 invalid     signature
   │           │
   ▼           ▼
  401     forwarded to service-a (or service-b)
"who even        │
 are you"         ▼
         ┌──────────────────────────────┐
         │      service-a / service-b     │
         │                                  │
         │ 1) read claims straight out of  │
         │    the token (Kong already      │
         │    verified it — no need to     │
         │    re-check the signature)      │
         │                                  │
         │ 2) "service-a" in services?      │
         │      no  → 403 not entitled      │
         │      yes → continue              │
         │                                  │
         │ 3) (role-gated routes only,      │
         │    e.g. /admin-only)             │
         │    role == "admin"?              │
         │      no  → 403 wrong role        │
         │      yes → continue              │
         └──────────────────────────────┘
                        │
                        ▼
                handle the request
                        │
          (fire-and-forget, background)
                        ▼
         ┌──────────────────────────┐
         │      audit-service        │
         │  POST /audit — direct     │
         │  container-to-container   │
         │  call, no Kong, no token  │
         └──────────────────────────┘
                        │
                        ▼
               audit-logs/audit.log
```

The two `403`s look identical from outside but come from different checks: step 2 means *"you're not entitled to this service at all"* (a plan/subscription problem); step 3 means *"you're entitled to be here, you just don't have the right role for this specific action"* (a permissions problem). A `401` is different again — Kong rejecting before anyone even knows who's asking.

## Flow 3 — Reading the audit trail (admin only)

```
   BROWSER  →  GET /api/audit  (Authorization: Bearer <token>)
        │
        ▼
   KONG (jwt plugin: is this signature genuine?)
        │
        ▼
   audit-service  →  decodes the token's own claims
        │
   role == "admin"?
        │
   ┌────┴────┐
   no        yes
   │          │
   ▼          ▼
  403    returns every logged event
```

Same pattern as Flow 2's role check, just living in `audit-service` instead of `service-a`/`service-b`.

## Running it

Backend:
```powershell
docker compose up --build
```
Kong listens on `localhost:80` (proxy) and `localhost:8001` (admin API). Every other container — `service-a`, `service-b`, `audit-service`, `auth-service` — has no exposed ports; they're reachable only through Kong or from each other over the internal Docker network.

Frontend:
```powershell
cd frontend
npm start
```
Opens on `localhost:4200`. Kong has a `cors` plugin configured specifically for that origin.

## Demo accounts

| Username | Password | Role | Entitled to |
|---|---|---|---|
| `alice` | `alicepw` | `admin` | `service-a` |
| `bob` | `bobpw` | `viewer` | `service-b` |

Log in as `alice` to see `/api/a/hello`, `/api/a/admin-only`, and the audit trail all succeed, and `/api/b/hello` come back `403` (wrong service). Log in as `bob` to see the reverse, plus a `403` on `/api/b/admin-only` (right service, wrong role) and on the audit trail (not an admin).

> These credentials, and the shared signing secret in `kong.yml`/`auth-service`, are hardcoded for demo purposes only — not something to carry into a real deployment as-is.

## Endpoints

| Method & path (through Kong) | Behind it | Guarded by |
|---|---|---|
| `POST /api/auth/login` | `auth-service` | nothing — this *is* the login |
| `GET /api/a/hello`, `GET /api/b/hello` | `service-a`/`service-b` | valid token + `services` claim |
| `GET /api/a/admin-only`, `GET /api/b/admin-only` | `service-a`/`service-b` | valid token + `services` claim + `role: admin` |
| `GET /api/a/health-a`, `GET /api/b/health-b` | `service-a`/`service-b` | valid token + `services` claim (same route-level `jwt` plugin covers everything under `/api/a`, `/api/b`) |
| `GET /api/audit` | `audit-service` | valid token + `role: admin` |

## Production considerations

This runs locally via Docker Desktop, which is purely a dev convenience — a GUI plus a lightweight Linux VM so Docker's engine (a Linux technology) works on Mac/Windows at all. Production servers are already Linux, so they run that same engine directly; the architecture here — one gateway, no service exposed except through it, JWT verification, claims-based entitlement — carries over conceptually. `docker-compose.yml` itself is even a legitimate way to run something this size in production (a single VM running `docker compose up -d`), not something you're forced to replace with Kubernetes.

What would actually need to change before this is a real deployment, not a POC:

- **Images get built once and pushed to a registry** (Docker Hub, AWS ECR, etc.) by a CI pipeline. Production pulls a pre-built image; it doesn't run `docker build` on the live server.
- **Secrets move out of the code.** The `supersecretkey` constant and the `USERS` dict in `auth-service` stand in for what would be a real users table (a database) plus a real secrets manager (AWS Secrets Manager, Vault, Kubernetes Secrets).
- **TLS becomes real.** This POC runs on plain `http://localhost`; production terminates actual HTTPS, usually at Kong itself or a load balancer in front of it.
- **The audit log needs a real store.** A bind-mounted file on one machine (`audit-logs/audit.log`) doesn't survive a restart or work across multiple replicas — that becomes a database table or a logging service (Datadog, Elasticsearch) instead.
- **The frontend stops being a dev server.** `ng serve` is strictly local; production runs `ng build` once and serves the resulting static files from a CDN or a plain web server container — no Node process running at all.
- **Each service re-verifying the JWT signature itself** (rather than trusting Kong's word for it) is a defense-in-depth improvement worth making before this handles anything real. Right now `service-a`/`service-b`/`audit-service` decode a token's claims without checking the signature — safe only because nothing can reach them except through Kong. If that network isolation ever breaks, a forged token would be accepted with no cryptographic check at all.

For this scale (four small services), a managed container platform (AWS ECS Fargate, Google Cloud Run, or even a single VM running this same Compose file with real secrets/TLS swapped in) gets to production-shaped without the operational overhead of a full Kubernetes cluster — that's worth reaching for once actual scale (autoscaling, zero-downtime rolling deploys across many instances) is needed, not before.
