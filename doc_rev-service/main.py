# document-diff-service/main.py
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

import httpx
from engines import document_diff_html
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from case_store import Case, init_db, make_case_router, remove_case, start_outbox_relay, write_slot_file

init_db()


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_outbox_relay()
    yield


app = FastAPI(lifespan=lifespan)

# Direct container DNS, bypassing Kong — same pattern as AUDIT_BASE in
# audit_client.py. docgen-service's own auth re-verifies whatever token we
# forward, so there's nothing extra to trust here.
DOCGEN_BASE = os.environ.get("DOCGEN_SERVICE_URL", "http://docgen-service:8000")

UPLOAD_SLOTS = {"original": {".docx"}, "returned": {".docx"}}
MIN_SLOTS_READY = ["original", "returned"]


class ChangeAuditItem(BaseModel):
    type: str
    before: str
    after: str


class MediaSummary(BaseModel):
    original: int
    returned: int
    compared: bool


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
    media: MediaSummary
    changes: list[ChangeAuditItem]


def _to_audit_output(result: dict) -> dict:
    return CompareAuditOutput.model_validate(result).model_dump()


def _read_docx(slot: str, path: Path) -> document_diff_html.ConvertedDocx:
    # UPLOAD_SLOTS rejects anything but .docx, but a case uploaded before that
    # restriction can still hold a .pdf on disk — fail with a readable reason
    # rather than handing a PDF to the .docx converter.
    if path.suffix.lower() != ".docx":
        raise ValueError(
            f"'{slot}' is a {path.suffix or 'file with no extension'}; "
            "this service compares .docx documents. Re-upload it as .docx."
        )
    converted = document_diff_html.docx_to_html(path)
    if not document_diff_html.html_to_text(converted.html):
        # A scan with no text layer lands here, now that images are dropped.
        raise ValueError(f"No text extracted from '{slot}'")
    return converted


def _analyze(paths: dict[str, Path], emit: Callable[[str, str], None],
             user_sub: str, token: str | None) -> dict:
    # user_sub and token are unused: this comparison is deterministic and calls
    # no model, so there is nothing for config-service to decide.
    # Both sides are .docx, so the comparison is always the structural redline
    # — it keeps the headings, tables and lists a contract is laid out with.
    original = _read_docx("original", paths["original"])
    returned = _read_docx("returned", paths["returned"])
    result = document_diff_html.compare_documents_html(original.html, returned.html)

    # docx_to_html() drops images before the diff ever sees them, because the
    # returned copy is signed and the generated original never is — comparing
    # them would flag a change on every signed document. That's the right call
    # for a text diff, but it must not read as "the signature was checked", so
    # what was set aside is reported rather than left silent. Which slot the
    # counts belong to is this service's knowledge, not the engine's, so they
    # are attached here.
    result["media"] = {
        "original": original.image_count,
        "returned": returned.image_count,
        "compared": False,
    }
    return result


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
