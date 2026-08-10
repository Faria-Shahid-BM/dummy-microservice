import os
import re
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import JSON, DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

app = FastAPI()
os.makedirs("logs", exist_ok=True)

ATTACHMENTS_DIR = "logs/attachments"
os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
_ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_engine = create_engine("sqlite:///logs/audit.db", connect_args={"check_same_thread": False})
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


Base.metadata.create_all(_engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class AuditEvent(BaseModel):
    user_id: str
    service: str
    action: str
    resource: str | None = None
    metadata: dict | None = None


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


def _serialize(row: AuditRow) -> dict:
    sections, attachments = _build_sections(row.detail)
    return {
        "id": row.id,
        "timestamp": row.timestamp.isoformat(),
        "user_id": row.user_id,
        "service": row.service,
        "action": row.action,
        "resource": row.resource,
        "attachments": attachments,
        "sections": sections,
    }


@app.post("/audit")
def log_event(event: AuditEvent, db: Session = Depends(get_db)):
    row = AuditRow(
        user_id=event.user_id,
        service=event.service,
        action=event.action,
        resource=event.resource,
        detail=event.metadata,
    )
    db.add(row)
    db.commit()
    return {"status": "logged"}


@app.get("/audit")
def get_logs(db: Session = Depends(get_db)):
    rows = db.execute(select(AuditRow).order_by(AuditRow.id.asc())).scalars().all()
    return [_serialize(r) for r in rows]


@app.get("/audit/{entry_id}")
def get_entry(entry_id: int, db: Session = Depends(get_db)):
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
