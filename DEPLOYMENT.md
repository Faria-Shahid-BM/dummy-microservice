# Deployment Handoff Guide

How to take this system — currently running on a developer's PC via `docker compose` plus `ng serve` — and stand it up on the client's server so real users can reach it over the network.

This guide targets a **single Linux server** running Docker (the smallest leap from the current setup). Where a bank would later split this across Kubernetes / multiple hosts, it's noted, but everything here works as one machine first.

---

## 1. The big picture: what changes vs. your PC

On your PC today:

- The **backend** (Kong + 6 services) runs under `docker compose`, reachable at `http://localhost`.
- The **frontend** is *not* deployed — you run it separately with `ng serve` on `http://localhost:4200`, and it talks to `http://localhost` (Kong).
- Everything resolves to `localhost` because your browser and the containers are on the same machine.

On the client's server, the browser is on a **different machine** than the backend. So three things must change:

| On your PC | On the server |
|---|---|
| Frontend run via `ng serve` (dev server) | Frontend **built once** into static files, served by a web server (nginx) |
| `KONG_BASE = 'http://localhost'` | Points at the server's real address (or is made relative — see §4) |
| Browser + backend both on `localhost` | Users reach a **single URL** (e.g. `https://gateway.bank.internal`); nginx serves the app and forwards `/api/*` to Kong |

**Recommended shape:** put one **nginx "front door"** on the server. It serves the built Angular files *and* reverse-proxies `/api/*` to Kong. That gives users one URL, one origin, TLS in one place, and eliminates CORS entirely.

```
                          ┌─────────────────── the server ───────────────────┐
  user's browser  ──TLS──▶│  nginx (:443)                                     │
  https://gateway/...     │   ├── /            → static Angular files          │
                          │   └── /api/*       → Kong (:80) ──▶ auth-service   │
                          │                                    ├▶ audit-service│
                          │                                    ├▶ doc-reviewer │
                          │                                    └▶ collateral   │
                          └───────────────────────────────────────────────────┘
```

---

## 2. Server prerequisites

A Linux VM the bank provides. Minimum for the POC workload:

- 4 vCPU / 8 GB RAM / 40 GB disk (more if the LLM runs on the same box — usually it won't; see §7).
- **Docker Engine** + **Docker Compose v2** installed.
- Outbound access to wherever container images and the LLM endpoint live (or an internal registry — banks typically mirror images internally).
- DNS: a hostname pointing at the server (e.g. `gateway.bank.internal`).
- A TLS certificate for that hostname (the bank's PKI will issue one).

Verify:

```bash
docker --version          # 24+ ideal
docker compose version    # v2.x
```

---

## 3. Get the code onto the server

Whatever the bank's process allows — `git clone` from their internal Git, or an scp/rsync of the project folder:

```bash
# example
git clone <the-repo-url> /opt/api-gateway
cd /opt/api-gateway
```

Everything below runs from `/opt/api-gateway`.

---

## 4. Configuration changes (do these before first start)

These are the values hardcoded to POC defaults that **must** change for a real deployment.

### 4a. The JWT signing keypair

**Updated — this is no longer a shared secret.** Tokens are signed with RS256: `auth-service` holds the
private key, Kong verifies with the matching public key inlined in `kong.yml`. A public key is not
sensitive, so no tracked file contains a secret.

Run once per environment, before the first `docker compose up`:

```bash
python3 scripts/generate_jwt_keys.py
```

This writes `keys/jwt-private.pem` (gitignored, mounted read-only into `auth-service` only) and
`keys/jwt-public.pem`, and rewrites the `rsa_public_key` block in `kong.yml` to match. Verify with
`grep -A2 rsa_public_key kong.yml` — if it still shows `REPLACE_BY_RUNNING_...`, don't start the stack.

Rotation: `--force`, then `docker compose up -d --force-recreate`. All existing tokens become invalid.

> Do not try to move the key into an environment variable read by `kong.yml`. Neither `${{ env "..." }}`
> nor `{vault://env/...}` resolves in that field on Kong 3.9.3, and both fail silently. See
> [POC_TO_PRODUCTION.md](POC_TO_PRODUCTION.md) §2 for the test results.

### 4b. Point the frontend at the server

In [frontend/src/app/session.service.ts:12](frontend/src/app/session.service.ts#L12):

```ts
// was: export const KONG_BASE = 'http://localhost';
export const KONG_BASE = '';   // relative — nginx proxies /api/* to Kong (recommended)
```

Setting it to `''` makes the app call `/api/auth/login` etc. **relative to whatever host served it** — so it just works behind the nginx front door, on any hostname, with no per-environment rebuild needed. (If you instead serve the frontend on a *different* host than the API, set this to the full API URL, e.g. `https://gateway.bank.internal`, and keep CORS — see 4c.)

This change requires **rebuilding** the frontend (§5).

### 4c. CORS origin

[kong.yml:93-96](kong.yml#L93) allows only `http://localhost:4200`.

- If you use the **single front-door** setup (§1, recommended): frontend and API are the same origin, so CORS isn't used — you can leave it or remove the plugin.
- If frontend and API are on **different** hostnames: change the origin to the frontend's real URL, e.g. `https://gateway.bank.internal`.

### 4d. LLM endpoint (collateral-reviewer & document-reviewer)

Create a `.env` file in the project root (see [.env.example](.env.example)). **For a bank this should point at an internal / approved model, not public OpenAI** — see §7.

```dotenv
LLM_BASE_URL=https://<internal-model-endpoint>/v1
LLM_API_KEY=<key>
LLM_MODEL_EXTRACTION=<model-name>
LLM_MODEL_VISION=<model-name>
# Note: no JWT secret here — signing uses the keypair from 4a.
```

Compose already reads `LLM_*` from the environment ([docker-compose.yml:69-73](docker-compose.yml#L69)).

### 4e. Don't expose Kong's Admin API

[docker-compose.yml:8,14](docker-compose.yml#L8) publishes the Kong Admin API on `8001`. It has **no authentication**. On a server, remove the `"8001:8001"` port mapping so it's only reachable inside the Docker network — you don't need it exposed because config is declarative (`kong.yml`).

---

## 5. Build & serve the frontend

The frontend must be compiled to static files. Angular's build output (per [angular.json](frontend/angular.json)) lands in `frontend/dist/frontend/browser`.

This is already set up as a **frontend container** that builds the app and serves it with nginx, acting as the front door described in §1. The relevant files (all committed to the repo):

- [frontend/Dockerfile](frontend/Dockerfile) — two-stage build: Node compiles the Angular app, then nginx serves the static output.
- [frontend/nginx.conf](frontend/nginx.conf) — serves the SPA (with `index.html` fallback for client-side routes) and reverse-proxies `/api/*` to `http://kong:80`.
- [frontend/.dockerignore](frontend/.dockerignore) — keeps `node_modules`/`dist` out of the build context.
- The `frontend` service in [docker-compose.yml](docker-compose.yml) — the only container that publishes a host port (`80:80`).

Note the build context is `./frontend` (the frontend doesn't need the repo's `common/` code, unlike the backend services). Because the `frontend` container now owns port 80, the `kong` service no longer publishes any host port — it's `expose`d internally only and reached as `http://kong:80`.

Rebuild after any frontend config change (like `KONG_BASE` in §4b — that value is baked in at build time):
```bash
docker compose build frontend
```

---

## 6. TLS (users connect over HTTPS)

Banks won't allow plain HTTP. Two common options:

- **Terminate TLS at the nginx front door**: add a `443` server block with the bank-issued cert, redirect `80 → 443`. Mount the cert/key into the container.
- **Terminate TLS at the bank's existing load balancer / WAF** in front of the server, which forwards plain HTTP to nginx internally. Many banks require traffic to pass through their WAF anyway — in that case the server only needs to accept connections from the WAF.

Coordinate with the bank's network team on which; it's usually the second.

---

## 7. The LLM decision (read before go-live)

`collateral-reviewer` and `document-reviewer` send document text to an OpenAI-compatible API ([common/llm_provider.py](common/llm_provider.py)). That text includes customer collateral and legal documents.

**Sending that to public OpenAI/OpenRouter is normally a compliance blocker for a bank** (data residency + confidentiality). The code already supports any OpenAI-style endpoint via `LLM_BASE_URL`, so the fix is configuration, not code: point it at an **internally-hosted model** (vLLM / LiteLLM / an approved Azure OpenAI tenant) inside the bank's network. If that model runs on separate GPU hardware, size the app server accordingly (it won't need the GPU itself).

Confirm the target model endpoint with the client **before** deployment — it's the one dependency that can't be improvised on the day.

---

## 8. Start it up

```bash
cd /opt/api-gateway
docker compose build          # builds all service images + frontend
docker compose up -d          # start everything in the background
docker compose ps             # all services "Up"; kong should be "healthy"
```

Kong loads its routes from `kong.yml` on start (DB-less mode — no database to provision).

The demo login accounts are seeded automatically into the auth database on first start ([auth-service/main.py:60](auth-service/main.py#L60)) — `alice`/`alicepw` (admin) and `bob`/`bobpw` (viewer). **Change or remove these before real use** via the admin user-management endpoints; they exist only to prove the flow.

---

## 9. Smoke test (prove it works end to end)

From the server (or any machine that can reach it), replacing the host as appropriate:

```bash
# 1. login returns a token
curl -s https://gateway.bank.internal/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"alicepw"}'

# 2. call an entitled service with that token
TOKEN=<paste token from step 1>
curl -s https://gateway.bank.internal/api/documents/health-doc -H "Authorization: Bearer $TOKEN"
```

Then open `https://gateway.bank.internal/` in a browser — you should get the login page, be able to sign in, and see only the services that account is entitled to.

---

## 10. How users connect (summary for the client)

1. User opens `https://gateway.bank.internal/` in a browser.
2. nginx serves the Angular app (static files).
3. User logs in → the app POSTs to `/api/auth/login` → nginx forwards to Kong → Kong routes to `auth-service` → a signed JWT comes back.
4. Every subsequent action sends that JWT. Kong validates it and enforces per-service entitlement (the custom `service-entitlement` plugin) before forwarding to the target service.
5. Reads/writes are audited by `audit-service`.

No client-side install; users just need the URL and network access to the server.

---

## 11. Day-2 operations

- **Logs:** `docker compose logs -f <service>` (e.g. `kong`, `auth-service`). Ship these to the bank's log platform for retention.
- **Audit log** currently writes to `./audit-logs/audit.log` ([docker-compose.yml:45](docker-compose.yml#L45)). For a bank, forward it to their SIEM / WORM storage — a container volume is not durable retention.
- **Auth database** is SQLite at `./auth-data/auth.db` ([docker-compose.yml:52](docker-compose.yml#L52)). Back this up; for HA/production move it to managed Postgres and federate to the bank's identity provider.
- **Updates:** `git pull` → `docker compose build` → `docker compose up -d` (rebuilds and restarts only what changed).
- **Restart on reboot:** add `restart: unless-stopped` to each service in compose so the stack comes back after a server reboot.

---

## 12. Where this grows into "bank-grade"

This single-server compose deployment is the practical starting point. The hardening path beyond it (see the separate production-readiness notes): move to Kubernetes/OpenShift for HA, secrets from Vault/Key Vault instead of `.env`, mTLS between services, RS256 asymmetric JWTs, managed Postgres, IdP federation, and images from the bank's scanned internal registry.
