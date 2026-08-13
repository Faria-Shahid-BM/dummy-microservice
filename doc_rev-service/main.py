# document-diff-service/main.py
from pathlib import Path
from typing import Callable

import httpx
from engines import extraction, document_diff
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from case_store import Case, init_db, make_case_router, remove_case, write_slot_file

app = FastAPI()
init_db()

# Direct container DNS, bypassing Kong — same pattern as AUDIT_BASE in
# audit_client.py. docgen-service's own auth re-verifies whatever token we
# forward, so there's nothing extra to trust here.
DOCGEN_BASE = "http://docgen-service:8000"

UPLOAD_SLOTS = {"original": {".docx", ".pdf"}, "returned": {".docx", ".pdf"}}
MIN_SLOTS_READY = ["original", "returned"]


class ChangeAuditItem(BaseModel):
    type: str
    before: str
    after: str


class CompareAuditOutput(BaseModel):
    """What's worth an audit reader's time from compare_documents()'s
    result — everything else (the full `segments` redline dump, each
    change's `context` window) is real data the compare UI needs, not
    something an audit trail needs. Declared as real fields rather than a
    key-name blacklist, so it's self-documenting and Pydantic just drops
    whatever isn't listed here."""

    identical: bool
    similarity: float
    summary: dict
    changes: list[ChangeAuditItem]


def _to_audit_output(result: dict) -> dict:
    return CompareAuditOutput.model_validate(result).model_dump()


def _read(slot: str, path: Path) -> str:
    if path.suffix.lower() == ".pdf" and extraction.is_scanned_pdf(path):
        raise ValueError(f"'{slot}' is a scanned PDF; this service needs text documents")
    text = extraction.extract_text(path)
    if not text.strip():
        raise ValueError(f"No text extracted from '{slot}'")
    return text


def _analyze(paths: dict[str, Path], emit: Callable[[str, str], None], user_sub: str) -> dict:
    # user_sub is unused: this comparison depends only on the two uploads.
    return document_diff.compare_documents(_read("original", paths["original"]), _read("returned", paths["returned"]))


def _sync_from_docgen(user_sub: str, token: str | None, db: Session) -> None:
    """Mirror this user's own Document Generator output into their Document
    Reviewer cases, so `original` is always whatever they most recently
    generated — never something they upload by hand.

    `/api/documents/mine` only returns approved documents, so a case whose
    document falls out of that list (rejected, never approved, or approved
    then regenerated without re-approval) is dropped here too — Document
    Reviewer must never show a case for a document that isn't currently
    approved in Document Generator.

    Best-effort: docgen-service being unreachable, or this user's token
    lacking the `docgen` scope, just means the list shows whatever was
    already synced rather than failing the whole page.
    """
    if not token:
        return
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = httpx.get(f"{DOCGEN_BASE}/api/documents/mine", headers=headers, timeout=5.0)
        resp.raise_for_status()
        items = resp.json()["documents"]
    except (httpx.HTTPError, KeyError, ValueError):
        return

    current_keys = {item["logical_key"] for item in items}
    stale = db.execute(
        select(Case).where(
            Case.created_by == user_sub,
            Case.external_key.is_not(None),
            Case.external_key.not_in(current_keys),
            Case.status != "analyzing",
        )
    ).scalars().all()
    for case in stale:
        remove_case(db, case)
    db.commit()

    for item in items:
        case = db.execute(
            select(Case).where(Case.created_by == user_sub, Case.external_key == item["logical_key"])
        ).scalar_one_or_none()
        if case is None:
            label = item["template_name"]
            if item.get("instance_label"):
                label = f"{label} ({item['instance_label']})"
            case = Case(
                name=f"{item['case_name']} — {label}",
                status="new",
                uploads={},
                created_by=user_sub,
                external_key=item["logical_key"],
            )
            db.add(case)
            db.flush()  # need case.id before writing its file
        elif case.status == "analyzing":
            continue  # don't fight an in-flight run; catch up next list load
        if case.source_doc_id == item["doc_id"]:
            db.commit()
            continue

        url = (
            f"{DOCGEN_BASE}/api/profiles/{item['profile_id']}/cases/{item['case_id']}"
            f"/documents/{item['doc_id']}/download"
        )
        try:
            dl = httpx.get(url, headers=headers, timeout=10.0)
            dl.raise_for_status()
        except httpx.HTTPError:
            db.commit()
            continue
        write_slot_file(case, "original", ".docx", dl.content, item["file_name"], MIN_SLOTS_READY)
        case.source_doc_id = item["doc_id"]
        db.commit()


app.include_router(make_case_router(          # Kong exposes this as /api/docdiff/cases...
    service_scope="docdiff",                  # 403 unless the JWT carries scope "docdiff"
    upload_slots=UPLOAD_SLOTS,
    min_slots_ready=MIN_SLOTS_READY,
    analyze=_analyze,
    to_audit_output=_to_audit_output,
    before_list=_sync_from_docgen,
    allow_extra_pairs=False,
))
