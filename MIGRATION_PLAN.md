# CAD Workbench → Kong + per-module microservices

Migration plan. Target: Kong as the single entry point owning authentication and
route-level authorization; one service per module behind it; Angular frontend
unchanged in shape, served by its own container.

---

## 0. What Kong can and cannot own

Kong can own **authentication** (verify the token is genuine and unexpired) and
**coarse authorization** (does this token's role/entitlement permit this route at
all). It cannot own three rules you currently enforce, because they are
*data-dependent* — the decision needs a database row, which no gateway has:

| Rule | Where it lives now | Why Kong can't decide it |
|---|---|---|
| The decider can never be the maker | [approvals.py:165](backend/app/control/approvals.py#L165) | Needs `approval.maker_id` |
| The Default profile is read-only | [deps.py:115](backend/app/auth/deps.py#L115) | Needs `profile.is_default` |
| You cannot remove your own admin role / deactivate yourself | [admin.py:115](backend/app/admin.py#L115), [:120](backend/app/admin.py#L120) | Needs to compare caller to target row |

So the division of labour is:

- **Kong**: is the signature genuine → is it unexpired → does this role reach this
  route → is this module in the token's entitlement list. Rejects with 401/403
  before any service is touched.
- **Services**: record-level rules only, on requests Kong has already cleared.

This is the same split README_f draws between its `service-entitlement` plugin's
403 ("you are not entitled to this service at all") and an in-service 403 ("you
are entitled to be here, you just can't do *this*"). It is defense in depth, not
duplication — a service reached directly on the internal network is still safe.

Everything below maximises what Kong owns within that constraint.

---

## 1. Token model

Kong can only make role decisions from claims it can read, so the opaque
DB-backed session token has to go. The design that keeps your current browser
security:

**RS256 JWT carried in an httpOnly cookie.**

Kong's `jwt` plugin reads tokens from a cookie via `config.cookie_names`, so it
verifies the signature straight out of `cw_session` — you do not have to move the
token into JS-readable storage. What you keep and what you trade:

| | Today | After |
|---|---|---|
| httpOnly / XSS-safe | yes | **yes** (cookie retained) |
| Kong can verify it | no | **yes** |
| Private key exposure | n/a | auth-service only; Kong holds the public half |
| Revocation | instant ([admin.py:125](backend/app/admin.py#L125)) | within the token TTL |
| Sliding idle timeout | yes ([deps.py:50](backend/app/auth/deps.py#L50)) | via refresh cadence |

Follow README_f's asymmetric model, not README_h's shared HS256 secret: only
auth-service can *mint*; Kong can only *verify*. A public key in `kong.yml` is
not a secret.

**Revocation mitigation.** 10-minute access token + a refresh endpoint on
auth-service. Deactivating a user takes effect within 10 minutes. If a bank
control owner won't accept a 10-minute window, add a Redis deny-list checked by
the claims-guard plugin — it is already doing per-request work, so the marginal
cost is one Redis GET.

**CSRF.** A cookie-borne token still needs CSRF defense. Set `SameSite=Strict`
and add an `Origin`/`Referer` check to the Kong plugin. Your double-submit
middleware ([main.py:72-95](backend/app/main.py#L72-L95)) moves to Kong and is
deleted from the app.

**Claims:**

```json
{
  "iss": "cad-workbench",
  "sub": "<user id>",
  "username": "admin.user",
  "role": "admin | maker | checker",
  "can_edit_config": true,
  "modules": ["collateral", "valuation", "docgen", "policy-qa"],
  "iat": 1700000000,
  "exp": 1700000600
}
```

**`modules` is what the tick-box dashboard writes.** Today
[admin-users.html:117-177](frontend/src/app/features/admin/admin-users.html#L117-L177)
toggles `is_admin` / `can_edit_config` / `is_active`. Add a `user_module_grants`
table and one checkbox per module; login puts the grants into the `modules`
claim; Kong's plugin checks it per route. Same caveat README_f flags: entitlements
are a snapshot taken at token issue, so a new grant lands on the next
refresh — ≤10 minutes with the TTL above.

---

## 2. Target topology

```
                        BROWSER
                           │
                           ▼
              ┌──────────────────────────┐
              │  frontend (nginx :80)    │   ng build output +
              │  /api/* → kong           │   /api/* reverse proxy
              └──────────────────────────┘
                           │
                           ▼
      ┌──────────────────────────────────────────┐
      │                  KONG                     │
      │  jwt (cookie_names=cw_session, exp)       │  ← authentication
      │  claims-guard (role + module, PRIO 899)   │  ← coarse authorization
      │  cors · rate-limiting · size-limiting     │
      │  correlation-id · prometheus              │
      │  injects X-User-Id / X-User-Role upstream │
      └──────────────────────────────────────────┘
          │       │        │        │        │
   ┌──────┘  ┌────┘   ┌───┘   ┌────┘   ┌───┘
   ▼         ▼        ▼       ▼        ▼
 auth    profile   jobs   approval  template     ← platform services
 audit   notify                                   (audit POST: no Kong route)
   │
   ├── document-reviewer   (no LLM)
   ├── collateral-reviewer
   ├── valuation-reviewer                        ← one per module
   ├── insurance-reviewer
   ├── policy-qa
   └── docgen

 shared infrastructure:  PostgreSQL (schema per service) · Redis (jobs+SSE)
                         MinIO / S3 (replaces DATA_DIR)
```

Only `frontend` publishes a host port. Kong's proxy and its Admin API stay
internal — README_f §4e is right that the Admin API in particular must never be
exposed.

---

## 3. Service inventory

### Platform services

| Service | Owns | Kong route | Guard |
|---|---|---|---|
| **auth** | `users`, `user_module_grants`, refresh state; OIDC ([providers/](backend/app/auth/providers/)) | `/api/auth/*` login+refresh: **no jwt plugin** (can't require a token to get one). `/api/admin/users*`: role=admin | mints tokens; holds the private key |
| **profile** | `profiles`, `profile_config_overrides` ([control/profile_config.py](backend/app/control/profile_config.py)) | `/api/profiles`, `/api/profiles/{id}/config` | member read, `can_edit_config` write |
| **audit** | `audit_log` | `GET /api/audit` only, role=admin | `POST /audit` has **no route** — internal network only (README_f's `methods: [GET]` trick) |
| **notify** | `notifications` | `/api/notifications` | own token |
| **jobs** | `jobs` table + Redis streams + SSE | `/api/jobs/{id}`, `/api/jobs/{id}/stream` | long `read_timeout`, buffering off |
| **approval** | `approvals` | `/api/approvals*`, `/api/profiles/{id}/approvals` | role gate at Kong; **maker≠checker stays in the service** |
| **template** | `templates`, `template_versions` | `/api/profiles/{id}/templates*` | module=`templates` |

### Domain services (one per module)

| Service | Engine | Own tables | Notes |
|---|---|---|---|
| **document-reviewer** | [document_diff.py](backend/app/engines/document_diff.py) | own `reviews` | no LLM — **split this one first** |
| **collateral-reviewer** | [collateral.py](backend/app/engines/collateral.py) | own `reviews` | |
| **valuation-reviewer** | [valuation.py](backend/app/engines/valuation.py) | own `reviews` | ships `data/default_panel.xlsx` |
| **insurance-reviewer** | [insurance.py](backend/app/engines/insurance.py) | own `reviews` | ships `data/insurance/*` |
| **policy-qa** | [policy_qa.py](backend/app/engines/policy_qa.py) | own | owns its vector index — cleanest boundary in the whole system |
| **docgen** | [engines/docgen/](backend/app/engines/docgen/) | `cases`, `generated_documents` | 1154-line router, the heaviest |

The four reviewers each get their **own** `reviews` table, so `Review.module`
disappears as a discriminator — it becomes implicit in which service you're
talking to.

**13 services + Kong + Postgres + Redis + MinIO.** That is the real container
count; plan CI, secrets, and log aggregation for it up front.

---

## 4. `cad-common` — the shared library

This is README_f's `common/` and README_h's copied `security.py`/`provider.py`,
done as a real versioned package. Without it you get 13 copies of everything.

| Module | Replaces | Notes |
|---|---|---|
| `claims.py` | [auth/deps.py](backend/app/auth/deps.py) | reads Kong-injected `X-User-*` headers; `require_role()`, `require_module()` factories. Keep the *data-dependent* halves of the current guards. |
| `audit_client.py` | [app/audit.py](backend/app/audit.py) | fire-and-forget POST. 10 modules import audit today — all become this. |
| `notify_client.py` | [app/notify.py](backend/app/notify.py) | |
| `jobs_client.py` | [jobs/runner.py](backend/app/jobs/runner.py) submit + emit | emit writes to a Redis stream |
| `config_client.py` | `effective_model()` / `prompt_override()` | **biggest behavioral change** — see §5D |
| `storage.py` | [app/storage.py](backend/app/storage.py) | S3/MinIO; keep the key-confinement checks |
| `llm/` | [app/llm/](backend/app/llm/) | registry + openai_compat + azure_openai + bedrock, moved verbatim |
| `reviews_base.py` | [modules/reviews_base.py](backend/app/modules/reviews_base.py) | the router factory |
| `engines/extraction.py`, `engines/util.py` | same | shared by all four reviewers |

**Your `make_review_router` factory is what makes four reviewer microservices
affordable.** Without it you would hand-write the list/create/upload/analyze/result
surface four times — exactly the 76% copy-paste README_h's POC suffered. Keep it
generic; resist per-service forks of it.

Ship it as a versioned wheel built once in CI. An unversioned shared library
across 13 services forces lockstep deploys, which is a distributed monolith with
extra steps.

---

## 5. The five hard migrations

Dependency-ordered. Every one is done **inside the current monolith** — that is
the point. Do not create a second container until §5A–E are finished.

### A. Break the database apart

Today one schema has foreign keys crossing every proposed boundary:
`Review`, `Template`, `Case`, `Approval` all `ForeignKey("profiles.id",
ondelete="CASCADE")` ([models.py:173-255](backend/app/models.py#L173-L255)),
`Notification` → `users.id`.

1. **Drop the cross-service FKs first, in the monolith.** `Review.profile_id`
   becomes a plain `String(32)` with app-level validation. Alembic migration; no
   behavior change if validation is correct — which your tests will tell you.
2. **Replace `ondelete="CASCADE"` with an explicit event.** profile-service
   publishes `profile.deleted`; each service deletes its own rows and its own
   storage prefix. Write this *before* splitting — until it exists, deleting a
   profile silently orphans reviews, cases, templates and their files.
3. **Kill the cross-service joins.** [approvals.py:82-83](backend/app/control/approvals.py#L82-L83)
   does `db.get(User, maker_id)` and `db.get(User, checker_id)` to render a name.
   Denormalise `maker_display_name` / `checker_display_name` onto the approval
   row rather than calling auth-service per render — for an audit record, the
   name *at decision time* is the correct value to retain anyway.
4. Then: **schema-per-service in one Postgres instance** before separate
   instances. Cheap, reversible, and it proves the boundaries hold.

### B. `DATA_DIR` → object storage

[storage.py](backend/app/storage.py) is the single choke point for every
filesystem touch — that is very good news. Swap its body for boto3/MinIO, keeping
the accessor signatures (`review_dir`, `case_output_dir`, …) but returning key
prefixes.

The engines take `Path` arguments ([`AnalyzeFn` = `dict[str, Path]`](backend/app/modules/reviews_base.py#L46)),
so add a download-to-tempdir step at the job boundary and upload results back.
**Do not rewrite the engines** — they are the valuable, tested part
([tests/engines/](backend/tests/engines/) should not need to change at all).

### C. Jobs + SSE → Redis

The hardest piece. [`JobBuffer`](backend/app/jobs/runner.py#L35) is a
process-local dict and SSE reads it directly; both the
[Dockerfile](deploy/Dockerfile) and [main.py](backend/app/main.py#L4) say
*single-worker* because of it.

- `jobs-service` owns the `jobs` table and the SSE endpoint.
- Workers in each domain service `emit()` into a **Redis Stream** keyed by job
  id, instead of `buffer.append()`.
- The SSE endpoint subscribes to that stream — so it works regardless of which
  container ran the job. Use Streams, not pub/sub: you need the
  replay-from-start that [`subscribe()`](backend/app/jobs/runner.py#L59) gives
  you today (a browser attaching mid-job must see prior output).
- The TTL reaper becomes a Redis key expiry.
- Kong: `read_timeout` above your longest job (README_h hit exactly this — a 60s
  default 504s a multi-minute LLM pipeline) and buffering off
  (`X-Accel-Buffering: no`).
- **`mark_orphans_failed()` ([runner.py:113](backend/app/jobs/runner.py#L113))
  becomes wrong** once workers are separate from jobs-service: restarting
  jobs-service would fail jobs still running elsewhere. Replace it with worker
  heartbeats + a staleness sweep.

### D. Per-profile config fan-out

Every module resolves prompts and models *inside the job* via a local DB read —
[collateral.py:22-37](backend/app/modules/collateral.py#L22-L37) is the pattern,
and 5 modules do it. That becomes an HTTP call to profile-service on every job.

- Cache with a 30–60s TTL in `config_client`.
- **Fail closed onto the shipped default, never error.** profile-service becoming
  a hard dependency of all six domain services is a genuine availability risk.
  You already have shipped fallbacks — the `_file()` defaults at
  [profile_config.py:40](backend/app/control/profile_config.py#L40) — so use them
  as the degraded path and log loudly.

### E. Auth → Kong

Last, not first, because it touches every service.

1. Add JWT minting to auth alongside cookie sessions; both work simultaneously.
2. Add `user_module_grants` + the per-module tick UI.
3. Stand Kong up in front of the monolith with `jwt` + `claims-guard`.
4. Swap `require_admin` / `require_profile_role` for the claim-reading versions
   in `cad-common`, **keeping** the Default-profile and maker≠checker halves.
5. Delete the `sessions` table only once everything works end to end.

---

## 6. Kong configuration shape

Two plugins do all the work. `claims-guard` at `PRIORITY = 899` — deliberately
below the `jwt` plugin's 1005, so the signature is already verified before it
trusts any claim (README_f's reasoning, and it is correct).

```yaml
_format_version: "3.0"

consumers:
  - username: cad-issuer
    jwt_secrets:
      - key: cad-workbench          # matches the iss claim
        algorithm: RS256
        rsa_public_key: |
          -----BEGIN PUBLIC KEY-----
          ...
          -----END PUBLIC KEY-----

plugins:                            # global
  - name: cors
  - name: rate-limiting
    config: { minute: 120, policy: local }
  - name: request-size-limiting
    config: { allowed_payload_size: 50 }   # matches MAX_UPLOAD_MB
  - name: correlation-id
  - name: prometheus

services:
  # ---- public: you cannot require a token to get your first one -------------
  - name: auth-public
    url: http://auth:8000
    routes:
      - name: login
        paths: ["/api/auth/login", "/api/auth/refresh", "/api/auth/oidc"]
        # no jwt plugin

  # ---- admin: role gate at the edge ----------------------------------------
  - name: auth-admin
    url: http://auth:8000
    routes:
      - name: admin-users
        paths: ["/api/admin/users"]
        plugins:
          - name: jwt
            config:
              cookie_names: ["cw_session"]
              claims_to_verify: ["exp"]
          - name: claims-guard
            config: { required_role: ["admin"] }

  # ---- a module service: role + entitlement --------------------------------
  - name: collateral
    url: http://collateral-reviewer:8000
    read_timeout: 600000            # LLM pipelines run for minutes
    routes:
      - name: collateral-reviews
        paths: ["~/api/profiles/[^/]+/reviews/collateral"]
        plugins:
          - name: jwt
            config: { cookie_names: ["cw_session"], claims_to_verify: ["exp"] }
          - name: claims-guard
            config:
              required_module: collateral
              required_role: ["admin", "maker", "checker"]

  # ---- audit: GET only; POST /audit is unreachable from outside ------------
  - name: audit
    url: http://audit:8000
    routes:
      - name: audit-read
        paths: ["/api/audit"]
        methods: ["GET"]
        plugins:
          - name: jwt
            config: { cookie_names: ["cw_session"], claims_to_verify: ["exp"] }
          - name: claims-guard
            config: { required_role: ["admin"] }

  # ---- SSE: long-lived, unbuffered ----------------------------------------
  - name: jobs
    url: http://jobs:8000
    read_timeout: 3600000
    routes:
      - name: job-stream
        paths: ["~/api/jobs/[^/]+/stream"]
        plugins:
          - name: jwt
            config: { cookie_names: ["cw_session"], claims_to_verify: ["exp"] }
```

`claims-guard/schema.lua` declares three fields — `required_role` (array),
`required_module` (string), `check_origin` (boolean) — and `handler.lua` contains
the logic **once**, plus injects `X-User-Id` / `X-User-Role` /
`X-User-Can-Edit-Config` upstream so services need not re-decode. Volume-mount the
plugin folder like `kong.yml` so route changes need no image rebuild.

Adding a 14th service then needs no new mechanism: one route, `jwt` +
`claims-guard`, a different `required_module`.

---

## 7. Sequencing

| Phase | Work | Weeks | Shippable outcome |
|---|---|---|---|
| **0** | Kong in front of the monolith, routing only. Stop publishing the app port. | 1 | A real gateway. Every existing test must still pass through it. |
| **1** | Token model: RS256 JWT in httpOnly cookie, `modules` grants + tick UI, `claims-guard` plugin, CSRF moves to Kong. | 2–3 | **Everything you asked for except the split** — Kong doing authn + authz + RBAC, admin ticks granting module access. Pause and validate here. |
| **2** | Extract `cad-common`; every module imports clients instead of calling in-process. No new containers. | 2–3 | Seams proven while rollback is still trivial. |
| **3** | MinIO + `storage.py` swap. | 1–2 | Stateless app container. |
| **4** | Redis jobs + SSE. | 2–3 | Multi-worker safe — verify with `uvicorn --workers 4`. |
| **5** | Drop cross-service FKs, denormalise the user-name joins, add `profile.deleted`. | 1 | Schema ready to cut. |
| **6** | Split, one service at a time: audit → notify → **document-reviewer** (no LLM, simplest) → collateral → valuation → insurance → policy-qa → docgen → template+approval → profile → **auth last**. | 4–8 | Per-module microservices. |
| **7** | Frontend container + nginx; delete the SPA handler at [main.py:142](backend/app/main.py#L142). | 1 | Final topology. |

**4–6 months for one engineer; 2–3 with two.** Phases 0–4 are roughly 8 weeks and
carry most of the operational value.

The load-bearing point: **phases 1–5 all happen inside the monolith.** Each is
independently valuable, independently shippable, and reversible. Phase 6 is
mechanical once they are done — and dangerous before.

Split order rationale: leaves first (audit, notify — no one depends on them),
`document-reviewer` next because it needs no LLM credentials so a failure there is
unambiguous, and `auth` last because everything depends on it.

---

## 8. Frontend

Genuinely small — the Angular app is already shaped for this.

- `withCredentials` stays; the token remains a cookie.
- Remove the CSRF interceptor once Kong does origin checking.
- Build the sidebar from the `modules` claim (README_f's pattern), so a user sees
  only entitled modules. [shell.ts](frontend/src/app/layout/shell.ts) is where
  this goes.
- Add per-module tick-boxes to [admin-users.html](frontend/src/app/features/admin/admin-users.html)
  next to the existing `is_admin` / `can_edit_config` / `is_active` ones.
- Distinguish three rejections in the interceptor: **401 from Kong** (no/expired
  token → redirect to login), **403 from Kong** (not entitled to the module →
  "your account does not have access to X"), **403 from a service** (entitled but
  the action is not permitted → show the `detail`). The 401 half already exists at
  [api.service.ts:26](frontend/src/app/core/api.service.ts#L26).
- SSE reconnect handling matters more once a job's stream can outlive a
  redeploy of the service that started it.

---

## 9. Tests

[backend/tests/](backend/tests/) has 15 files. Two groups behave very differently:

- `test_auth.py`, `test_rbac.py`, `test_admin_users.py` assert the cookie/DB
  session model. **Rewrite these in phase 1**, not phase 6 — they are your proof
  the token swap preserved behavior.
- [tests/engines/](backend/tests/engines/) should survive the entire migration
  **untouched**. If a phase breaks them, the engines have stopped being pure and
  you have reintroduced the coupling the split is meant to remove. Treat that as
  a build failure, not a test to update.

Add per-phase integration tests through Kong from phase 0 onward, so "works
directly" and "works through the gateway" never diverge silently.
