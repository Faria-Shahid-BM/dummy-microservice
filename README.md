# API Gateway POC — Kong + FastAPI microservices

A proof-of-concept that puts **[Kong Gateway](https://konghq.com/)** in front of a set of FastAPI services. It demonstrates a realistic API-gateway pattern: centralized routing, **RS256 JWT authentication** at the edge (delivered as an httpOnly cookie), **role-based access control (RBAC) with defence in depth**, an **admin dashboard** for user management, **model usage tracking**, and **live Server-Sent-Events streaming** of slow LLM pipelines — all wired with Docker Compose and driven by an Angular single-page frontend.

Six business services sit behind the gateway: **collateral review**, **document diff**, **valuation review**, **insurance review**, **policy Q&A (RAG)**, and **document generation** — plus three infrastructure services: **auth**, **audit** (a durable, per-event token-cost store), and **config** (per-user model selection).

Every LLM-backed reviewer **verifies the uploaded document is the kind it expects before spending on analysis** — collateral checks each upload matches its slot and that the two aren't the same file; valuation and insurance reject anything that doesn't read as their document type.

---

## Architecture

```
   Browser
          │  http://localhost
          ▼
  ┌──────────────────────────────────────────────────────────┐
  │        frontend (nginx) — Angular app + /api/* proxy        │
  └──────────────────────────────────────────────────────────┘
          │  http://kong:80 (internal only)
          ▼
  ┌──────────────────────────────────────────────────────────┐
  │                        Kong                                  │
  │        routing · JWT signature check · CORS                 │
  └──────────────────────────────────────────────────────────┘
     │        │          │          │          │          │
     ▼        ▼          ▼          ▼          ▼          ▼
 /api/auth /api/     /api/      /api/      /api/     /api/policyqa
           collateral docdiff    valuation  insurance
     │        │(JWT)     │(JWT)     │(JWT)     │(JWT)     │(JWT)
     ▼        ▼          ▼          ▼          ▼          ▼
  auth-   collateral  document-  valuation  insurance  policyqa-
  service  service    diff-svc   service    service    service
 (login + (LLM x-     (determin- (LLM +     (LLM       (RAG chat +
  admin    check,      istic      panel      policy     per-user
  API)     SSE)        redline)   xlsx)      compliance) ingest)
     │        └──────────┴──────────┴──────────┴──────────┘
     │              audit events via transactional outbox
     ▼                              ▼
  SQLite (auth-data vol)        audit-service → SQLite (audit-logs/ vol)
```

Two more services sit alongside: **docgen-service** (`/api/profiles,jobs,templates,approvals,notifications`) for template-driven document generation, and **config-service** (`/api/config`) for per-user model selection. Both are JWT-protected the same way.

- The **frontend** container (Angular, built to static files, served by nginx) is the only container that publishes a host port; it reverse-proxies everything under `/api/*` to Kong over the internal Docker network. Kong itself publishes no host ports.
- Kong verifies the **RS256 JWT signature** (against a public key in `kong.yml`; only auth-service holds the private key) on every protected route. A Kong `pre-function` copies the JWT out of the httpOnly `access_token` cookie into the `Authorization` header, so the browser never handles the raw token.
- **Defence in depth:** each protected service *also* re-verifies the JWT and checks the caller's **scope** (`security.py`) — a validly-signed token still can't reach a service the user isn't authorized for (403).
- **auth-service** issues tokens and hosts an **admin-only** user-management API backed by SQLite.
- The business services emit **audit events** to **audit-service** through a **transactional outbox** (`outbox.py`) — the event is written in the same DB transaction as the action it records, then a background relay delivers it, so an event is never silently lost because a service was briefly unreachable. audit-service stores them in SQLite, including each review's **token cost** in dedicated columns.
- **config-service** resolves which model each reviewer runs, per user: a user's own choice, falling back to the deployment default.

---

## Services

| Service                 | Build dir            | Route (via Kong)          | Auth | Description                                                              |
|-------------------------|----------------------|---------------------------|:----:|--------------------------------------------------------------------------|
| `auth-service`          | `auth-service/`      | `/api/auth/*`             | self | Issues RS256 JWTs (private key held only here); admin CRUD over users/scopes (SQLite). argon2id password hashing. |
| `collateral-service`    | `collateral-service/`| `/api/collateral/*`       | JWT  | LLM cross-check of a legal opinion vs a property document. Verifies each upload's document kind first. **SSE stream**.|
| `document-diff-service` | `doc_rev-service/`   | `/api/docdiff/*`          | JWT  | Deterministic word-level redline (original vs returned copy). No LLM.     |
| `valuation-service`     | `valuation-service/` | `/api/valuation/*`        | JWT  | LLM review of a valuation report vs approved-valuer panel + policy rules. Rejects unreadable or non-valuation uploads. |
| `insurance-service`     | `insurance-service/` | `/api/insurance/*`        | JWT  | LLM compliance check of an insurance policy vs bank policy + rules. Rejects non-policy uploads. |
| `policyqa-service`      | `policyqa-service/`  | `/api/policyqa/*`         | JWT  | Retrieval-augmented policy Q&A; per-user document ingestion (RAG).        |
| `docgen-service`        | `docgen-service/`    | `/api/profiles,jobs,templates,approvals,notifications` | JWT | Template-driven document generation: analyse a case, select a template, fill it, maker-checker approval. Background jobs; own Alembic-migrated SQLite. |
| `config-service`        | `config-service/`    | `/api/config/*`           | JWT  | Per-user model selection for every reviewer; resolves user override → deployment default. Admin overview of all users' models. |
| `audit-service`         | `audit-service/`     | `/api/audit/*` (GET)      | JWT (edge + service) | Durable audit log in **SQLite** (not a flat file), fed by each service's transactional outbox. Records per-event token cost; `/audit/usage` (admin) aggregates spend per user per reviewer. POST is internal-network only and derives `user_id` from the producer's verified token. |
| `kong`                  | (image `kong:3`)     | internal only (`http://kong:80`)| —    | API gateway, DB-less declarative config (`kong.yml`). No host port — reached only via the `frontend` container's nginx proxy. |
| `frontend`              | `frontend/`          | `:80` (public)            | —    | Angular app built to static files and served by nginx; reverse-proxies `/api/*` to Kong. The only container that publishes a host port. |

> **`strip_path: true`** — Kong removes the route prefix before forwarding. So `POST /api/collateral/cases` reaches the service as `POST /cases`, `POST /api/auth/login` → `POST /login`, etc.
>
> Note: the document-diff service's build directory is `doc_rev-service/`, but its compose service name, Kong upstream host, and route are all `document-diff-service` / `/api/docdiff`.

### Endpoint reference

**Auth & admin**

| Method + path (via Kong)                | Scope         | Body / notes                                              |
|-----------------------------------------|---------------|-----------------------------------------------------------|
| `POST /api/auth/login`                  | none          | `{username,password}` → sets httpOnly `access_token` cookie; body `{username, scopes}`. |
| `POST /api/auth/logout`                 | any           | Clears the cookie.                                        |
| `GET /api/auth/users`                   | `admin`       | List users + scopes.                                      |
| `POST /api/auth/users`                  | `admin`       | Create `{username,password,scopes}`.                      |
| `PUT /api/auth/users/{username}/scopes` | `admin`       | Replace a user's scopes.                                  |
| `DELETE /api/auth/users/{username}`     | `admin`       | Remove a user.                                            |

**Case-based reviewers** (collateral, valuation, insurance, docdiff) share one router — `{service}` is the scope and route prefix. A review is: create a case, upload each slot, analyze, read the result. A rejected document-kind / completeness check returns **422**.

| Method + path (via Kong)                                | Scope        | Notes                                              |
|---------------------------------------------------------|--------------|----------------------------------------------------|
| `GET/POST /api/{service}/cases`                         | `{service}`  | List / create the caller's own cases.              |
| `GET/DELETE /api/{service}/cases/{id}`                  | `{service}`  | Fetch / delete one owned case.                     |
| `POST /api/{service}/cases/{id}/uploads/{slot}`         | `{service}`  | multipart `file` into a named slot (e.g. `legal`, `property`, `report`, `policy`). |
| `POST /api/{service}/cases/{id}/analyze`                | `{service}`  | Runs the review; **SSE** stage/token progress, result per pair. |
| `GET /api/{service}/cases/{id}/result`                  | `{service}`  | The stored result once analyzed.                   |
| `POST /api/{service}/cases/{id}/pairs` (+ pair uploads) | `{service}`  | Extra document pairs reviewed on the same case.    |

**Policy Q&A**

| Method + path (via Kong)     | Scope       | Notes                                                     |
|------------------------------|-------------|-----------------------------------------------------------|
| `GET /api/policyqa/status`   | `policy_qa` | Whether the user has a personal index; bundled available. |
| `POST /api/policyqa/chat`    | `policy_qa` | `{query, history}` → `{answer, sources}` (RAG).           |
| `POST /api/policyqa/ingest`  | `policy_qa` | multipart `file` → builds the caller's own index.         |
| `DELETE /api/policyqa/index` | `policy_qa` | Deletes the caller's index (chat falls back to bundled).  |

**Config, usage, audit, docgen**

| Method + path (via Kong)             | Scope         | Notes                                                     |
|--------------------------------------|---------------|-----------------------------------------------------------|
| `GET /api/config/effective/{scope}`  | `{scope}`     | The models this caller's next review of that type will run on. |
| `PUT/DELETE /api/config/{scope}/{key}` | `{scope}`   | Set / reset the caller's own model choice.                |
| `GET /api/config/admin/overview`     | `admin`       | Every user's model overrides + the deployment defaults.   |
| `GET /api/audit/audit`               | any valid JWT | All audit events. *(Known issue: not admin-scoped — see the security review.)* |
| `GET /api/audit/audit/usage`         | `admin`       | Token spend per user per reviewer, and models each ran on. |
| `/api/profiles,jobs,templates,approvals,notifications` | `docgen` / `docgen_check` | Document-generation pipeline (profiles, jobs, templates, maker-checker approvals). |

The auth admin endpoints have **no Kong JWT plugin** (login must be reachable), so auth-service enforces the `admin` scope itself.

---

## Role-based access control (RBAC)

Access is governed by **scopes** carried in the JWT (`scopes` claim). Scopes: `collateral`, `docdiff`, `valuation`, `insurance`, `policy_qa`, `docgen`, `docgen_check` (approves docgen work — maker-checker), and `admin`.

### Seed users

Seeded into the SQLite store on first boot (`SEED_USERS` in [`auth-service/main.py`](auth-service/main.py)):

| Username  | Password      | Scopes                                                              |
|-----------|---------------|--------------------------------------------------------------------|
| `admin`   | `password123` | all: `admin`, `collateral`, `docdiff`, `valuation`, `insurance`, `policy_qa`, `docgen` |
| `checker` | `checkerpass` | `docgen`, `docgen_check` (approves generated documents)            |
| `carol`   | `carolpass`   | `collateral`, `valuation`                                          |
| `dave`    | `davepass`    | `docdiff`, `insurance`                                             |

These are only the **seed**. An `admin` can add/remove users and toggle any user's scopes from the **Admin dashboard** in the UI. Edits persist across restarts on the `auth-data` volume.

> **Note on seeding & scope changes:** the DB is seeded only when empty. If you add a scope in code but the `auth-data` volume already has users, existing users keep their old scopes — grant the new scope via the dashboard, or wipe the volume (`docker compose down -v`) to reseed. A JWT snapshots scopes at login, so **log out and back in** after a scope change.

### Three enforcement layers (defence in depth)

1. **UI (UX only)** — the [Angular frontend](frontend/src/app/dashboard/dashboard.component.ts) shows only the service tiles / admin dashboard the user's scopes allow.
2. **Gateway** — Kong verifies the JWT **signature** on all business routes.
3. **Service (the real boundary)** — [`security.py`](security.py) re-verifies the JWT and returns **403** unless the required scope is present; the admin API requires the `admin` scope. A route called directly (e.g. `curl`) is still enforced.

---

## How the JWT flow works

1. The client posts credentials to `auth-service`, which verifies them (argon2id) against the SQLite store and returns an **RS256** JWT — signed with a **private key only auth-service holds**, issuer `poc-issuer`, containing `sub` (username) and `scopes`. The token is set as an **httpOnly `access_token` cookie** (`SameSite=Lax`), so browser JS never sees it.
2. The browser sends the cookie automatically on subsequent requests.
3. A Kong **`pre-function`** copies the cookie's value into an `Authorization: Bearer …` header, then **Kong's JWT plugin** verifies the signature against the consumer's **RSA public key** (matched by the `iss` claim) — see [`kong.yml`](kong.yml).
4. The upstream service (`security.py`) independently re-decodes the token, checks `exp`/`iss`, and enforces the required **scope**.

> **Key rotation:** the public key lives inline in `kong.yml` while the private key lives in `keys/` (gitignored). The two must be a matching pair, or every request fails signature verification — `docker compose restart kong` after changing `kong.yml`, since it's mounted, not baked in.

---

## Live streaming (collateral review)

The LLM pipelines run several calls and can take minutes, so `POST /api/{service}/cases/{id}/analyze` streams progress instead of blocking.

- **Backend:** [`streaming.py`](streaming.py) runs the blocking engine on a worker thread and bridges its `emit()` progress callback into **Server-Sent Events** (`text/event-stream`, with `X-Accel-Buffering: no` so Kong/nginx doesn't buffer).
- **Token streaming:** [`provider.py`](provider.py) has a `stream()` method; the human-readable **observations** step streams tokens live. Scanned-PDF OCR reports **page-level** progress instead (pages transcribe in parallel).
- **Frontend:** the Angular case-detail pages consume the SSE stream and render a live stage checklist with per-step token counts and timings.

**SSE event contract** (each `data:` payload is valid JSON): `open` (connect ack) · `event` (stage/page progress, and `usage` events carrying per-step token cost) · `content` (live LLM tokens) · `pair_result` / `result` (final object) · `error`.

---

## Model usage & token tracking

Every LLM reply already reports its token count; the reviewers now surface it instead of discarding it.

- Each pipeline step emits a `usage` event (prompt / completion / total tokens, plus wall-clock time), shown live on the case page. The deterministic comparison steps correctly report **zero** — visible proof they cost nothing.
- Each review records what it cost, tagged with the model it actually ran on, into the audit trail via the outbox — in **dedicated columns**, so totals aggregate in SQL rather than by parsing JSON.
- The **Admin dashboard → Model usage** page reads `/api/audit/audit/usage` and `/api/config/admin/overview` to show token spend per user per reviewer, and each user's configured models.

## Per-user model configuration

`config-service` owns which model each reviewer role uses. A user can override any role they're entitled to (Configuration in the header); unset roles fall back to the deployment default from the service environment. The review services resolve their models through `config_client.py` at the start of each run, degrading to the environment default if config-service is unreachable.

---

## Policy Q&A (RAG)

`policyqa-service` is a stdlib-only retrieval-augmented chat engine ([`engines/policy_qa.py`](engines/policy_qa.py)):

- Embeddings come from the LLM provider (`provider.embed`, `MODEL_EMBEDDING = openai/text-embedding-3-large`); vectors are stored unit-normalized as a flat `float32` file, and search is plain-Python cosine similarity.
- A **bundled default index** ships in the image at `engines/data/policy_qa_bundled/` (308 chunks, dim 3072).
- Users can **ingest their own policy** (`POST /ingest`): the upload is text/OCR-extracted, chunked, embedded, and saved as that user's **personal index** under the `policyqa-data` volume. Chat uses the personal index when present, otherwise the bundled one. `GET /status` reports which, and `DELETE /index` removes the personal one.

---

## Data storage

- **Users → SQLite.** `auth-service` owns a SQLite DB on the `auth-data` volume; passwords are **argon2id** hashed. Seeded from `SEED_USERS` only when empty.
- **Reviewer cases → per-service SQLite.** Each case-based reviewer keeps its own DB and uploads on its own volume (`collateral-data`, `valuation-data`, `insurance-data`, `docdiff-data`), scoped to the creating user. Shared router: [`case_store.py`](case_store.py).
- **Policy Q&A indexes → volume.** Per-user RAG indexes live under the `policyqa-data` volume. The bundled default index is baked into the image.
- **Audit → SQLite.** `audit-service` stores events (with per-event token cost) in a SQLite DB on a host-mounted volume ([`audit-logs/`](audit-logs/)). Producers deliver via a **transactional outbox** ([`outbox.py`](outbox.py)) with a background relay, so an event is durable against audit-service being briefly down.
- **Config → SQLite.** `config-service` stores per-user model overrides on the `config-data` volume; the default for each role comes from the service's environment.
- **Docgen → SQLite (Alembic).** `docgen-service` uses Alembic migrations on the `docgen-data` volume — the one service with real migrations rather than `create_all` + hand-written `ALTER TABLE`.
- **Kong → DB-less.** Kong runs in declarative mode from [`kong.yml`](kong.yml); no Kong database.

---

## Shared code & engines

The document/LLM logic lives in the [`engines/`](engines/) package — copied into each business-service image along with shared modules at the repo root ([`provider.py`](provider.py), [`security.py`](security.py), [`case_store.py`](case_store.py), [`outbox.py`](outbox.py), [`streaming.py`](streaming.py), [`audit_client.py`](audit_client.py), [`config_client.py`](config_client.py)). These sit at the root **on purpose** — each service's Dockerfile copies them in, so a shared fix reaches every service without a separate package.

- **Engines:** `extraction` (text + vision OCR), `collateral`, `document_diff`, `valuation`, `insurance`, `policy_qa`, `util`, `evidence`, `field_match`.
- **Verification:** `document_kind` (collateral slot/kind check + duplicate guard), `completeness` + `insurance_completeness` + `valuation_completeness` (is-this-the-right-document gates).
- **Token accounting:** `token_usage` (shared usage helpers threaded through every LLM call).
- **Prompts** (frozen domain IP): [`engines/prompts/`](engines/prompts/) — `collateral_extraction`, `collateral_observations`, `collateral_adjudication`, `document_kind`, `extraction_transcription`, `valuation_extraction`, `insurance_extraction`, `insurance_analysis`, `policy_qa_system`.
- **Data:** [`engines/data/`](engines/data/) — `policy.txt`, `collateral_policy_rules.txt`, `default_panel.xlsx` (valuation panel), `policy_qa_bundled/` (RAG index).

---

## Tech stack

- **Kong 3** (DB-less / declarative config)
- **FastAPI** + **Uvicorn** (Python 3.11)
- **PyJWT** (**RS256**, asymmetric — private key only in auth-service), **SQLite** + **argon2id** (argon2-cffi) for the user store; **Alembic** migrations in docgen
- **pdfplumber**, **PyMuPDF (fitz)**, **python-docx** for text/OCR extraction; **openpyxl** (valuation panel) + **python-dateutil**; **httpx** for LLM calls (OpenRouter-compatible, streaming + embeddings)
- **Docker Compose** for orchestration; **Angular** frontend, built to static files and served by **nginx** (also proxies `/api/*` to Kong)

---

## Getting started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- A modern browser (for the frontend)
- An **OpenRouter API key** for the LLM-backed services (collateral, valuation, insurance, policy Q&A)

### 1. Configure the LLM key

Create a `.env` file in the project root:

```env
LLM_API_KEY=sk-or-...your-openrouter-key...
```

Models are set per service in [`docker-compose.yml`](docker-compose.yml): the review services use `MODEL_EXTRACTION` / `MODEL_VISION` (default `google/gemini-2.5-Flash`); policy Q&A uses `MODEL_CHAT` + `MODEL_EMBEDDING` (`openai/text-embedding-3-large`). The **document-diff** service is deterministic and needs no key.

### 2. Build and start

```bash
docker compose up --build
```

Kong becomes healthy first, then the services start, then `frontend` builds the Angular app and starts nginx.

### 3. Open the frontend

Open `http://localhost` in your browser — the `frontend` container serves the built Angular app there and reverse-proxies `/api/*` to Kong internally. Sign in (e.g. `admin` / `password123`); you'll see the service tiles and admin dashboard your scopes allow.

> Frontend source changes need a rebuild of just that container (`docker compose up --build frontend`). Backend/Dockerfile/`requirements` changes need a rebuild of the affected service; `kong.yml` changes need `docker compose restart kong` (it's mounted, not baked in).

### 4. Or use the API directly

The token now arrives as an httpOnly cookie, so use a cookie jar (`-c` to save, `-b` to send) rather than a bearer header:

```bash
# 1. Log in — the JWT is set as an access_token cookie, saved to cookies.txt
curl -s -c cookies.txt -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"password123"}'

# 2. Policy Q&A (uses the bundled index unless you've ingested your own)
curl -b cookies.txt -X POST http://localhost/api/policyqa/chat \
  -H "Content-Type: application/json" \
  -d '{"query":"What is the minimum insurance coverage required?","history":[]}'

# 3. A collateral review is case-based: create → upload slots → analyze
CASE=$(curl -s -b cookies.txt -X POST http://localhost/api/collateral/cases \
  -H "Content-Type: application/json" -d '{"name":"demo"}' | jq -r .id)
curl -b cookies.txt -X POST http://localhost/api/collateral/cases/$CASE/uploads/legal    -F file=@legal.pdf
curl -b cookies.txt -X POST http://localhost/api/collateral/cases/$CASE/uploads/property -F file=@property.pdf
curl -b cookies.txt -X POST http://localhost/api/collateral/cases/$CASE/analyze          # SSE stream

# 4. Scope enforcement: dave lacks the "collateral" scope → 403
curl -s -c dave.txt -X POST http://localhost/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"dave","password":"davepass"}'
curl -i -b dave.txt http://localhost/api/collateral/cases     # -> 403 Insufficient scope
```

---

## Configuration reference

Key values (hard-coded for the POC — change before any real use):

| Setting                       | Value                              | Where                                     |
|-------------------------------|------------------------------------|-------------------------------------------|
| JWT signing                   | **RS256** — private key `keys/jwt-private.pem` (gitignored); public key inline in `kong.yml` | `keys/`, `kong.yml`, `docker-compose.yml` env |
| JWT issuer (`iss`)            | `poc-issuer`                       | `kong.yml`, `docker-compose.yml` env      |
| Kong consumer                 | `poc-user`                         | `kong.yml`                                |
| Token delivery                | httpOnly `access_token` cookie, `SameSite=Lax`, 2 h expiry | `auth-service/main.py`   |
| Scopes                        | collateral, docdiff, valuation, insurance, policy_qa, docgen, docgen_check, admin | `auth-service/main.py`, `security.py` |
| Models (per role, per reviewer) | deployment defaults in env; per-user overrides in config-service | `docker-compose.yml`, `config-service` |
| LLM upstream timeouts         | 310 s read/write (LLM services)    | `kong.yml`                                |
| CORS origins / methods        | `*` / GET, POST, PUT, DELETE, OPTIONS | `kong.yml`                             |
| LLM base URL / key            | OpenRouter / `LLM_API_KEY`         | `docker-compose.yml`, `.env`              |
| Public port                   | `80` (`frontend` container only)   | `docker-compose.yml`                      |
| Kong proxy / admin ports      | `80` / `8001` (internal only)      | `docker-compose.yml`                      |
| Volumes                       | `auth-data`, `config-data`, `docgen-data`, `policyqa-data`, `{collateral,docdiff,valuation,insurance}-data` (+ `./audit-logs` bind) | `docker-compose.yml` |

> The LLM services' upstream timeouts are raised to ~310 s (past `provider.py`'s 300 s HTTP timeout) because the pipelines can run for minutes; Kong's 60 s default would otherwise return a 504.

> **Security status:** this is a POC. A security review of the current branch found open issues (notably the `/api/audit/audit` read route is not admin-scoped, and a private key exists in earlier git history). See [`POC_TO_PRODUCTION.md`](POC_TO_PRODUCTION.md) and the security review before any real deployment.
