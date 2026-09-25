import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import (JSON, DateTime, Integer, String, create_engine, func,
                        inspect, select, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from security import require_any_token, require_any_token_allow_expired, require_scope

app = FastAPI()

DATA_DIR = Path(os.environ.get("DATA_DIR", "logs"))
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR}/audit.db")
DATA_DIR.mkdir(parents=True, exist_ok=True)

ATTACHMENTS_DIR = DATA_DIR / "attachments"
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
_ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass


class AuditRow(Base):
    __tablename__ = "audit_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    user_id: Mapped[str] = mapped_column(String(255))
    service: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    resource: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Exactly what the producer sent via audit_client.py's `metadata` — the
    # complete, untouched record. Named `detail` (not `metadata`) because
    # that name collides with SQLAlchemy's own Base.metadata.
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Below: promoted from docgen-service's own local audit trail when it was
    # folded into this one (see POC_TO_PRODUCTION.md #14) — every producer can
    # now carry them, not just docgen.
    subject_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A producer-generated id (outbox.py's row id), so at-least-once delivery
    # from an outbox relay can be de-duplicated safely (see log_event).
    # Nullable: a hand-sent event with no outbox behind it just skips dedupe.
    event_id: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    # What this event's LLM work cost. Real columns rather than a reach into
    # `detail`, so the totals survive a producer changing what it audits, and
    # so they can be summed in SQL instead of by parsing every row's JSON.
    # NULL on the events that spend nothing (a login, an upload).
    #
    # `usage_model` is recorded per event, not looked up later: a user's
    # configured model changes, and the question worth answering about a past
    # run is what it actually ran on.
    usage_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    usage_prompt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_completion: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_total: Mapped[int | None] = mapped_column(Integer, nullable=True)


Base.metadata.create_all(_engine)

# create_all() only creates missing TABLES, never new columns on one that
# already exists — same cheap stand-in for real migration tooling as
# case_store.py's _ADDED_COLUMNS (see POC_TO_PRODUCTION.md #13).
_ADDED_COLUMNS = (
    ("subject_type", "VARCHAR(40)"),
    ("subject_id", "VARCHAR(64)"),
    ("profile_id", "VARCHAR(64)"),
    ("event_id", "VARCHAR(32)"),
    ("usage_model", "VARCHAR(128)"),
    ("usage_prompt", "INTEGER"),
    ("usage_completion", "INTEGER"),
    ("usage_total", "INTEGER"),
)
_existing_columns = {c["name"] for c in inspect(_engine).get_columns("audit_entries")}
with _engine.begin() as _conn:
    for _name, _ddl_type in _ADDED_COLUMNS:
        if _name not in _existing_columns:
            _conn.execute(text(f"ALTER TABLE audit_entries ADD COLUMN {_name} {_ddl_type}"))
    # SQLite allows any number of NULLs in a unique index — only two equal,
    # non-null event_ids collide — which is exactly "dedupe retried
    # deliveries, don't require one on every row".
    _conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_audit_entries_event_id ON audit_entries (event_id)"
    ))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class UsageReport(BaseModel):
    """What one audited piece of LLM work cost.

    Declared rather than dug out of `metadata`, for the same reason
    `to_audit_output` exists: a producer's result shape is its own business and
    changes freely, but the admin usage view reads this and needs it to keep
    meaning the same thing.
    """
    model: str | None = None
    prompt: int = 0
    completion: int = 0
    total: int = 0


class AuditEvent(BaseModel):
    # No user_id here: it used to be a plain string the producer sent, so any
    # process reachable on the internal network could write an entry
    # claiming to be anyone. It's derived instead from the caller's verified
    # bearer token (see log_event) — the one field an audit trail can't
    # afford to take on trust.
    service: str
    action: str
    resource: str | None = None
    metadata: dict | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    profile_id: str | None = None
    # outbox.py's row id, when this arrived via a service's outbox relay
    # rather than a one-off direct call — lets log_event de-duplicate a
    # retried delivery instead of logging the same event twice.
    event_id: str | None = None
    # Omitted by events that spend no tokens.
    usage: UsageReport | None = None


# ── Display shaping ──────────────────────────────────────────────────────
# audit-service has no idea what "compare" or "chat" means, and it shouldn't
# need to: each producer already builds the {"input": ..., "output": ...}
# dict it sends via audit_client.py, so THAT dict is its DTO — it only
# includes a field here if that field is worth an audit reader's time (e.g.
# doc_rev-service strips its own noisy `segments`/`context` before sending;
# case_store.py sends `input: {"attachments": [...]}` with nothing else once
# the file itself is already shown as a link). All that's left here is a
# generic, domain-free rule: turn `input`/`output` into a section if there's
# non-attachment content in it. Computed fresh on every read, so `detail` in
# the DB always stays exactly what was sent, whatever the display rules end
# up being when someone changes them.


def _humanize(key: str) -> str:
    spaced = key.replace("_", " ")
    return spaced[:1].upper() + spaced[1:] if spaced else spaced


def _to_display_node(value) -> dict:
    if value is None or value == "":
        return {"type": "text", "value": "—"}
    if isinstance(value, bool):
        return {"type": "text", "value": "Yes" if value else "No"}
    if isinstance(value, list):
        if not value:
            return {"type": "text", "value": "—"}
        if all(isinstance(v, dict) for v in value):
            columns: list[str] = []
            seen: set[str] = set()
            for row in value:
                for k in row:
                    if k not in seen:
                        seen.add(k)
                        columns.append(k)
            return {
                "type": "table",
                "columns": [_humanize(c) for c in columns],
                "rows": [[_to_display_node(row.get(c)) for c in columns] for row in value],
            }
        return {"type": "list", "items": [_to_display_node(v) for v in value]}
    if isinstance(value, dict):
        if not value:
            return {"type": "text", "value": "—"}
        return {
            "type": "fields",
            "rows": [{"label": _humanize(k), "value": _to_display_node(v)} for k, v in value.items()],
        }
    return {"type": "text", "value": str(value)}


def _build_sections(detail: dict | None):
    if not detail:
        return [], []
    input_ = detail.get("input") or {}
    output_ = detail.get("output") or {}
    attachments = [a for a in (input_.get("attachments") or []) if isinstance(a, dict)]

    sections = []
    input_fields = {k: v for k, v in input_.items() if k != "attachments"}
    if input_fields:
        sections.append({"title": "Input", "content": _to_display_node(input_fields)})
    if output_:
        sections.append({"title": "Output", "content": _to_display_node(output_)})
    return sections, attachments


def _iso_utc(value: datetime | None) -> str | None:
    """ISO-8601 with an explicit UTC offset.

    SQLite stores no timezone information even on a DateTime(timezone=True)
    column, so a value read back is naive despite having been written as UTC.
    Serialised without an offset, a browser reads it as local time and every
    timestamp silently shifts by the viewer's offset.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _serialize(row: AuditRow) -> dict:
    sections, attachments = _build_sections(row.detail)
    return {
        "id": row.id,
        "timestamp": _iso_utc(row.timestamp),
        "user_id": row.user_id,
        "service": row.service,
        "action": row.action,
        "resource": row.resource,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "profile_id": row.profile_id,
        "attachments": attachments,
        "sections": sections,
    }


@app.post("/audit")
def log_event(
    event: AuditEvent,
    db: Session = Depends(get_db),
    # Delivery can legitimately arrive after the token that authorized it has
    # expired (a service's outbox relay retries until audit-service is
    # reachable again — see outbox.py) — signature+issuer are still verified,
    # only the expiry check is skipped.
    token: dict = Depends(require_any_token_allow_expired),
):
    sub = token.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token has no subject")
    if event.event_id is not None:
        existing = db.execute(
            select(AuditRow).where(AuditRow.event_id == event.event_id)
        ).scalar_one_or_none()
        if existing is not None:
            return {"status": "already_logged", "id": existing.id}
    row = AuditRow(
        user_id=sub,
        service=event.service,
        action=event.action,
        resource=event.resource,
        detail=event.metadata,
        subject_type=event.subject_type,
        subject_id=event.subject_id,
        profile_id=event.profile_id,
        event_id=event.event_id,
        usage_model=event.usage.model if event.usage else None,
        usage_prompt=event.usage.prompt if event.usage else None,
        usage_completion=event.usage.completion if event.usage else None,
        usage_total=event.usage.total if event.usage else None,
    )
    db.add(row)
    db.commit()
    return {"status": "logged", "id": row.id}


# Admin-only, and enforced here rather than by hiding the route in the
# frontend: this reports every user's spend, so a client-side guard would be
# no protection at all against a direct call.
@app.get("/audit/usage")
def get_usage(
    db: Session = Depends(get_db),
    _admin: dict = Depends(require_scope("admin")),
):
    """Token spend per user per service, and the models each of them ran on.

    Summed in SQL over the rows that carry usage; a service that doesn't report
    any is simply absent rather than reported as zero, which would read as
    "free" instead of "not measured yet".
    """
    rows = db.execute(
        select(
            AuditRow.user_id,
            AuditRow.service,
            func.count().label("runs"),
            func.sum(AuditRow.usage_prompt).label("prompt"),
            func.sum(AuditRow.usage_completion).label("completion"),
            func.sum(AuditRow.usage_total).label("total"),
            func.max(AuditRow.timestamp).label("last_run"),
        )
        .where(AuditRow.usage_total.isnot(None))
        .group_by(AuditRow.user_id, AuditRow.service)
        .order_by(func.sum(AuditRow.usage_total).desc())
    ).all()

    # Which models each user actually ran, per service — distinct from whatever
    # they have configured now, which config-service reports separately.
    model_rows = db.execute(
        select(AuditRow.user_id, AuditRow.service, AuditRow.usage_model)
        .where(AuditRow.usage_model.isnot(None))
        .distinct()
    ).all()
    models: dict[str, list[str]] = {}
    for user_id, service, model in model_rows:
        models.setdefault(f"{user_id}\x00{service}", []).append(model)

    return {
        "by_user_service": [
            {
                "user_id": r.user_id,
                "service": r.service,
                "runs": r.runs,
                "prompt": r.prompt or 0,
                "completion": r.completion or 0,
                "total": r.total or 0,
                "last_run": _iso_utc(r.last_run),
                "models": sorted(models.get(f"{r.user_id}\x00{r.service}", [])),
            }
            for r in rows
        ]
    }


@app.get("/audit")
def get_logs(
    service: str | None = None,
    action: str | None = None,
    profile_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    db: Session = Depends(get_db),
    token: dict = Depends(require_any_token),
):
    # Filters are all optional and additive — an unfiltered call keeps
    # returning exactly what it always has (the admin UI at
    # frontend/src/app/admin/audit relies on that shape).
    q = select(AuditRow).order_by(AuditRow.id.asc())
    if service:
        q = q.where(AuditRow.service == service)
    if action:
        q = q.where(AuditRow.action == action)
    if profile_id:
        q = q.where(AuditRow.profile_id == profile_id)
    if subject_type:
        q = q.where(AuditRow.subject_type == subject_type)
    if subject_id:
        q = q.where(AuditRow.subject_id == subject_id)
    rows = db.execute(q).scalars().all()
    return [_serialize(r) for r in rows]


@app.get("/audit/{entry_id}")
def get_entry(entry_id: int, db: Session = Depends(get_db), token: dict = Depends(require_any_token)):
    row = db.get(AuditRow, entry_id)
    if row is None:
        raise HTTPException(404)
    return _serialize(row)


@app.post("/audit/attachments")
async def upload_attachment(file: UploadFile = File(...)):
    attachment_id = uuid.uuid4().hex
    with open(os.path.join(ATTACHMENTS_DIR, attachment_id), "wb") as f:
        f.write(await file.read())
    return {"attachment_id": attachment_id}


@app.get("/audit/attachments/{attachment_id}")
def get_attachment(attachment_id: str, filename: str | None = None):
    if not _ATTACHMENT_ID_RE.fullmatch(attachment_id):
        raise HTTPException(404)
    path = os.path.join(ATTACHMENTS_DIR, attachment_id)
    if not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, filename=filename or attachment_id, content_disposition_type="inline")
