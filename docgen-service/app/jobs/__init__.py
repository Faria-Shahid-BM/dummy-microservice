"""Job system: DB-backed runner + in-memory SSE buffers.

The SSE helper is intentionally not re-exported: ``app.jobs.sse`` is the one
service module allowed to import FastAPI/Starlette, so importing it must stay
explicit (``from app.jobs.sse import sse_response``) to keep ``app.jobs``
usable from FastAPI-free contexts (engines, scripts, tests).
"""
from app.jobs.runner import JobBuffer, JobConflict, JobRunner, runner

__all__ = ["JobBuffer", "JobConflict", "JobRunner", "runner"]
