"""SSE bridge from job buffers to async endpoints.

The one service module allowed to import FastAPI/Starlette. Protocol
(ARCHITECTURE.md "Jobs + SSE"): every message is ``data: {json}`` —
``{"type": "reasoning"|"content"|"event", "text": ...}`` for items,
``{"type": "done"}`` / ``{"type": "error", "error": ...}`` terminal.
Buffered items replay first, then live items; ``: ka`` comment lines keep
the connection alive through proxies during silence.
"""
from __future__ import annotations

import json
import queue
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.core.db import session_scope
from app.jobs.runner import runner
from app.models import Job

KEEPALIVE_SECONDS = 15
IDLE_CUTOFF_SECONDS = 600

_TERMINAL = ("done", "error")


def _message(type_: str, text: str) -> str:
    if type_ == "done":
        payload: dict = {"type": "done"}
    elif type_ == "error":
        payload = {"type": "error", "error": text}
    else:
        payload = {"type": type_, "text": text}
    return f"data: {json.dumps(payload)}\n\n"


def _terminal_from_db(job_id: str) -> str | None:
    """Reconstruct the terminal message from the jobs row (buffer reaped)."""
    with session_scope() as db:
        job = db.get(Job, job_id)
    if job is None:
        return _message("error", "unknown job")
    if job.status == "succeeded":
        return _message("done", "")
    if job.status in ("failed", "cancelled"):
        return _message("error", job.error or f"job {job.status}")
    return None  # active row with no buffer: pre-orphan-marking edge


async def job_event_stream(job_id: str) -> AsyncIterator[str]:
    """Yield SSE lines for a job: replay, then live, with keep-alives.

    Closes right after the terminal message. If the buffer was already
    reaped but the DB row is terminal, emits the terminal immediately.
    After ``IDLE_CUTOFF_SECONDS`` of total silence the stream closes
    without a terminal — EventSource reconnects and replays.
    """
    buffer = runner.get_buffer(job_id)
    if buffer is None:
        terminal = await run_in_threadpool(_terminal_from_db, job_id)
        yield terminal if terminal is not None else _message(
            "error", "job stream unavailable"
        )
        return

    replay, q = buffer.subscribe()
    try:
        for type_, text in replay:
            yield _message(type_, text)
            if type_ in _TERMINAL:
                return
        idle = 0
        while True:
            try:
                item = await run_in_threadpool(q.get, True, KEEPALIVE_SECONDS)
            except queue.Empty:
                idle += KEEPALIVE_SECONDS
                if idle >= IDLE_CUTOFF_SECONDS:
                    return
                yield ": ka\n\n"
                continue
            idle = 0
            type_, text = item
            yield _message(type_, text)
            if type_ in _TERMINAL:
                return
    finally:
        buffer.unsubscribe(q)


def sse_response(job_id: str) -> StreamingResponse:
    """StreamingResponse wired for SSE (no cache, no proxy buffering)."""
    return StreamingResponse(
        job_event_stream(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
