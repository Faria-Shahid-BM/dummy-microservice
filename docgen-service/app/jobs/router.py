"""Job status + SSE stream endpoints (authorization: any signed-in user)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.deps import current_user
from app.core.db import db_session
from app.jobs.runner import runner
from app.jobs.sse import sse_response
from app.models import Job, User

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _authorized_job(db: Session, user: User, job_id: str) -> Job:
    """Single organization: every authenticated user may observe any job."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/{job_id}")
def job_status(
    job_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
) -> dict:
    _authorized_job(db, user, job_id)
    return runner.get_status(db, job_id)


@router.get("/{job_id}/stream")
async def job_stream(
    job_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    _authorized_job(db, user, job_id)
    return sse_response(job_id)
