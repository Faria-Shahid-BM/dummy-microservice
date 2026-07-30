# API Gateway POC

A proof-of-concept showing Kong as a single API gateway in front of a set of microservices, with real login-issued JWTs, claims-based service entitlement, in-app role checks, centralized audit logging, and two real feature services (a deterministic document-diff reviewer and an LLM-based collateral reviewer) — plus a routed Angular frontend to drive the whole thing.

## What this demonstrates

- API gateway pattern — one entry point, no service reachable directly
- JWT authentication vs. claims-based authorization as two distinct, separable concerns
- Defense-in-depth reasoning (why a service might re-verify a signature even though Kong already did)
- Role-based permission checks living inside a service, separate from "can you reach this service at all"
- A custom Kong plugin, written once, reused across routes with different config instead of copy-pasted logic
- A shared internal library so every service imports the same auth/audit/LLM code instead of duplicating it
- A real admin UI for granting/revoking access, backed by SQLite instead of a hardcoded dict
- Two genuinely different "reviewer" services on the same pattern: one fully deterministic (no LLM), one LLM-based
- Docker multi-container orchestration with Docker Compose
- A routed Angular frontend (login / dashboard / admin) driving a real login flow, not hardcoded tokens

## System overview

```
                    BROWSER (Angular, routed)
             /login      /dashboard        /admin/*
                │             │               │
                │     (sidebar = your         │  (admin-only,
                │      token's "services"     │   own shell)
                │      claim)                 │
                └─────────────┬───────────────┘
                               |  Authorization: Bearer <token>
                               ▼
                    ┌────────────────────┐
                    │        KONG        │
                    │  jwt +             │
                    │  service-entitlement│
                    │  (route-scoped)    │
                    └────────────────────┘
                               │
        ┌────────────────┬─────────────────┬────────────────────┐
        ▼                ▼                 ▼                    ▼
  auth-service     audit-service    document-reviewer   collateral-reviewer
  (/login is             ▲           (no LLM,            (LLM: field
   public; user          │            real HTML diff      extraction +
   mgmt is               │            for .docx)          comparison +
   admin-only)           │                                observations)
                         │                  │                   │
                         └──────────────────┴───────────────────┴──── POST /audit
                                       direct container call, bypasses Kong entirely
```

None of the backend containers expose a port to your machine — only the `frontend` container does (`docker-compose.yml`), and it reverse-proxies `/api/*` to Kong. That's what actually makes "everything goes through Kong" true rather than a convention. Adding a service needs no new Kong mechanism: give it its own route with `jwt` + `service-entitlement` and a different `required_service`.

## Containers

- **kong** — the gateway. Every external request goes through it first. Verifies JWTs are genuine (`jwt` plugin) and checks per-route service entitlement (a custom plugin, `service-entitlement`). Does *not* decide role-based permissions or anything feature-specific; that's left to each service.
- **auth-service** — issues tokens (`POST /login`, checks a SQLite-backed, password-hashed users table) and, for admins only, manages that table: `POST /`, `GET /` (create/list users), `PUT /{username}/services` (grant/revoke entitlements). Every admin action gets its own audit entry.
- **audit-service** — internal-only. Receives events from every other service directly over the Docker network (never through Kong) and writes them to `audit-logs/audit.log`. Exposes exactly one thing through Kong: a paginated `GET` route to read the log back, gated to admin-role tokens only.
- **document-reviewer** — compares an original vs. returned document. `.docx` gets converted to real HTML (via `mammoth`) and diffed in place, so the result actually looks like the document with changes highlighted; `.pdf` falls back to a flat-text diff. Fully deterministic — no LLM either way (an earlier version tried LLM-assisted PDF heading detection; dropped in favor of a simpler, always-correct click-to-jump-to-change mechanism instead of guessing at document structure).
- **collateral-reviewer** — cross-checks a legal opinion against a property document using a real LLM: extracts a fixed field set from each, compares field-by-field, and generates plain-English observations for any discrepancy. Falls back to vision-OCR (rasterize + a vision-model transcription call) for scanned PDFs with no text layer.
- **frontend** (`frontend/`, Angular, not in `docker-compose.yml`) — routed pages: `/login`, `/dashboard` (sidebar built from your token's `services` claim, one panel per entitled service), `/admin/*` (audit trail, user list with permission toggles, add-user — three separate pages sharing one admin shell, visible only to admins).

## Why one shared secret instead of one-secret-per-user

Kong's JWT plugin can be set up with a separate secret per user (its `consumers` model), which was the first version of this POC. It got replaced with a single shared secret because it maps better to how real systems actually work: **one issuer signs every token**, and *what that token grants* — which services, what role — is data carried inside the token's claims, decided at login time against a real users table. Kong's job is to answer *is this signature genuine, and is this token entitled to be here at all* — the second half lives in a custom plugin, not hand-copied into every service.

## Why a custom Kong plugin instead of per-service checks

The entitlement check ("is `document-reviewer` in this token's `services` claim?") originally lived inside each service — fine for two services, but it doesn't scale: at 25 services, that's 25 copies of the same JWT-decoding logic. A real custom Kong plugin (`kong-plugins/kong/plugins/service-entitlement/`) solves this properly: `handler.lua` contains the check exactly once, and `schema.lua` declares one config field (`required_service`) that each route sets independently:

```yaml
- name: service-entitlement
  config:
    required_service: document-reviewer
```

No image rebuild needed — the plugin folder is volume-mounted into Kong, the same way `kong.yml` itself is. It runs at `PRIORITY = 899`, deliberately lower than the `jwt` plugin's (1005) — Kong runs higher-priority plugins first, so the signature is already verified by the time this plugin trusts the claims inside it.

## Why a shared library instead of per-service copies

`common/` holds three files imported by whichever services need them, instead of every service carrying its own copy:

- **`claims.py`** — `get_claims()` (decode a token's payload; Kong already verified the signature, so this doesn't re-check it) and `require_role(role)`, a parameterized FastAPI dependency factory so a route-level permission check reads as one line: `Depends(require_role("admin"))`.
- **`audit.py`** — the fire-and-forget POST to `audit-service`. This one couldn't move into Kong — Kong has no way to know a request means `"hello.called"` versus `"compare.called"`; that's business-specific context only the service has at the moment it happens.
- **`llm_provider.py`** — a minimal OpenAI-compatible chat-completion client, shared by `document-reviewer`'s (now-removed) LLM path and `collateral-reviewer`. Configured entirely via `LLM_BASE_URL`/`LLM_API_KEY` env vars (see `.env.example`) so the real key never lives in a committed file.

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
   │  against SQLite (hashed)  │
   │                            │
   │  sign a JWT:               │
   │    iss:      "poc-app"     │
   │    username: "alice"       │
   │    role:     "admin"       │
   │    services: ["document-…"] │
   └──────────────────────────┘
        │
        ▼
   { token, username, role, services } → stored in the browser (in memory,
   SessionService) → router redirects to /dashboard
```

## Flow 2 — Calling a service (using the token)

This is the same Kong-side gate for every protected route — `document-reviewer` and `collateral-reviewer` both sit behind `jwt` + `service-entitlement`, just with a different `required_service` and different logic once the request actually arrives.

```
   BROWSER
        │  Authorization: Bearer <token>
        ▼
   ┌────────────────────────────────┐
   │              KONG                │
   │                                    │
   │  1) jwt plugin                     │
   │     - verify signature against     │
   │       the ONE registered secret    │
   │       missing/invalid → 401        │
   │                                    │
   │  2) service-entitlement plugin     │
   │     (custom, written once, PRIORITY│
   │     899 — runs AFTER jwt)          │
   │     - read the "services" claim    │
   │       straight out of the token    │
   │     - required_service in it?      │
   │         no  → 403 not entitled     │
   │         yes → continue             │
   └────────────────────────────────┘
                    │
                    ▼
       forwarded to the matched service
                    │
                    ▼
       document-reviewer / collateral-reviewer
       (no role gate — any entitled caller can use it; the
        "expensive" gate is upstream at Kong, not a role check.
        Role gating still exists where it matters: auth-service's
        user management and audit-service's log read both call
        Depends(require_role("admin")) themselves.)
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
                          │  call, no Kong, no token   │
                          └──────────────────────────┘
                                       │
                                       ▼
                              audit-logs/audit.log
```

The two `403`s look identical from outside but come from different layers: Kong's `service-entitlement` plugin means *"you're not entitled to this service at all"* (a plan/subscription problem, rejected before ever reaching a service); a `403` from inside a service means *"you're entitled to be here, you just don't have the right role for this specific action"* (a permissions problem). A `401` is different again — Kong rejecting before anyone even knows who's asking.

## Flow 3 — Document Reviewer's two diff paths

```
   POST /api/documents/compare  (two files: original, returned)
                    │
                    ▼
          both files .docx?
           │                │
          yes               no (either is .pdf, or mixed)
           │                │
           ▼                ▼
   ┌───────────────┐  ┌──────────────────┐
   │ mammoth: docx  │  │ pdfplumber/docx:  │
   │ → real HTML    │  │ flatten to plain  │
   │ (tables, h1's,  │  │ text              │
   │  bold, etc.)    │  └──────────────────┘
   └───────────────┘            │
           │                    ▼
           ▼            word-level diff (difflib),
   tag-preserving        each change gets a
   token diff: tags       sequential id
   flow through          │
   untouched, only        ▼
   changed WORDS get    render: "text" — plain
   wrapped in            redline with colored
   <del>/<ins>           delete/insert spans
           │
           ▼
   render: "html" — the ACTUAL document,
   changes highlighted in place, re-parsed
   through BeautifulSoup to auto-repair any
   tag imbalance the diff introduced

   Either way: "changes" table rows are clickable — jumps to and flashes the
   matching doc-change-N anchor in the rendered view above. (The anchor is a
   CSS class, not an id — Angular's [innerHTML] sanitizer strips id attributes
   but passes class through untouched.)
```

An earlier version tried attributing each change to a "section"/"field" derived from document headings and table columns. It fell apart on real documents (dense label/value form letters, not clean heading hierarchies) — dropped in favor of the click-to-jump mechanism above, which needs no understanding of document structure to be correct.

## Flow 4 — Reading the audit trail (admin only, paginated)

```
   BROWSER  →  GET /api/audit?limit=20&offset=0  (Authorization: Bearer <token>)
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
  403    reverse-chronological slice + total count
         → { items: [...], total: N }
```

Same role-check pattern as `auth-service`'s user management, just living in `audit-service`. The admin frontend page calls this automatically on load (no manual refresh needed) and re-calls it on Previous/Next.

## Running it

Backend:
```powershell
copy .env.example .env
# edit .env with a real LLM_BASE_URL / LLM_API_KEY — only needed for collateral-reviewer
docker compose up --build
```
Kong listens on `localhost:80` (proxy) and `localhost:8001` (admin API). Every other container has no exposed ports; they're reachable only through Kong or from each other over the internal Docker network. `document-reviewer` needs no LLM credentials at all — only `collateral-reviewer` does.

Frontend:
```powershell
cd frontend
npm start
```
Opens on `localhost:4200`. Kong has a `cors` plugin configured specifically for that origin.

## Demo accounts

| Username | Password | Role | Entitled to |
|---|---|---|---|
| `alice` | `alicepw` | `admin` | `document-reviewer`, `collateral-reviewer` |
| `bob` | `bobpw` | `viewer` | `collateral-reviewer` |

These are just the seeded starting point — log in as `alice` (admin) and use `/admin/users` to create more users or grant/revoke `document-reviewer`/`collateral-reviewer` access to existing ones. A user's *existing* token won't reflect a new grant until they log in again — entitlements are a snapshot taken at login, not a live lookup.

> These credentials are seeded for demo purposes only — not something to carry into a real deployment as-is.

**First-time setup:** tokens are signed with RS256, so you need a keypair before the first start. Run `python scripts/generate_jwt_keys.py` once — it writes the gitignored `keys/` folder and fills in the matching public key in `kong.yml`. Without it, `auth-service` refuses to start.

## Endpoints

| Method & path (through Kong) | Behind it | Guarded by |
|---|---|---|
| `POST /api/auth/login` | `auth-service` | nothing — this *is* the login |
| `POST /api/auth/users`, `GET /api/auth/users` | `auth-service` | Kong: valid token; `auth-service` itself checks `role: admin` |
| `PUT /api/auth/users/{username}/services` | `auth-service` | same as above |
| `GET /api/documents/health-doc` | `document-reviewer` | Kong: valid token + `service-entitlement` plugin |
| `GET /api/collateral/health-collateral` | `collateral-reviewer` | Kong: valid token + `service-entitlement` plugin |
| `POST /api/documents/compare` | `document-reviewer` | Kong: valid token + `service-entitlement` plugin (no role gate) |
| `POST /api/collateral/compare` | `collateral-reviewer` | Kong: valid token + `service-entitlement` plugin (no role gate) |
| `GET /api/audit` | `audit-service` | Kong: valid token; `audit-service` itself checks `role: admin` |

## Production considerations

This runs locally via Docker Desktop, which is purely a dev convenience. Production servers already run Docker's engine directly; the architecture here — one gateway, no service exposed except through it, JWT verification, claims-based entitlement — carries over conceptually. `docker-compose.yml` itself is even a legitimate way to run something this size in production, not something you're forced to replace with Kubernetes.

What would actually need to change before this is a real deployment, not a POC:

- **Images get built once and pushed to a registry** by a CI pipeline, not built on the live server.
- **Secrets move out of files into a secrets manager.** The JWT signing key is no longer hardcoded — it's an RS256 private key in the gitignored `keys/` folder, with only the public key in `kong.yml`. For production that private key, and the `LLM_API_KEY` currently in a local `.env`, should both come from a real secrets manager (AWS Secrets Manager, Vault, Kubernetes Secrets) rather than files on disk.
- **TLS becomes real.** This POC runs on plain `http://localhost`; production terminates actual HTTPS.
- **The audit log needs a real store.** A bind-mounted file on one machine doesn't survive a restart or work across replicas — that becomes a database table or a logging service instead.
- **The frontend stops being a dev server.** Production runs `ng build` once and serves the static output from a CDN or a plain web server container.
- **Each service re-verifying the JWT signature itself** (rather than trusting Kong's word for it) is a defense-in-depth improvement worth making before this handles anything real — safe right now only because nothing can reach these services except through Kong.
- **`collateral-reviewer`'s LLM calls have real cost and latency** that a demo doesn't need to think about — rate limiting, cost caps, and timeout/retry tuning all become real operational concerns at any scale.

For this scale, a managed container platform (AWS ECS Fargate, Google Cloud Run, or a single VM running this same Compose file with real secrets/TLS swapped in) gets to production-shaped without the operational overhead of a full Kubernetes cluster.
