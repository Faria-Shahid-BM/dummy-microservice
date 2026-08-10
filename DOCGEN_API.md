# Docgen Service — Frontend Integration Guide

The **docgen-service** is the stateful "core" of the platform: AI-assisted credit
document generation with a full maker–checker approval workflow. Unlike the other
microservices (one stateless call in → one result out), docgen is a **multi-step,
stateful pipeline** backed by its own database and file storage. It sits behind
Kong like every other service and trusts Kong's JWT.

This document is everything a frontend engineer needs to build the UI.

---

## 1. Base URL & routing

Everything goes through **Kong** at `http://localhost` (the same origin as the other
services). Kong forwards these path prefixes to docgen-service (JWT-protected):

- `/api/profiles/**` — profiles, cases, the whole pipeline, templates, approvals list
- `/api/jobs/**` — job status + progress stream
- `/api/approvals/**` — submit / decide on an approval
- `/api/notifications` — the signed-in user's notifications

> Only use these prefixes. The container also holds the reviewer/policy-qa code,
> but Kong routes those to their own services — those endpoints are unreachable here.

---

## 2. Authentication (JWT)

Auth is handled by the separate **auth-service**, not docgen. The flow:

1. `POST /api/auth/login` with `{ "username", "password" }` → `{ token, username, scopes }`.
2. Attach the token on every docgen call: `Authorization: Bearer <token>`.
3. Tokens **expire after 2 hours** — on a `401 "Signature has expired"`, log in again.

### Scopes → roles

docgen-service maps JWT `scopes` to its internal role model:

| Scope(s) | Role | Can do |
|---|---|---|
| `docgen` | **maker** | Create cases, upload input/templates, run the pipeline, submit for approval |
| `docgen` + `docgen_check` | **checker** | Approve / reject submissions (cannot create) |
| `admin` (+ `docgen`) | **admin** | Everything — but **cannot approve their own work** (four-eyes) |

**A token needs `docgen` (or `admin`) just to reach the service** — a user with only
`docgen_check` is rejected (403). So a checker user must have **both** `docgen` and
`docgen_check`.

### You need at least TWO users
The four-eyes rule forbids the same person from approving their own work. To run the
full flow you need a **maker** and a **different checker**. (Seeded for testing:
`admin`/`password123` as maker, `checker`/`checkerpass` as checker.)

---

## 3. The workflow (the order the UI drives)

```
create case
  → upload input (.pdf/.docx)
  → extract            (job)   → case text
  → analyze            (job)   → credit analysis        [optional for select]
  → select             (job)   → which templates this case needs
  → fill               (job × N) → generated .docx documents
  → submit each document
  → checker approves
  → download (approved-only)
```

Templates live in a **separate library** (set up once, reused by every case):

```
upload template (.docx)
  → analyze version   (job)   → descriptor (lets `select` recognize it)
  → checker approves          → version becomes "current" & usable
```

**Select only sees templates whose current version is approved AND has a descriptor.**
So the template library must be populated + approved before `select`/`fill` will produce anything.

---

## 4. Endpoints

### Profiles
Everything is scoped to a profile. A non-default **"Workspace"** profile is seeded — use it.
- `GET /api/profiles` → `{ profiles: [{ id, name, is_default, role }] }` — pick the one with `is_default:false`.
- `GET /api/profiles/{pid}`

### Cases
- `GET  /api/profiles/{pid}/cases` → list
- `POST /api/profiles/{pid}/cases` `{ name }` → case
- `GET  /api/profiles/{pid}/cases/{cid}` → case + stage flags (`has_input`, `has_case_text`, `has_analysis`, `has_selected`, `generated_count`, `active_jobs`)
- `POST /api/profiles/{pid}/cases/{cid}/input` — **multipart** `file=` (.pdf/.docx). One input per case (replaces).
- `POST /api/profiles/{pid}/cases/{cid}/extract` → **job**
- `GET/PUT /api/profiles/{pid}/cases/{cid}/case-text` — read / hand-edit the extracted text
- `POST /api/profiles/{pid}/cases/{cid}/analyze` → **job**
- `GET/PUT /api/profiles/{pid}/cases/{cid}/analysis`
- `POST /api/profiles/{pid}/cases/{cid}/select` → **job** (needs case-text; 400 otherwise)
- `GET/PUT /api/profiles/{pid}/cases/{cid}/selected`
- `POST /api/profiles/{pid}/cases/{cid}/fill` `{ tasks?: string[] }` → `{ jobs:[{task_key, job_id, status}], skipped:[...] }` (omit `tasks` = fill all)

### Generated documents
- `GET  /api/profiles/{pid}/cases/{cid}/documents` → `{ documents:[{ id, file_name, applied_ops, failed_ops, unfilled_fields, needs_attention, approval_state, approval_id }], active_jobs }`
- `POST /api/profiles/{pid}/cases/{cid}/documents/{docId}/submit` — maker submits for review
- `GET  /api/profiles/{pid}/cases/{cid}/documents/{docId}/download` — the single .docx
- `GET  /api/profiles/{pid}/cases/{cid}/documents/{docId}/provenance` — what was filled/failed
- `GET  /api/profiles/{pid}/cases/{cid}/documents/download-all?approved_only=true` — zip

### Template library
- `GET  /api/profiles/{pid}/templates` → list (with `current_version_no`)
- `POST /api/profiles/{pid}/templates` — **multipart** `file=` (**.docx only**), `name=`, `language=` (en|ar|bilingual), `note=` → creates template + v1, auto-submits for approval
- `GET  /api/profiles/{pid}/templates/{tid}` → template + `versions[]` (`has_descriptor`, `is_current`, `approval_state`)
- `POST /api/profiles/{pid}/templates/{tid}/versions` — multipart, upload a new version
- `POST /api/profiles/{pid}/templates/{tid}/versions/{vid}/analyze` → **job** (generates the descriptor)
- `GET/PUT /api/profiles/{pid}/templates/{tid}/versions/{vid}/descriptor`
- `GET  /api/profiles/{pid}/templates/{tid}/versions/{vid}/file` — download the .docx

### Approvals (the maker–checker gate)
Used for **both** template versions and generated documents.
- `GET  /api/profiles/{pid}/approvals?state=pending&subject_type=template_version|generated_document`
  → `{ approvals:[{ id, subject_type, subject_id, subject:{name,link}, state, maker, checker, ... }] }`
- `POST /api/approvals/{aid}/submit` — maker submits (draft/rejected → pending)
- `POST /api/approvals/{aid}/decide` `{ approve: bool, comment: string }` — **checker only**, and never the maker (403). A comment is **required** when rejecting.

State machine: `draft → pending → approved | rejected` (rejected can be resubmitted).
Approving a **template version** makes it the current/usable one. Approving a
**generated document** unlocks it for the approved-only download.

### Jobs (long steps run async)
Any step marked "job" returns a job object `{ id, kind, status, error, result, ... }`.
- `GET /api/jobs/{jid}` — poll status: `queued → running → succeeded | failed`.
- `GET /api/jobs/{jid}/stream` — **SSE** live progress.

**SSE format** (note: this is the monolith format — *different* from the collateral
service's `event:`-typed stream). Each line is `data: {json}`:
- `{"type":"reasoning","text":...}` / `{"type":"content","text":...}` — live model output
- `{"type":"event","text":"<compact json>"}` — stage/page progress (e.g. `{"event":"page_complete","page":3,"total":10}`)
- `{"type":"done"}` — finished OK
- `{"type":"error","error":...}` — failed
The server replays the buffer on connect, so reset accumulated text on each `open`.
If SSE is inconvenient, **polling `GET /api/jobs/{jid}` every few seconds works fine.**

### Notifications
- `GET /api/notifications` — the signed-in user's notifications (e.g. checkers get "review requested").

---

## 5. Notes & gotchas

- **Templates must be `.docx`.** Case inputs may be `.pdf` or `.docx`.
- **A "template" is a blank form** to be filled — not the source case document.
- `failed_ops` / `unfilled_fields` on a generated document are normal to a degree —
  they mean the model couldn't map some fields. Surface `needs_attention` in the UI.
- **Case status is progressive** (`new → input → extracted → analyzed → selected → generating → generated`) and only moves forward.
- **LLM steps can take a while** (extraction of a scanned PDF does vision OCR per page).
  Show progress from the job. If a step fails with an upstream idle-timeout, retrying
  usually works.
- **Everything is behind Kong's JWT**, and each mutating action is also role-checked
  server-side (defense in depth) — the UI hiding a button is not the security boundary.
