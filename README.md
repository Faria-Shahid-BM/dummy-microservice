# API Gateway POC — Kong + FastAPI microservices

A proof-of-concept that puts **[Kong Gateway](https://konghq.com/)** in front of a set of FastAPI services. It demonstrates a realistic API-gateway pattern: centralized routing, **JWT authentication** at the edge, **role-based access control (RBAC) with defence in depth**, an **admin dashboard** for user/permission management, **CORS**, a best-effort **audit trail**, and **live Server-Sent-Events streaming** of a slow LLM pipeline — all wired with Docker Compose and driven by a single-page frontend.

Five business services sit behind the gateway: **collateral review**, **document diff**, **valuation review**, **insurance review**, and **policy Q&A (RAG)** — plus **auth** and **audit**.

---

## Architecture

```
   Browser (frontend/index.html + activity.js)
          │  http://localhost
          ▼
  ┌──────────────────────────────────────────────────────────┐
  │                        Kong  (:80)                          │
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
     │                    best-effort audit events
     ▼                              ▼
  SQLite (auth-data vol)        audit-service → audit-logs/audit.log
```

- All traffic enters through **Kong** on port `80`.
- Kong verifies the **JWT signature** on every protected route (`/api/collateral`, `/api/docdiff`, `/api/valuation`, `/api/insurance`, `/api/policyqa`).
- **Defence in depth:** each protected service *also* re-verifies the JWT and checks the caller's **scope** (`security.py`) — a validly-signed token still can't reach a service the user isn't authorized for (403).
- **auth-service** issues tokens and hosts an **admin-only** user-management API backed by SQLite.
- The business services emit best-effort **audit events** to **audit-service**, which appends them to a host-mounted log file.

---

## Services

| Service                 | Build dir            | Route (via Kong)          | Auth | Description                                                              |
|-------------------------|----------------------|---------------------------|:----:|--------------------------------------------------------------------------|
| `auth-service`          | `auth-service/`      | `/api/auth/*`             | self | Issues HS256 JWTs; admin CRUD over users/scopes (SQLite).                |
| `collateral-service`    | `collateral-service/`| `/api/collateral/*`       | JWT  | LLM cross-check of a legal opinion vs a property document. **SSE stream**.|
| `document-diff-service` | `doc_rev-service/`   | `/api/docdiff/*`          | JWT  | Deterministic word-level redline (original vs returned copy). No LLM.     |
| `valuation-service`     | `valuation-service/` | `/api/valuation/*`        | JWT  | LLM review of a valuation report vs approved-valuer panel + policy rules. |
| `insurance-service`     | `insurance-service/` | `/api/insurance/*`        | JWT  | LLM compliance check of an insurance policy vs bank policy + rules.       |
| `policyqa-service`      | `policyqa-service/`  | `/api/policyqa/*`         | JWT  | Retrieval-augmented policy Q&A; per-user document ingestion (RAG).        |
| `audit-service`         | `audit-service/`     | `/api/audit/*`            | none | Append-only JSON-lines audit log.                                        |
| `kong`                  | (image `kong:3`)     | `:80` proxy, `:8001` admin| —    | API gateway, DB-less declarative config (`kong.yml`).                     |

> **`strip_path: true`** — Kong removes the route prefix before forwarding. So `POST /api/collateral/review` reaches the service as `POST /review`, `POST /api/auth/login` → `POST /login`, etc.
>
> Note: the document-diff service's build directory is `doc_rev-service/`, but its compose service name, Kong upstream host, and route are all `document-diff-service` / `/api/docdiff`.

### Endpoint reference

| Method + path (via Kong)                | Scope         | Body / notes                                              |
|-----------------------------------------|---------------|-----------------------------------------------------------|
| `POST /api/auth/login`                  | none          | `{username,password}` → `{token, username, scopes}`.      |
| `GET /api/auth/users`                   | `admin`       | List users + scopes.                                      |
| `POST /api/auth/users`                  | `admin`       | Create `{username,password,scopes}`.                      |
| `PUT /api/auth/users/{username}/scopes` | `admin`       | Replace a user's scopes.                                  |
| `DELETE /api/auth/users/{username}`     | `admin`       | Remove a user.                                            |
| `POST /api/collateral/review`           | `collateral`  | multipart `legal`, `property` → full JSON result.         |
| `POST /api/collateral/review/stream`    | `collateral`  | Same inputs; **SSE** progress + result.                   |
| `POST /api/docdiff/compare`             | `docdiff`     | multipart `original`, `returned` → diff JSON.             |
| `POST /api/valuation/review`            | `valuation`   | multipart `report` → valuation JSON.                      |
| `POST /api/insurance/review`            | `insurance`   | multipart `policy` → `{insurance_report: …}`.             |
| `GET /api/policyqa/status`              | `policy_qa`   | Whether the user has a personal index; bundled available. |
| `POST /api/policyqa/chat`               | `policy_qa`   | `{query, history}` → `{answer, sources}` (RAG).           |
| `POST /api/policyqa/ingest`             | `policy_qa`   | multipart `file` → builds the caller's own index.         |
| `DELETE /api/policyqa/index`            | `policy_qa`   | Deletes the caller's index (chat falls back to bundled).  |
| `GET /api/audit/audit`                  | none          | Returns all audit events.                                 |

The admin endpoints have **no Kong JWT plugin** (login must be reachable), so auth-service enforces the `admin` scope itself.

---

## Role-based access control (RBAC)

Access is governed by **scopes** carried in the JWT (`scopes` claim). Scopes: `collateral`, `docdiff`, `valuation`, `insurance`, `policy_qa`, `admin`, and `doc_gen` (**reserved — no service wired yet**).

### Seed users

Seeded into the SQLite store on first boot (`SEED_USERS` in [`auth-service/main.py`](auth-service/main.py)):

| Username | Password      | Scopes                                                              |
|----------|---------------|--------------------------------------------------------------------|
| `admin`  | `password123` | all: `admin`, `collateral`, `docdiff`, `valuation`, `insurance`, `policy_qa`, `doc_gen` |
| `carol`  | `carolpass`   | `collateral`, `valuation`                                          |
| `dave`   | `davepass`    | `docdiff`, `insurance`                                             |

These are only the **seed**. An `admin` can add/remove users and toggle any user's scopes from the **Admin dashboard** in the UI. Edits persist across restarts on the `auth-data` volume.

> **Note on seeding & scope changes:** the DB is seeded only when empty. If you add a scope in code but the `auth-data` volume already has users, existing users keep their old scopes — grant the new scope via the dashboard, or wipe the volume (`docker compose down -v`) to reseed. A JWT snapshots scopes at login, so **log out and back in** after a scope change.

### Three enforcement layers (defence in depth)

1. **UI (UX only)** — [`frontend/index.html`](frontend/index.html) shows only the service cards / admin dashboard the user's scopes allow.
2. **Gateway** — Kong verifies the JWT **signature** on all business routes.
3. **Service (the real boundary)** — [`security.py`](security.py) re-verifies the JWT and returns **403** unless the required scope is present; the admin API requires the `admin` scope. A route called directly (e.g. `curl`) is still enforced.

---

## How the JWT flow works

1. The client posts credentials to `auth-service`, which verifies them against the SQLite store and returns a JWT signed with the shared secret `mysecret123`, issuer `poc-issuer`, containing `sub` (username) and `scopes`.
2. The client sends `Authorization: Bearer <token>` on subsequent requests.
3. **Kong's JWT plugin** verifies the signature against the consumer's `jwt_secret` (matched by the `iss` claim) — see [`kong.yml`](kong.yml).
4. The upstream service (`security.py`) independently re-decodes the token, checks `exp`/`iss`, and enforces the required **scope**.

---

## Live streaming (collateral review)

The collateral pipeline runs several LLM calls and can take a while, so it streams progress instead of blocking on one long request.

- **Backend:** [`collateral-service`](collateral-service/main.py) exposes `POST /review/stream`. [`streaming.py`](streaming.py) runs the blocking engine on a worker thread and bridges its `emit()` progress callback into **Server-Sent Events** (`text/event-stream`, with `X-Accel-Buffering: no` so Kong/nginx doesn't buffer).
- **Token streaming:** [`provider.py`](provider.py) has a `stream()` method; the human-readable **observations** step streams tokens live. Scanned-PDF OCR reports **page-level** progress instead (pages transcribe in parallel).
- **Frontend:** [`frontend/activity.js`](frontend/activity.js) is a standalone, dependency-free widget that consumes the SSE stream and renders a live stage checklist + a live "LLM output" panel, then resolves with the final result.

**SSE event contract** (each `data:` payload is valid JSON): `open` (connect ack) · `event` (stage/page progress) · `content` (live LLM tokens) · `result` (final object) · `error`.

---

## Policy Q&A (RAG)

`policyqa-service` is a stdlib-only retrieval-augmented chat engine ([`engines/policy_qa.py`](engines/policy_qa.py)):

- Embeddings come from the LLM provider (`provider.embed`, `MODEL_EMBEDDING = openai/text-embedding-3-large`); vectors are stored unit-normalized as a flat `float32` file, and search is plain-Python cosine similarity.
- A **bundled default index** ships in the image at `engines/data/policy_qa_bundled/` (308 chunks, dim 3072).
- Users can **ingest their own policy** (`POST /ingest`): the upload is text/OCR-extracted, chunked, embedded, and saved as that user's **personal index** under the `policyqa-data` volume. Chat uses the personal index when present, otherwise the bundled one. `GET /status` reports which, and `DELETE /index` removes the personal one.

---

## Data storage

- **Users → SQLite.** `auth-service` owns a SQLite DB at `/app/data/users.db`, persisted on the `auth-data` volume. Table `users(username, password_hash, scopes)`; passwords are **PBKDF2-HMAC-SHA256** hashed (stdlib, salted). Seeded from `SEED_USERS` only when empty.
- **Policy Q&A indexes → volume.** Per-user RAG indexes live under `/app/data/indexes/<user>/` on the `policyqa-data` volume. The bundled default index is baked into the image.
- **Audit → flat file.** `audit-service` appends JSON lines to `logs/audit.log`, host-mounted at [`audit-logs/`](audit-logs/). Not a database.
- **Kong → DB-less.** Kong runs in declarative mode from [`kong.yml`](kong.yml); no Kong database.

---

## Shared code & engines

The document/LLM logic lives in the [`engines/`](engines/) package — copied into each business-service image along with [`provider.py`](provider.py), [`security.py`](security.py), and (collateral only) [`streaming.py`](streaming.py):

- **Engines:** `extraction` (text + vision OCR), `collateral`, `document_diff`, `valuation`, `insurance`, `policy_qa`, `util`.
- **Prompts** (frozen domain IP): [`engines/prompts/`](engines/prompts/) — `collateral_extraction`, `collateral_observations`, `extraction_transcription`, `valuation_extraction`, `insurance_extraction`, `insurance_analysis`, `policy_qa_system`.
- **Data:** [`engines/data/`](engines/data/) — `policy.txt`, `collateral_policy_rules.txt`, `default_panel.xlsx` (valuation panel), `policy_qa_bundled/` (RAG index).

---

## Tech stack

- **Kong 3** (DB-less / declarative config)
- **FastAPI** + **Uvicorn** (Python 3.12)
- **PyJWT** (HS256), **SQLite** + **PBKDF2** (stdlib) for the user store
- **pdfplumber**, **PyMuPDF (fitz)**, **python-docx** for text/OCR extraction; **openpyxl** (valuation panel) + **python-dateutil**; **httpx** for LLM calls (OpenRouter-compatible, streaming + embeddings)
- **Docker Compose** for orchestration; vanilla **HTML/CSS/JS** frontend (no build step)

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

Kong becomes healthy first, then the services start.

### 3. Open the frontend

Open [`frontend/index.html`](frontend/index.html) directly in your browser (it's static — not served by Docker). It talks to Kong at `http://localhost`. Sign in (e.g. `admin` / `password123`); you'll see the service cards and admin dashboard your role allows.

> After editing `index.html`/`activity.js`, just refresh the browser. Backend/Dockerfile/`requirements` changes need a rebuild (`docker compose up --build`); `kong.yml` changes need `docker compose restart kong` (it's mounted, not baked in).

### 4. Or use the API directly

```bash
# 1. Log in to get a JWT
TOKEN=$(curl -s -X POST http://localhost/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"password123"}' | jq -r .token)

# 2. Deterministic document diff (fast, no LLM)
curl -X POST http://localhost/api/docdiff/compare \
  -H "Authorization: Bearer $TOKEN" \
  -F original=@original.docx -F returned=@returned.docx

# 3. Policy Q&A (uses the bundled index unless you've ingested your own)
curl -X POST http://localhost/api/policyqa/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"What is the minimum insurance coverage required?","history":[]}'

# 4. Scope enforcement: dave lacks the "collateral" scope → 403
DAVE=$(curl -s -X POST http://localhost/api/auth/login -H "Content-Type: application/json" \
  -d '{"username":"dave","password":"davepass"}' | jq -r .token)
curl -i -X POST http://localhost/api/collateral/review \
  -H "Authorization: Bearer $DAVE" \
  -F legal=@legal.pdf -F property=@property.pdf     # -> 403 Insufficient scope

# 5. Read the audit log
curl http://localhost/api/audit/audit
```

---

## Configuration reference

Key values (hard-coded for the POC — change before any real use):

| Setting                       | Value                              | Where                                     |
|-------------------------------|------------------------------------|-------------------------------------------|
| JWT secret                    | `mysecret123`                      | `kong.yml`, `docker-compose.yml` env      |
| JWT issuer (`iss`)            | `poc-issuer`                       | `kong.yml`, `docker-compose.yml` env      |
| Kong consumer                 | `poc-user`                         | `kong.yml`                                |
| Token expiry                  | 2 hours                            | `auth-service/main.py`                    |
| Scopes                        | collateral, docdiff, valuation, insurance, policy_qa, admin, doc_gen* | `auth-service/main.py`, `security.py` |
| LLM upstream timeouts         | 310 s read/write (LLM services)    | `kong.yml`                                |
| CORS methods                  | GET, POST, PUT, DELETE, OPTIONS    | `kong.yml`                                |
| LLM base URL / key            | OpenRouter / `LLM_API_KEY`         | `docker-compose.yml`, `.env`              |
| Kong proxy / admin ports      | `80` / `8001`                      | `docker-compose.yml`                      |
| Volumes                       | `auth-data`, `policyqa-data` (+ `./audit-logs` bind) | `docker-compose.yml`    |

\* `doc_gen` is a reserved scope; no service is wired for it yet.

> The LLM services' upstream timeouts are raised to ~310 s (past `provider.py`'s 300 s HTTP timeout) because the pipelines can run for minutes; Kong's 60 s default would otherwise return a 504.
