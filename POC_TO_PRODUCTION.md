# POC → Production: Gap Assessment

**Internal document — not for client distribution.**

The system works end to end and is a good POC. This is the list of things that are POC-level by
design and should be decided on *before* it is handed to a client, so nothing is discovered during
their security review.

Nothing here is a defect in what was built. These are the deliberate shortcuts that come with a
proof of concept, written down so they can be prioritised rather than inherited.

---

## Summary

| # | Gap | Severity | Blocks client handover? | Rough effort |
|---|---|---|---|---|
| 1 | Services trust JWT claims without verifying the signature | **High** | Yes | ~1 day |
| 2 | ~~JWT signing secret hardcoded, HS256 shared secret~~ | ~~High~~ | **RESOLVED** — now RS256 | done |
| 3 | Demo accounts `alice` / `bob` seeded automatically | **High** | Yes | ~1 hour |
| 4 | No TLS — HTTP only | **High** | Yes (or LB terminates) | 0 if upstream LB, ~0.5 day if on-box |
| 5 | Document text sent to an external LLM | **High** | Yes — compliance | Config + client sign-off |
| 6 | No restart policy — stack dies on reboot | Medium | Should fix | ~15 min |
| 7 | Audit log is a plain file on one server | Medium | Should fix | Depends on SIEM |
| 8 | User store is SQLite on local disk | Medium | Acceptable for pilot | ~2 days for Postgres |
| 9 | Secrets live in a `.env` file on disk | Medium | Acceptable for pilot | Depends on Vault access |
| 10 | CORS allows `http://localhost:4200` | Low | No | ~5 min |
| 11 | No upload size or page-count guard on document compare | Low | No | ~1 hour |

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

## 2. JWT signing secret hardcoded — **RESOLVED, now RS256**

**What it was.** `supersecretkey` was hardcoded in both `auth-service/main.py` and `kong.yml`, and the
two had to match. Both files are tracked in git, so a `git pull` on a deployed server silently
restored the known placeholder.

**What was done.** Moved to RS256 asymmetric signing:

| Change | File |
|---|---|
| Signs with a private key read from `JWT_PRIVATE_KEY_PATH`; refuses to start if unreadable | [`auth-service/main.py`](auth-service/main.py#L14-L38) |
| Consumer now uses `algorithm: RS256` + inline `rsa_public_key` — no secret in the file | [`kong.yml`](kong.yml#L107-L129) |
| Keypair generator; writes `keys/` and patches `kong.yml` to match | [`scripts/generate_jwt_keys.py`](scripts/generate_jwt_keys.py) |
| `keys/` mounted read-only into `auth-service` only | [`docker-compose.yml`](docker-compose.yml#L51-L66) |
| `keys/` ignored so the private key can never be committed | [`.gitignore`](.gitignore) |
| `pyjwt` → `pyjwt[crypto]`, required for RS256 | [`requirements.txt`](requirements.txt) |

**Verified end to end** against the running stack — 11 checks, all passing: login returns a token with
`alg: RS256`; that token reaches an entitled service; a token signed with the **old** `supersecretkey` is
rejected (401); a token signed with a **different RSA key** is rejected (401), proving Kong verifies
against this specific public key rather than accepting any RS256 token; an unsigned `alg: none` token
is rejected; entitlement still holds (a viewer gets 403 on a service they are not entitled to, 200 on
one they are).

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

**Remaining work.** The private key is a file on disk mounted into the container. For production that
should come from Vault / Key Vault instead — see #9. Key rotation is manual
(`--force` + `--force-recreate`) and invalidates all issued tokens.

**Note on #1.** RS256 is also what makes #1 cheap. Under HS256, a service verifying signatures would
need the *signing* secret — one secret in one place becomes the same secret in six. With RS256 each
service needs only the public key and holds nothing sensitive. #1 is now a small, self-contained change.

---

## 3. Demo accounts seeded automatically

**What it is.** [`auth-service/main.py:59-62`](auth-service/main.py#L59-L62) seeds `alice`/`alicepw`
(admin) and `bob`/`bobpw` (viewer) whenever the user table is empty — so on every fresh install, and
again if the database file is ever lost.

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

`auth-data/auth.db` is a single SQLite file. Fine for a pilot on one server. It rules out running more
than one `auth-service` instance, and there is no backup or failover story beyond copying the file
(guide §6.4). Managed Postgres plus federation to the client's identity provider is the production
shape — at which point most of this service's user management stops being yours to own.

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

## Suggested sequencing

**Before any client handover:** #3 (1 hour), #6 (15 min), #10 (5 min) — trivial and remove obvious
review findings. #1 and #2 together (~1 day) — the substantive one. #4 and #5 need client decisions,
so raise them now rather than on installation day.

**Before production sign-off:** #7 and #9, both depending on the client's platform.

**Pilot-acceptable, plan for later:** #8 and #11.
