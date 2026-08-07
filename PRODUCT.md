# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary users are demo/evaluation audiences at prospective bank and lending-institution clients — each client runs its own deployment. Within a deployment, two roles use the product: (1) reviewer/analyst staff who run collateral, valuation, insurance, and document-diff checks and use policy Q&A, entitled by scope; (2) an admin who manages users and their scopes via the Admin dashboard.

## Product Purpose

CAD Workbench automates the manual document-review work bank teams do around loan/credit documentation: cross-checking a legal opinion against a property document (collateral review), diffing an original vs. returned copy (document review), reviewing a valuation report against an approved-valuer panel and policy rules, checking an insurance policy against bank policy and rules, and answering policy questions over ingested documents (RAG). Success is a client evaluator seeing accurate, fast automated review that would otherwise take much longer manually, in a demoable end-to-end pipeline.

## Positioning

The differentiator is speed/automation: replacing slow manual document review with fast automated LLM-driven review. Gateway-level and per-service security (JWT verification, RS256, scope enforcement, audit trail) is necessary supporting infrastructure for a credible client demo, but it is not the lead pitch.

## Operating Context

Runs as a Docker Compose stack: Angular frontend (nginx, the only published port) → Kong gateway (JWT signature check, CORS, no host port) → FastAPI business services (collateral, document-diff, valuation, insurance, policyqa) plus auth-service and audit-service. Each demo/client runs its own instance of the full stack. Workflow: a reviewer logs in, sees only the service tiles their scopes grant, uploads document(s) to a review panel, and gets a structured result (comparison table, diff, or chat answer); an admin manages users/scopes from a separate Admin dashboard.

## Capabilities and Constraints

- Five review services: collateral (LLM cross-check, SSE stream available on the backend), document-diff (deterministic, no LLM), valuation (LLM vs. approved-valuer panel + policy rules), insurance (LLM compliance check), policy Q&A (RAG chat with per-user document ingestion).
- RBAC via JWT `scopes` claim: `collateral`, `docdiff`, `valuation`, `insurance`, `policy_qa`, `admin`, plus reserved `doc_gen` (no service wired yet).
- Defense-in-depth: Kong verifies the JWT signature at the edge; each service independently re-verifies the JWT and checks scope (403 if missing).
- Best-effort audit trail: business services emit events to audit-service, appended to a host-mounted log.
- Collateral review's SSE streaming endpoint exists on the backend but isn't yet wired to a live progress UI in the frontend (the panel currently calls the blocking endpoint).
- Undecided: which specific client(s) or verticals beyond banking/lending this will be demoed to; whether multi-tenant hosting is ever needed, or per-client deployment stays the model.

## Brand Commitments

Product name: **CAD Workbench**.

## Evidence on Hand

- Seed demo accounts (`admin`, `carol`, `dave`) with different scope sets, used to demonstrate RBAC live.
- `POC_TO_PRODUCTION.md` documents known POC-stage gaps (demo accounts, no TLS, SQLite user store, etc.) — demo framing should not present these as production-ready without caveats.
- No real client names, testimonials, or case studies exist yet — do not fabricate any for demo material.

## Product Principles

- Automation speed is the headline; every surface should make "this used to take a while, now it takes seconds" legible.
- Defense-in-depth security is real and load-bearing, but it's supporting evidence for trust, not the lead pitch.
- Each client's deployment is self-contained; nothing in the product should assume shared state across clients.
- Scopes gate what a user can even see, not just what they can do — the UI should never show a tile a user isn't entitled to.
- POC-stage shortcuts are known and tracked, not hidden — demo framing should acknowledge pilot status rather than overstate production-readiness.
