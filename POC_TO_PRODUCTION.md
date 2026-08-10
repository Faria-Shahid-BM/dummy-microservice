# POC → Production: Gap Assessment

**Internal document — not for client distribution.**

The system works end to end and is a good POC. This is the list of things that are POC-level by
design and should be decided on *before* it is handed to a client, so nothing is discovered during
their security review.

Nothing here is a defect in what was built. These are the deliberate shortcuts that come with a
proof of concept, written down so they can be prioritised rather than inherited.

> **Branch note (2026-08-06):** this document originally described `main`. The team decided to move
> past demo/POC status and build the production system on `haiqa-with-angular-frontend` instead
> (Haiqa's backend services + main's Angular frontend). That merge **silently regressed #2** — the
> RS256 work described below doesn't exist on this branch; auth reverted to hardcoded HS256. Item #2
> has been un-resolved below and items 3/8 renamed to this branch's actual accounts/paths. Item #12
> (org-level entitlements) and #13 (schema/migrations) are new, added when this branch's work started.

---

## Summary

| # | Gap | Severity | Blocks client handover? | Rough effort |
|---|---|---|---|---|
| 1 | Services trust JWT claims without verifying the signature | **High** | Yes | ~1 day |
| 2 | JWT signing secret hardcoded, HS256 shared secret (**regressed** — see branch note above) | **High** | Yes | ~1 day (redo RS256 wiring) |
| 3 | Demo accounts `admin`/`carol`/`dave` seeded automatically | **High** | Yes | ~1 hour |
| 4 | No TLS — HTTP only | **High** | Yes (or LB terminates) | 0 if upstream LB, ~0.5 day if on-box |
| 5 | Document text sent to an external LLM | **High** | Yes — compliance | Config + client sign-off |
| 6 | No restart policy — stack dies on reboot | Medium | Should fix | ~15 min |
| 7 | Audit log is a plain file on one server | Medium | Should fix | Depends on SIEM |
| 8 | User store is SQLite on local disk, scopes stored as a JSON blob column | Medium | Acceptable for pilot | ~2 days for Postgres |
| 9 | Secrets live in a `.env` file on disk | Medium | Acceptable for pilot | Depends on Vault access |
| 10 | CORS allows `http://localhost:4200`, and `auth-service` allows `*` at the app level | Low | No | ~5 min |
| 11 | No upload size or page-count guard on document compare | Low | No | ~1 hour |
| 12 | No organization/tenant concept — admin grants services per-user only | Medium | No, but requested for production | ~2-3 days |
| 13 | No migration tooling — schema is a raw `CREATE TABLE IF NOT EXISTS` on every boot | Medium | Should fix before #8/#12 | ~0.5 day (Alembic) |

---

## 1. Services trust JWT claims without verifying the signature

**What it is.** [`common/claims.py`](common/claims.py) `get_claims()` splits the bearer token, base64-decodes
the payload, and returns it. There is no signature check. Every service — including
`require_role("admin")` in `auth-service` — acts on that unverified data.

**Why it matters.** The design assumes every request arrives through Kong, which does verify. That
holds today: the backend services publish no host ports and nothing else runs on the Compose network.
But the protection is a single network control with nothing behind it. Any container later added to
that network can call `auth-service:8000` directly, present a handmade token with no signature at all
claiming `role: admin`, and create users.

A bank's security review will ask what happens if the gateway is bypassed. "It can't be" is a weaker
answer than "the services check too."

**Recommended fix.** Verify the signature in `get_claims()` and return 401 on failure. Pairs with #2
— see the note there on why this only makes sense with RS256.

---

## 2. JWT signing secret hardcoded — **regressed, RS256 work needs redoing on this branch**

**What it is now.** `mysecret123` is hardcoded as the fallback in three places, and all three must
match: [`auth-service/main.py:26`](auth-service/main.py#L26), [`security.py:32`](security.py#L32)
(read by every downstream service), and [`kong.yml:114`](kong.yml#L114). Both app files are tracked
in git, so a `git pull` on a deployed server silently restores the known placeholder. Signing/verifying
uses HS256 throughout (`security.py:48`, and `kong.yml`'s `jwt` plugin per-route).

**What was previously done on `main`, and is gone on this branch.** An earlier pass on `main` moved to
RS256 asymmetric signing — private key read from `JWT_PRIVATE_KEY_PATH` with startup refusal if
unreadable, `kong.yml` switched to `algorithm: RS256` + inline `rsa_public_key`, a keypair generator at
`scripts/generate_jwt_keys.py`, `keys/` mounted read-only and gitignored, `pyjwt[crypto]` in
requirements — and was verified end to end (11 checks: correct `alg: RS256`, rejection of tokens signed
with the old secret, rejection of tokens signed with a different RSA key, rejection of `alg: none`,
entitlement still enforced).

That work does not exist in `auth-service/main.py`, `security.py`, or `kong.yml` on
`haiqa-with-angular-frontend` — this branch's auth stack came from Haiqa's backend, which used HS256,
and the RS256 change from `main` was never carried over. A `keys/` directory with a keypair is present
on disk (untracked) but nothing in the code reads it. **Treat this as needing to be redone, not
finished** — same target design as before, but re-applied against Haiqa's `auth-service/main.py` and
`security.py` instead of `common/claims.py`.

**Why RS256 rather than hiding the secret.** Two mechanisms for injecting a secret into `kong.yml` were
tested against Kong 3.9.3 and **neither works**, both failing silently:

- `secret: ${{ env "KONG_JWT_SIGNING_SECRET" }}` — Kong stores that *literal string* and uses it as the
  signing secret. `kong config parse` reports `parse successful`, variable set or unset.
- `secret: "{vault://env/jwt-signing-secret}"` — a correctly-signed token is **rejected with 401**,
  with the variable set, unset, or `KONG_`-prefixed. Kong logged nothing about vaults, suggesting the
  JWT credential `secret` field is not vault-referenceable in the entity schema.

Both tests were validated with controls in the same harness: a literal secret gave 401 with no token,
502 with a correctly-signed token, and 401 with a wrong signature — so the method distinguishes the
outcomes correctly and the negative results are real.

RS256 sidesteps all of it: the only key in `kong.yml` is public, so there is nothing to inject.

**Remaining work.** Re-wire RS256 signing in `auth-service/main.py` and verification in `security.py`
+ `kong.yml`, reusing the on-disk `keys/` pair or regenerating it. Once done, the private key is a file
mounted into the container — for production that should come from Vault / Key Vault instead, see #9.
Key rotation is manual and invalidates all issued tokens.

**Note on #1.** RS256 is also what makes #1 cheap. Under HS256 (current state), a service verifying
signatures needs the *signing* secret — one secret in one place becomes the same secret in six
services. With RS256 each service needs only the public key and holds nothing sensitive.

---

## 3. Demo accounts seeded automatically

**What it is.** [`auth-service/main.py:37-41,84-89`](auth-service/main.py#L37-L41) seeds
`admin`/`password123` (all scopes), `carol`/`carolpass` (collateral, valuation), and
`dave`/`davepass` (docdiff, insurance) whenever the user table is empty — so on every fresh install,
and again if the database file is ever lost.

**Why it matters.** A known-credential admin account on a client system. The installation guide tells
them to delete both after setup (§5.4), but that relies on someone remembering, and a database reset
re-creates them.

**Recommended fix.** Seed only when an explicit `SEED_DEMO_USERS=true` flag is set. Local development
keeps working unchanged; a client install starts with an empty user table and creates a real admin.
Optionally, bootstrap a single admin from `ADMIN_USERNAME`/`ADMIN_PASSWORD` and refuse to start if
neither those nor the demo flag are provided.

---

## 4. No TLS

**What it is.** [`frontend/nginx.conf`](frontend/nginx.conf#L2) has one `listen 80;` block and no
certificate configuration. Credentials and bearer tokens cross the network in clear text.

**Options.** If the client terminates TLS at a load balancer or WAF in front of the server — the usual
arrangement, and what most banks require anyway — **no application change is needed**. If it has to
terminate on this box, that is real work: a 443 server block, certificate and key mounted into the
`frontend` container, 443 published in Compose, and an 80 → 443 redirect.

**Action.** Confirm which with the client's network team. Do not assume; it changes the delivery.

---

## 5. Document text sent to an external LLM

**What it is.** `collateral-reviewer` and `document-reviewer` post document text to whatever
`LLM_BASE_URL` points at. [`.env.example`](.env.example#L6) currently defaults to
`https://openrouter.ai/api/v1`, and the model defaults in
[`docker-compose.yml:76-77`](docker-compose.yml#L76-L77) are public OpenAI model names.

**Why it matters.** That text is customer collateral and legal document content. Sending it to a public
provider is normally a hard compliance blocker on data residency and confidentiality grounds — not a
technical problem but a contractual one.

**Recommended fix.** No code change needed: the client points `LLM_BASE_URL` at an internally hosted
or approved model. Two things follow from that — the two model-name variables must be set explicitly,
because the `gpt-4o-mini` defaults won't exist on an internal endpoint, and the approved endpoint must
be confirmed with the client **before** installation day. It is the one dependency that cannot be
improvised.

---

## 6. No restart policy

No service in [`docker-compose.yml`](docker-compose.yml) sets `restart:`, so the stack does not come
back after a server reboot. Adding `restart: unless-stopped` to all eight services is a few minutes'
work and should just be done.

---

## 7. Audit log durability

The audit trail is appended to `audit-logs/audit.log`, a bind-mounted file on one server. It is
deletable by anyone with host access and has no retention guarantee — which is most of the point of an
audit log in a regulated environment. Forward it to the client's SIEM or WORM storage.

---

## 8. User store is SQLite

[`auth-service/main.py:29`](auth-service/main.py#L29) points at `/app/users.db`, a single SQLite file
(env override `USERS_DB`). Fine for a pilot on one server. It rules out running more than one
`auth-service` instance, and there is no backup or failover story beyond copying the file. Managed
Postgres plus federation to the client's identity provider is the production shape — at which point
most of this service's user management stops being yours to own.

Also note: `scopes` is stored as a JSON-array string in a single column
([`auth-service/main.py:79`](auth-service/main.py#L79)), not a normalized join table. Works fine for
per-user assignment; becomes awkward once org-level scopes (#12) need to be unioned in at login. Worth
normalizing (#13) at the same time as the Postgres move rather than twice.

---

## 9. Secrets in a `.env` file

The LLM key sits in a `chmod 600` file on the server. Reasonable for a pilot, and better than being in
git. A bank will generally expect Vault / Key Vault injection instead. Worth raising early because it
often depends on access their platform team has to grant.

---

## 10. CORS allows a development origin

[`kong.yml:95`](kong.yml#L95) permits `http://localhost:4200`. Harmless in the delivered setup — the
frontend and API share an origin, so CORS is never exercised — but it reads as leftover development
config to a reviewer. Remove it or set it to the real hostname.

---

## 11. No upload or page-count guard

[`frontend/nginx.conf`](frontend/nginx.conf#L7) caps uploads at 50 MB, but nothing bounds page count.
`find_missing_pages()` in
[`document-reviewer/main.py`](document-reviewer/main.py#L44-L56) grows quadratically with document
length — measured at 0.2s for 25 pages, 3.9s for 100, and 16.9s for 200. A ~400-page PDF would exceed
Kong's 60-second proxy timeout and surface as a generic gateway error with no indication of the cause.
Not a security issue and not reachable with normal documents; worth a page-count check with a clear
error message.

---

## 12. No organization/tenant concept

**What it is.** The admin portal only grants services to individual users — one at a time, via
`PUT /api/auth/users/{username}/scopes`
([`admin-users.component.ts:45-60`](frontend/src/app/admin/users/admin-users.component.ts#L45)) and
the scope checklist on user creation
([`admin-add-user.component.ts:25-29`](frontend/src/app/admin/add-user/admin-add-user.component.ts#L25)).
There is no organization/tenant entity anywhere in the backend (confirmed by grep across `.py`/`.ts`),
and `PRODUCT.md` still lists multi-tenant hosting as undecided.

**Wanted for production.** Grant a service to an organization once, and every user registered under
that org automatically inherits it, instead of an admin re-checking a box per user.

**Design direction (see chat for full discussion).** Add an `organizations` table and an `org_scopes`
join table; add `org_id` on `users`. Compute effective scopes as the union of the user's own scopes
and their org's scopes at token-issuance time (login), not via a live DB check per request — this
keeps every service's stateless "decode JWT, check scope" model (#1's design) intact. Trade-off to
flag explicitly: because scopes are baked into the JWT, an admin adding a service to an org won't reach
already-logged-in members of that org until their token is refreshed or they re-login — decide token
TTL with that in mind. Should land after #13 (normalizing the scopes column) so org-scope unions aren't
built on top of a JSON blob.

---

## 13. No migration tooling

**What it is.** `init_db()` in
[`auth-service/main.py:67-89`](auth-service/main.py#L67-L89) runs a raw
`CREATE TABLE IF NOT EXISTS` on every boot. There's no migrations directory or tool (Alembic or
equivalent) anywhere in the repo, so schema changes have no upgrade path beyond hand-editing a running
database.

**Why it matters now specifically.** Both the Postgres move (#8) and org-level entitlements (#12)
require schema changes (new tables, normalizing the `scopes` column). Doing that without migration
tooling means either a manual one-off script or risking data loss on a client's existing database.

**Recommended fix.** Introduce Alembic before #8/#12 land, so those two land as tracked migrations
rather than ad-hoc schema edits.

---

## Suggested sequencing

**Before any client handover:** #3 (1 hour), #6 (15 min), #10 (5 min) — trivial and remove obvious
review findings. #1 and #2 together (~1 day) — the substantive one, and currently the most urgent
since #2 has regressed to a hardcoded shared secret on this branch. #4 and #5 need client decisions,
so raise them now rather than on installation day.

**Before production sign-off:** #7 and #9, both depending on the client's platform. #13 (migrations)
before attempting #8 or #12.

**Production feature work:** #8 (Postgres) → #12 (org-level entitlements), in that order, both behind
#13.

**Pilot-acceptable, plan for later:** #11.
