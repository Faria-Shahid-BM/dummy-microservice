"""DB-backed job runner: bounded thread pool + in-memory event buffers.

Jobs persist as ``jobs`` rows (queued -> running -> succeeded/failed) while
live output streams through per-job in-memory buffers that SSE endpoints
subscribe to (see ``app.jobs.sse``). Buffers are reaped after a TTL; rows
persist. Single-process by design (ARCHITECTURE.md "Jobs + SSE").
"""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import session_scope
from app.models import JOB_ACTIVE_STATUSES, Job, utcnow

EMIT_TYPES = ("reasoning", "content", "event")
REAPER_INTERVAL_SECONDS = 60

# fn(emit) -> optional result dict; emit(type, text) appends to the buffer
JobFn = Callable[[Callable[[str, str], None]], dict | None]


class JobConflict(Exception):
    """An active job with the same key already exists."""


class JobBuffer:
    """Ordered (type, text) items for one job, fanned out to subscriber queues."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._lock = threading.Lock()
        self._items: list[tuple[str, str]] = []
        self._subscribers: list[queue.Queue[tuple[str, str]]] = []
        self.finished_at: datetime | None = None  # set with the terminal marker

    def append(self, type_: str, text: str) -> None:
        with self._lock:
            self._items.append((type_, text))
            for q in self._subscribers:
                q.put((type_, text))

    def finish(self, marker: str, text: str = "") -> None:
        """Append the terminal marker ('done' | 'error') and timestamp it."""
        with self._lock:
            self._items.append((marker, text))
            self.finished_at = utcnow()
            for q in self._subscribers:
                q.put((marker, text))

    def subscribe(self) -> tuple[list[tuple[str, str]], queue.Queue[tuple[str, str]]]:
        """Atomically snapshot items-so-far and register a live queue."""
        q: queue.Queue[tuple[str, str]] = queue.Queue()
        with self._lock:
            replay = list(self._items)
            self._subscribers.append(q)
        return replay, q

    def unsubscribe(self, q: queue.Queue[tuple[str, str]]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def close(self) -> None:
        """Detach all subscribers, pushing a final 'done' so no stream hangs."""
        with self._lock:
            subs, self._subscribers = list(self._subscribers), []
        for q in subs:
            q.put(("done", ""))


class JobRunner:
    """Bounded executor + buffer registry + reaper. One instance per process."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=settings.job_workers, thread_name_prefix="job"
        )
        self._buffers: dict[str, JobBuffer] = {}
        self._buffers_lock = threading.Lock()
        # serializes the active-key check + insert (app-level uniqueness)
        self._submit_lock = threading.Lock()
        self._reaper_started = False

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Startup hook (app.main lifespan): mark orphans, start the reaper."""
        self.mark_orphans_failed()
        if not self._reaper_started:
            self._reaper_started = True
            threading.Thread(
                target=self._reap_loop, name="job-buffer-reaper", daemon=True
            ).start()

    def shutdown(self) -> None:
        """Shutdown hook (app.main lifespan): stop accepting work, release
        subscribers so no SSE connection outlives the process gracefully."""
        self._executor.shutdown(wait=False, cancel_futures=True)
        with self._buffers_lock:
            buffers = list(self._buffers.values())
        for buf in buffers:
            buf.close()

    def mark_orphans_failed(self) -> int:
        """Fail jobs left queued/running by a previous process (restart)."""
        with session_scope() as db:
            res = db.execute(
                update(Job)
                .where(Job.status.in_(JOB_ACTIVE_STATUSES))
                .values(
                    status="failed",
                    error="server restarted during execution",
                    finished_at=utcnow(),
                )
            )
            return res.rowcount or 0

    # ------------------------------------------------------------ submission

    def submit(
        self,
        kind: str,
        key: str,
        fn: JobFn,
        *,
        profile_id: str | None = None,
        subject_id: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        """Insert a queued jobs row and schedule ``fn(emit)`` on the pool.

        Raises:
            JobConflict: if a job with the same key is queued or running.
        """
        with self._submit_lock, session_scope() as db:
            active = db.execute(
                select(Job.id).where(
                    Job.key == key, Job.status.in_(JOB_ACTIVE_STATUSES)
                )
            ).first()
            if active:
                raise JobConflict(f"job with key '{key}' is already active")
            job = Job(
                kind=kind,
                key=key,
                profile_id=profile_id,
                subject_id=subject_id,
                created_by=user_id,
                status="queued",
            )
            db.add(job)
            db.flush()
            row = _job_dict(job)
        buffer = JobBuffer(row["id"])
        with self._buffers_lock:
            self._buffers[row["id"]] = buffer
        self._executor.submit(self._run, row["id"], fn, buffer)
        return row

    def _run(self, job_id: str, fn: JobFn, buffer: JobBuffer) -> None:
        with session_scope() as db:
            db.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(status="running", started_at=utcnow())
            )

        def emit(type: str, text: str) -> None:
            if type not in EMIT_TYPES:
                raise ValueError(f"emit type must be one of {EMIT_TYPES}: {type!r}")
            buffer.append(type, text)

        try:
            result = fn(emit)
        except Exception as exc:  # job errors land on the row, never the pool
            marker, text = "error", f"{type(exc).__name__}: {exc}"
            values: dict = {"status": "failed", "error": text, "finished_at": utcnow()}
        else:
            marker, text = "done", ""
            values = {
                "status": "succeeded",
                "result": result if isinstance(result, dict) else None,
                "finished_at": utcnow(),
            }
        try:
            with session_scope() as db:
                db.execute(update(Job).where(Job.id == job_id).values(**values))
        finally:
            # marker must reach subscribers even if the persist step fails
            buffer.finish(marker, text)

    # --------------------------------------------------------------- queries

    def get_status(self, db: Session, job_id: str) -> dict | None:
        """Job row as a dict (id, kind, status, error, result, timestamps)."""
        job = db.get(Job, job_id)
        return _job_dict(job) if job else None

    def get_buffer(self, job_id: str) -> JobBuffer | None:
        with self._buffers_lock:
            return self._buffers.get(job_id)

    # ---------------------------------------------------------------- reaper

    def _reap_loop(self) -> None:
        while True:
            time.sleep(REAPER_INTERVAL_SECONDS)
            try:
                self._reap_once()
            except Exception:  # the reaper must never die
                pass

    def _reap_once(self) -> None:
        cutoff = utcnow() - timedelta(minutes=settings.job_buffer_ttl_minutes)
        with self._buffers_lock:
            expired = [
                (job_id, buf)
                for job_id, buf in self._buffers.items()
                if buf.finished_at is not None and buf.finished_at <= cutoff
            ]
            for job_id, _ in expired:
                del self._buffers[job_id]
        # close outside the registry lock: put() may wake subscriber threads
        for _, buf in expired:
            buf.close()


def _job_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "key": job.key,
        "profile_id": job.profile_id,
        "subject_id": job.subject_id,
        "status": job.status,
        "created_by": job.created_by,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "error": job.error,
        "result": job.result,
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


runner = JobRunner()
