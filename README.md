**What this is**
All client traffic flows through one gateway — microservices are never directly reachable
One JWT auth check at the gateway covers every microservice — zero auth code in any service
One shared auth service issues tokens — no per-service login logic
One shared audit service records business events — no per-service audit infrastructure
Adding a new microservice requires no changes to auth, audit, or the frontend flow

**Architectur**

Browser (frontend/index.html)
        │
        ▼
   Kong (port 80)
        │
        ├── /api/auth  ──────────────► auth-service     (issues JWT tokens)
        │
        ├── /api/a  ── JWT check ────► service-a        (FastAPI microservice)
        │                                   │
        ├── /api/b  ── JWT check ────► service-b        (FastAPI microservice)
        │                                   │
        └── /api/audit ─────────────► audit-service ◄──┘
                                            │
                                      audit-logs/audit.log


**Containers**
Container	Role	Exposed externally
Kong	API gateway — routing, JWT, CORS	Port 80 (proxy), 8001 (admin)
auth-service	Issues JWT tokens on login	Via Kong at /api/auth
service-a	Dummy microservice A	Via Kong at /api/a (JWT required)
service-b	Dummy microservice B	Via Kong at /api/b (JWT required)
audit-service	Stores audit events	Via Kong at /api/audit (internal write)
