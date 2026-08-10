"""Policy Q&A module: grounded chat over a profile's policy index.

Endpoints (ARCHITECTURE.md "API surface", `policy_qa`):

- ``GET  /api/profiles/{pid}/policy-qa/status`` — index availability.
- ``POST /api/profiles/{pid}/policy-qa/chat`` — one grounded Q&A turn.
  **Documented exception to the jobs-only rule** (ARCHITECTURE.md "Backend
  conventions"): a chat turn is interactive and short-lived (one embedding
  call + one completion), so it runs as a plain sync ``def`` endpoint in
  FastAPI's threadpool rather than a job.
- ``POST /api/profiles/{pid}/policy-qa/ingest`` — upload a policy document
  and (re)build this profile's index as a ``policy.ingest`` job.
- ``DELETE /api/profiles/{pid}/policy-qa/index`` — remove the profile index;
  chat falls back to the bundled default index automatically.

Roles: any profile member may view status and chat; ingest and delete are
maker-only. All files live under ``data/profiles/{pid}/policy_qa/`` via
``storage.policy_qa_dir`` (server-generated names only).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import audit, storage
from app.auth.deps import current_user, require_profile_maker, require_profile_member
from app.control.profile_config import effective_model, prompt_override
from app.core.db import db_session, session_scope
from app.engines import extraction
from app.engines import policy_qa as policy_qa_engine
from app.jobs.runner import JobConflict, runner
from app.llm import LLMError
from app.llm.registry import get_provider
from app.models import User

router = APIRouter(prefix="/api", tags=["policy_qa"])

ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}
# Suffixes that go through the extraction engine (text/vision-OCR routing).
_EXTRACTED_SUFFIXES = (".pdf", ".docx")
# Server-side cap on the chat history accepted from the client.
HISTORY_MAX_ITEMS = 24
# Everything a profile ingest leaves on disk (index triple + extracted text).
_INDEX_FILES = ("chunks.json", "vectors.bin", "meta.json", "source.txt")


def _chunk_count(index_dir: Path) -> int | None:
    """``count`` from an index directory's ``meta.json``, or None if unreadable."""
    try:
        meta = json.loads((index_dir / "meta.json").read_text(encoding="utf-8"))
        return int(meta["count"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


# ----------------------------------------------------------------------- status


@router.get("/profiles/{profile_id}/policy-qa/status")
def policy_qa_status(
    profile_id: str,
    role: str = Depends(require_profile_member),
) -> dict:
    index_dir = storage.policy_qa_dir(profile_id)
    has_profile_index = policy_qa_engine.has_index(index_dir)
    bundled_available = policy_qa_engine.has_index(policy_qa_engine.BUNDLED_DIR)
    return {
        "has_profile_index": has_profile_index,
        "chunk_count": _chunk_count(index_dir) if has_profile_index else None,
        "bundled_available": bundled_available,
        "bundled_chunk_count": (
            _chunk_count(policy_qa_engine.BUNDLED_DIR) if bundled_available else None
        ),
    }


# ------------------------------------------------------------------------- chat


class ChatMessage(BaseModel):
    role: str = ""
    content: str = ""


class ChatBody(BaseModel):
    query: str
    history: list[ChatMessage] = Field(default_factory=list)


@router.post("/profiles/{profile_id}/policy-qa/chat")
def chat(
    profile_id: str,
    body: ChatBody,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    """One grounded Q&A turn — ``{"answer": str, "sources": [str]}``.

    Plain sync ``def`` (threadpool): the documented exception to the
    jobs-only rule (see module docstring). Uses the profile's own index when
    one exists; the engine falls back to the bundled index otherwise.
    Models and grounding prompt resolve from the profile's current config.
    """
    question = body.query.strip()
    if not question:
        raise HTTPException(status_code=422, detail="query must not be empty")
    history = [
        {"role": m.role, "content": m.content}
        for m in body.history[-HISTORY_MAX_ITEMS:]
    ]
    index_dir = storage.policy_qa_dir(profile_id)
    try:
        return policy_qa_engine.answer(
            question,
            history,
            index_dir=index_dir if policy_qa_engine.has_index(index_dir) else None,
            provider=get_provider(),
            chat_model=effective_model(db, profile_id, "chat"),
            embed_model=effective_model(db, profile_id, "embedding"),
            system_prompt=prompt_override(db, profile_id, "policy_qa.system.prompt"),
        )
    except LLMError:
        raise HTTPException(
            status_code=502,
            detail=(
                "The language model service could not be reached — "
                "please try again in a moment."
            ),
        )
    except FileNotFoundError:
        # Neither a profile index nor the bundled default exists.
        raise HTTPException(
            status_code=404,
            detail="No policy index is available yet — ingest a policy document first.",
        )


# ----------------------------------------------------------------------- ingest


@router.post("/profiles/{profile_id}/policy-qa/ingest")
async def ingest_policy(
    profile_id: str,
    request: Request,
    file: UploadFile = File(...),
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    """Upload a policy document and rebuild the profile index (job).

    ``.pdf``/``.docx`` go through the extraction engine (vision-OCR for
    scanned PDFs); ``.txt``/``.md`` are ingested as-is. The extracted text is
    persisted as ``source.txt`` and chunked + embedded into the index.
    Returns the job row; progress streams at ``/api/jobs/{id}/stream``.
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"File type {suffix or '(none)'!r} is not allowed; "
                f"expected one of: {', '.join(sorted(ALLOWED_SUFFIXES))}"
            ),
        )
    index_dir = storage.policy_qa_dir(profile_id)
    tmp_path = index_dir / f"upload-{uuid.uuid4().hex}{suffix}"
    try:
        await storage.save_upload(file, tmp_path, allowed_suffixes=ALLOWED_SUFFIXES)
    except storage.UploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    display_name = storage.sanitize_display_name(file.filename or f"policy{suffix}")

    def run(emit) -> dict:
        try:
            # Per-profile config resolved at run time (fresh session).
            with session_scope() as jdb:
                vision_model = effective_model(jdb, profile_id, "vision")
                embed_model = effective_model(jdb, profile_id, "embedding")
                transcription_prompt = prompt_override(
                    jdb, profile_id, "extraction.transcription.prompt")
            target_dir = storage.policy_qa_dir(profile_id)
            source_path = target_dir / "source.txt"
            extra: dict = {}
            if suffix in _EXTRACTED_SUFFIXES:
                transcription = extraction.extract_document(
                    tmp_path, get_provider(), vision_model, emit=emit,
                    prompt=transcription_prompt,
                )
                if (
                    transcription.pages_total
                    and len(transcription.pages_failed) == transcription.pages_total
                ):
                    raise ValueError(
                        "Transcription failed for every page of the document."
                    )
                text = transcription.text
                extra = {
                    "pages_total": transcription.pages_total,
                    "pages_failed": len(transcription.pages_failed),
                }
            else:  # .txt / .md — no extraction needed
                text = tmp_path.read_text(encoding="utf-8", errors="replace")
            source_path.write_text(text, encoding="utf-8")
            result = policy_qa_engine.build_index(
                source_path,
                target_dir,
                get_provider(),
                embed_model,
                emit=emit,
            )
            return {**result, **extra}
        finally:
            tmp_path.unlink(missing_ok=True)  # never leave the raw upload behind

    try:
        job = runner.submit(
            "policy.ingest",
            f"policy-ingest:{profile_id}",
            run,
            profile_id=profile_id,
            subject_id=profile_id,
            user_id=user.id,
        )
    except JobConflict:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=409, detail="A policy ingest is already running for this profile"
        )
    audit.record(db, user, "policy_qa.ingest", profile_id=profile_id,
                 subject_type="policy_index", subject_id=profile_id,
                 detail={"file_name": display_name, "job_id": job["id"]},
                 request=request)
    db.commit()
    return job


# ----------------------------------------------------------------------- delete


@router.delete("/profiles/{profile_id}/policy-qa/index")
def delete_index(
    profile_id: str,
    request: Request,
    role: str = Depends(require_profile_maker),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    """Delete the profile's index files; chat falls back to the bundled index.

    Idempotent — deleting when no index exists succeeds with an empty list.
    """
    index_dir = storage.policy_qa_dir(profile_id)
    deleted: list[str] = []
    for name in _INDEX_FILES:
        path = index_dir / name
        if path.exists():
            path.unlink()
            deleted.append(name)
    audit.record(db, user, "policy_qa.index_delete", profile_id=profile_id,
                 subject_type="policy_index", subject_id=profile_id,
                 detail={"deleted": deleted}, request=request)
    db.commit()
    return {"ok": True, "deleted": deleted}
