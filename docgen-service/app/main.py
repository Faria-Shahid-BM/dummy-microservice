"""CAD Workbench application factory.

Single origin: /api/* is the JSON API; everything else serves the Angular
SPA build (deep links fall back to index.html). Run single-worker — see
ARCHITECTURE.md "Jobs + SSE" for the in-memory event buffer constraint.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.core.config import settings
from app.core.db import Base, engine

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("app")


def _seed_default_profile() -> None:
    """Ensure the read-only Default profile (the factory baseline) exists."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.models import Profile

    with session_scope() as db:
        exists = db.execute(
            select(Profile).where(Profile.is_default.is_(True))
        ).scalar_one_or_none()
        if exists is None:
            db.add(
                Profile(
                    name="Default",
                    is_default=True,
                    description=(
                        "Factory baseline configuration. Read-only — create a "
                        "profile to customise prompts, models and settings."
                    ),
                )
            )
        ws = db.execute(
            select(Profile).where(Profile.name == "Workspace")
            ).scalar_one_or_none()
        if ws is None:
            db.add(
                Profile(
                    name="Workspace", is_default=False,
                    description="Working profile for docgen (Kong)."
                )
            )
            log.info("Seeded the Workspace profile")
            log.info("Seeded the Default profile")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Idempotent for dev/first boot; production migrations run `alembic upgrade
    # head` before start (Docker CMD) — create_all never alters existing tables.
    Base.metadata.create_all(engine)
    _seed_default_profile()
    from app.jobs.runner import runner

    runner.start()
    log.info("%s started (db=%s)", settings.app_name, settings.database_url.split("://")[0])
    yield
    runner.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)

    
    # --- upload rejections are client errors, not 500s ----------------------
    from app.storage import UploadError

    @app.exception_handler(UploadError)
    async def upload_error(request: Request, exc: UploadError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    # --- routers ------------------------------------------------------------
    from app import audit, notify, profiles
    from app.control import approvals, profile_config, templates
    from app.jobs import router as jobs_router
    from app.modules import collateral, docgen, document_reviewer, insurance, policy_qa, valuation

    app.include_router(audit.router)
    app.include_router(notify.router)
    app.include_router(profiles.router)
    app.include_router(profile_config.router)
    app.include_router(templates.router)
    app.include_router(approvals.router)
    app.include_router(jobs_router.router)
    app.include_router(docgen.router)
    app.include_router(document_reviewer.router)
    app.include_router(collateral.router)
    app.include_router(valuation.router)
    app.include_router(insurance.router)
    app.include_router(policy_qa.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "app": settings.app_name}

    # --- SPA ------------------------------------------------------------------
    dist = settings.frontend_dist

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        candidate = (dist / full_path).resolve() if full_path else None
        if (
            candidate
            and candidate.is_file()
            and candidate.is_relative_to(dist.resolve())
        ):
            return FileResponse(candidate)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            {"detail": "Frontend build not found — run the Angular build (see README)"},
            status_code=503,
        )

    return app


app = create_app()
