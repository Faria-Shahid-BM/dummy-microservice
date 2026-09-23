"""config-service — the one place every other service's settings live.

Like auth-service and audit-service, this is shared infrastructure rather than
a reviewer: the review services own documents and results, this owns *how* they
run. Today that means which model fills each role; the registry below is shaped
so a prompt or a numeric setting is a registration, not a new mechanism.

Why a service rather than a shared module. Each service keeps its own database
on purpose (see PRODUCT.md — a deployment is self-contained), so a shared
module would still have produced one overrides table per service and no single
answer to "what is this deployment configured to do". Putting it behind one
service is the same trade already made for auth and audit.

Resolution, most specific first::

    this user's override  ->  the deployment default  ->  (nothing: 500)

Deleting an override IS "reset to default"; no copy of the default is stored,
so changing a default in compose still moves every user who never chose their
own. Defaults live here, in this service's environment, so there is exactly one
place to read them from — see DEFAULT_ENV on each setting.

Callers are the review services themselves, via config_client.py. That client
falls back to its own environment when this service is unreachable, so config
being down degrades to "everyone gets the deployment default" rather than
stopping reviews.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import outbox
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from security import get_raw_token, require_any_token

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))

# A model id is a provider path like "google/gemini-2.5-flash". There's no
# closed list worth validating against — the catalogue changes constantly and a
# deployment may point at a gateway other than OpenRouter — so the limit is
# only what catches a paste accident.
MAX_VALUE = 500


# --------------------------------------------------------------------------
# registry: what exists, and where its default comes from
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Setting:
    """One configurable setting.

    `kind` is the vocabulary docgen's profile_config already uses (model |
    prompt | number) so the two read as one idea; only "model" is registered
    today. `default_env` is read at request time rather than at import, so a
    restart with a changed environment reports the new default immediately.
    """

    key: str
    label: str
    kind: str
    default_env: str
    description: str = ""

    def default(self) -> str:
        return os.environ.get(self.default_env, "")


@dataclass(frozen=True)
class ServiceSettings:
    """One reviewer's settings, and the JWT scope that may see or change them."""

    scope: str
    label: str
    description: str
    settings: tuple[Setting, ...]


def _model(role: str, label: str, env: str, description: str) -> Setting:
    return Setting(f"model.{role}", label, "model", env, description)


_EXTRACTION = "Reads the fields out of each uploaded document."
_VISION = "Transcribes scanned pages that carry no text layer."

REGISTRY: tuple[ServiceSettings, ...] = (
    ServiceSettings(
        "collateral", "Collateral Review",
        "Cross-checks a legal opinion against a property document.",
        (
            _model("extraction", "Extraction model", "COLLATERAL_MODEL_EXTRACTION", _EXTRACTION),
            _model("vision", "Vision / OCR model", "COLLATERAL_MODEL_VISION", _VISION),
        ),
    ),
    ServiceSettings(
        "valuation", "Valuation Review",
        "Checks a valuation report against the approved-valuer panel and policy rules.",
        (
            _model("extraction", "Extraction model", "VALUATION_MODEL_EXTRACTION", _EXTRACTION),
            _model("vision", "Vision / OCR model", "VALUATION_MODEL_VISION", _VISION),
        ),
    ),
    ServiceSettings(
        "insurance", "Insurance Review",
        "Grades an insurance policy against the bank policy and rules.",
        (
            _model("extraction", "Extraction model", "INSURANCE_MODEL_EXTRACTION", _EXTRACTION),
            _model("vision", "Vision / OCR model", "INSURANCE_MODEL_VISION", _VISION),
        ),
    ),
    ServiceSettings(
        "policy_qa", "Policy Q&A",
        "Answers policy questions over the documents you've ingested.",
        (
            _model("chat", "Chat model", "POLICYQA_MODEL_CHAT",
                   "Writes the answer from the retrieved passages."),
            _model("embedding", "Embedding model", "POLICYQA_MODEL_EMBEDDING",
                   "Indexes documents and matches a question to passages. Changing "
                   "this makes existing indexes unreadable — re-ingest after changing it."),
            _model("vision", "Vision / OCR model", "POLICYQA_MODEL_VISION", _VISION),
        ),
    ),
    ServiceSettings(
        "docgen", "Document Generator",
        "Generates documents from a template and a case. A profile that pins a "
        "model overrides your choice for work in that profile — see that "
        "profile's own configuration screen.",
        (
            _model("extraction", "Extraction model", "DOCGEN_MODEL_EXTRACTION", _EXTRACTION),
            _model("vision", "Vision / OCR model", "DOCGEN_MODEL_VISION", _VISION),
            _model("selection", "Selection model", "DOCGEN_MODEL_SELECTION",
                   "Picks which template fits a case."),
            _model("fill", "Fill model", "DOCGEN_MODEL_FILL",
                   "Writes the values into the chosen template."),
            _model("analysis", "Analysis model", "DOCGEN_MODEL_ANALYSIS",
                   "Describes a template and analyses a generated document."),
        ),
    ),
)

BY_SCOPE: dict[str, ServiceSettings] = {s.scope: s for s in REGISTRY}


# --------------------------------------------------------------------------
# the provider's catalogue: what you can actually pick
# --------------------------------------------------------------------------
# Offered as suggestions and as an advisory check, never as a whitelist. Three
# reasons it cannot be authoritative:
#
#   1. It lists no embedding models at all (444 entries, zero), so policy Q&A's
#      shipped default `openai/text-embedding-3-large` is absent from it.
#   2. Ids differ in case from what works — this deployment's default is
#      `google/gemini-2.5-Flash`, the catalogue says `google/gemini-2.5-flash`
#      — so matching is case-insensitive and a miss is only ever a warning.
#   3. LLM_BASE_URL may point somewhere other than OpenRouter, whose catalogue
#      this is. A gateway with its own model names is a valid deployment.
#
# So a typed id that isn't here still saves. The UI says "not in the
# catalogue", which is the honest claim; "does not exist" would not be.

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
# The catalogue changes on the order of days; refetching per page load would
# put a third-party outage in the way of a settings screen for no benefit.
CATALOGUE_TTL_SECONDS = 3600.0
CATALOGUE_TIMEOUT = 8.0

_catalogue_lock = threading.Lock()
_catalogue: list[dict] = []
_catalogue_fetched_at = 0.0
_catalogue_error = ""


def _fetch_catalogue() -> tuple[list[dict], str]:
    try:
        response = httpx.get(f"{LLM_BASE_URL}/models", timeout=CATALOGUE_TIMEOUT)
        response.raise_for_status()
        raw = response.json().get("data") or []
    except (httpx.HTTPError, ValueError) as exc:
        return [], f"{type(exc).__name__}: {exc}"

    models = []
    for entry in raw:
        model_id = entry.get("id")
        if not isinstance(model_id, str):
            continue
        architecture = entry.get("architecture") or {}
        models.append({
            "id": model_id,
            "name": entry.get("name") or model_id,
            "context_length": entry.get("context_length"),
            # Whether it can read an image at all — the one capability that
            # decides if a model can fill the vision/OCR role.
            "vision": "image" in (architecture.get("input_modalities") or []),
        })
    models.sort(key=lambda m: m["id"])
    return models, ""


def catalogue() -> tuple[list[dict], str]:
    """The provider's model list, cached. Never raises: on failure the caller
    gets an empty list and a reason, and the settings screen falls back to a
    plain text box."""
    global _catalogue, _catalogue_fetched_at, _catalogue_error
    with _catalogue_lock:
        stale = (time.monotonic() - _catalogue_fetched_at) > CATALOGUE_TTL_SECONDS
        if stale or (not _catalogue and not _catalogue_error):
            models, error = _fetch_catalogue()
            # Keep the last good list on a failed refresh: a stale suggestion
            # list is worth more than none.
            if models or not _catalogue:
                _catalogue = models
            _catalogue_error = error
            _catalogue_fetched_at = time.monotonic()
        return _catalogue, _catalogue_error


# --------------------------------------------------------------------------
# storage: overrides only
# --------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class Override(Base):
    __tablename__ = "config_overrides"

    #: JWT `sub` — an override belongs to the person who set it.
    user_sub: Mapped[str] = mapped_column(String(255), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(MAX_VALUE))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


_engine = create_engine(
    f"sqlite:///{DATA_DIR / 'config.db'}", connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)

# Audit outbox (see outbox.py): the override write and "this must be audited"
# commit together, so a model can never change without a record of it. Worth
# the row — a change here decides what every later review of this user's runs
# on, which is the first thing to check when two runs of the same documents
# disagree.
_OUTBOX = outbox.outbox_table(Base.metadata)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(_engine)
    asyncio.create_task(outbox.run_relay(SessionLocal, _OUTBOX))
    yield


app = FastAPI(lifespan=lifespan, title="config-service")
router = APIRouter(prefix="/api/config", tags=["config"])


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
# Every caller is already past Kong's signature check; this re-verifies the
# token itself (same defence-in-depth as audit-service) and then gates each
# scope's settings on holding that scope. A reviewer you can't use is a
# reviewer whose models you can't see or change.

def _user(claims: dict = Depends(require_any_token)) -> dict:
    if not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Token has no subject")
    return claims


def _entitled(claims: dict, scope: str) -> bool:
    return scope in (claims.get("scopes") or [])


def _service_or_404(scope: str) -> ServiceSettings:
    service = BY_SCOPE.get(scope)
    if service is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown scope '{scope}'; configured services are: "
                   f"{', '.join(sorted(BY_SCOPE))}",
        )
    return service


def _require_entitled(scope: str, claims: dict) -> ServiceSettings:
    service = _service_or_404(scope)
    if not _entitled(claims, scope):
        # 403 rather than 404: the scope exists, this user simply can't use it.
        raise HTTPException(status_code=403, detail=f"Missing required scope: {scope}")
    return service


def _overrides(db: Session, user_sub: str, scope: str | None = None) -> dict[tuple[str, str], Override]:
    stmt = select(Override).where(Override.user_sub == user_sub)
    if scope is not None:
        stmt = stmt.where(Override.scope == scope)
    return {(row.scope, row.key): row for row in db.execute(stmt).scalars()}


def _payload(setting: Setting, row: Override | None) -> dict:
    return {
        "key": setting.key,
        "label": setting.label,
        "kind": setting.kind,
        "description": setting.description,
        "default": setting.default(),
        "value": row.value if row else setting.default(),
        "is_overridden": row is not None,
        "updated_at": row.updated_at.isoformat() if row else None,
    }


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@router.get("")
def overview(claims: dict = Depends(_user), db: Session = Depends(get_db)) -> dict:
    """Everything this user may configure, grouped by service.

    Scopes they don't hold are left out entirely rather than shown locked —
    the sidebar already hides those reviewers, and a settings page listing
    tools you can't open only raises questions.
    """
    rows = _overrides(db, claims["sub"])
    return {
        "services": [
            {
                "scope": service.scope,
                "label": service.label,
                "description": service.description,
                "settings": [
                    _payload(s, rows.get((service.scope, s.key))) for s in service.settings
                ],
                "override_count": sum(
                    1 for s in service.settings if (service.scope, s.key) in rows
                ),
            }
            for service in REGISTRY
            if _entitled(claims, service.scope)
        ]
    }


@router.get("/catalogue")
def model_catalogue(_claims: dict = Depends(_user)) -> dict:
    """Models the configured provider advertises, for suggestions and an
    advisory spelling check. `available: false` means pick blind — a plain
    text box, no warnings — rather than block on a list we couldn't fetch."""
    models, error = catalogue()
    return {"available": bool(models), "source": LLM_BASE_URL, "error": error, "models": models}


@router.get("/effective/{scope}")
def effective(
    scope: str, claims: dict = Depends(_user), db: Session = Depends(get_db)
) -> dict:
    """What a service should actually run for this caller, keyed by role.

    This is the call config_client.py makes at the start of a review. It is
    deliberately the same resolution the overview reports, so the page can
    never disagree with what runs.
    """
    service = _require_entitled(scope, claims)
    rows = _overrides(db, claims["sub"], scope)
    models: dict[str, str] = {}
    for setting in service.settings:
        if setting.kind != "model":
            continue
        row = rows.get((scope, setting.key))
        value = row.value if row else setting.default()
        if value:
            models[setting.key.removeprefix("model.")] = value
    return {"scope": scope, "models": models}


class ConfigValue(BaseModel):
    value: str = Field(min_length=1, max_length=MAX_VALUE)


@router.put("/{scope}/{key:path}")
def set_override(
    scope: str,
    key: str,
    body: ConfigValue,
    claims: dict = Depends(_user),
    token: str | None = Depends(get_raw_token),
    db: Session = Depends(get_db),
) -> dict:
    service = _require_entitled(scope, claims)
    setting = next((s for s in service.settings if s.key == key), None)
    if setting is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown setting '{key}' for '{scope}'; it has: "
                   f"{', '.join(s.key for s in service.settings)}",
        )
    value = body.value.strip()
    if not value:
        raise HTTPException(status_code=422, detail="Value cannot be blank")

    row = db.get(Override, {"user_sub": claims["sub"], "scope": scope, "key": key})
    previous = row.value if row else setting.default()
    if row is None:
        row = Override(user_sub=claims["sub"], scope=scope, key=key, value=value)
        db.add(row)
    else:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    outbox.enqueue(
        db, _OUTBOX, service="config-service", token=token, action="config.set",
        resource=f"{scope}:{key}",
        detail={"input": {"scope": scope, "key": key, "from": previous, "to": value}},
    )
    db.commit()
    return _payload(setting, row)


@router.delete("/{scope}/{key:path}")
def reset_override(
    scope: str,
    key: str,
    claims: dict = Depends(_user),
    token: str | None = Depends(get_raw_token),
    db: Session = Depends(get_db),
) -> dict:
    service = _require_entitled(scope, claims)
    setting = next((s for s in service.settings if s.key == key), None)
    if setting is None:
        raise HTTPException(status_code=404, detail=f"Unknown setting '{key}' for '{scope}'")
    row = db.get(Override, {"user_sub": claims["sub"], "scope": scope, "key": key})
    if row is not None:
        outbox.enqueue(
            db, _OUTBOX, service="config-service", token=token, action="config.reset",
            resource=f"{scope}:{key}",
            detail={"input": {"scope": scope, "key": key, "from": row.value,
                              "to": setting.default()}},
        )
        db.delete(row)
        db.commit()
    return _payload(setting, None)


app.include_router(router)
