# document-diff-service/main.py
from pathlib import Path
from typing import Callable
from engines import extraction, document_diff
from fastapi import FastAPI
from pydantic import BaseModel
from case_store import init_db, make_case_router

app = FastAPI()
init_db()


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


app.include_router(make_case_router(          # Kong exposes this as /api/docdiff/cases...
    service_scope="docdiff",                  # 403 unless the JWT carries scope "docdiff"
    upload_slots={"original": {".docx", ".pdf"}, "returned": {".docx", ".pdf"}},
    min_slots_ready=["original", "returned"],
    analyze=_analyze,
    to_audit_output=_to_audit_output,
))
