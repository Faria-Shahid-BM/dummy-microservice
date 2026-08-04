"""Shared SSE streaming helper for the LLM-backed services.

Turns a blocking engine pipeline (which reports progress through an
``emit(type, text)`` callback) into a live Server-Sent Events stream, so the
frontend can show what's happening instead of spinning on one long request.

How it works: the blocking pipeline is run on a worker thread; its ``emit``
calls are hopped back onto the event loop (``call_soon_threadsafe``) into an
``asyncio.Queue``; an async generator drains that queue and yields SSE frames.
When the pipeline returns, its value is emitted as a final ``result`` event
(or ``error`` if it raised).

Event contract (every ``data:`` payload is valid JSON — the client always
``JSON.parse``es it):

    event: open      data: {"ok": true}                      once, on connect
    event: event     data: {"stage": "...", ...}             engine stage/page progress (already JSON)
    event: content   data: "<text chunk>"                    live LLM tokens (JSON-encoded string)
    event: reasoning data: "<text chunk>"                    live reasoning tokens, if any
    event: result    data: { ...full pipeline result... }    once, on success
    event: error     data: {"error": "<message>"}            once, on failure

Any FastAPI service can reuse this: build a ``run_blocking(emit)`` closure that
runs its engine, then ``return await sse_stream(run_blocking)``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from fastapi.responses import StreamingResponse

# emit(type, text): type in {"event", "content", "reasoning"} (engine contract).
EmitFn = Callable[[str, str], None]
# run_blocking(emit) -> result: the blocking pipeline; returns a JSON-able value.
RunFn = Callable[[EmitFn], Any]

_DONE = object()


def _frame(event: str, data: str) -> str:
    """One SSE frame. ``data`` must be a single line (our payloads are JSON,
    which never contains a raw newline)."""
    return f"event: {event}\ndata: {data}\n\n"


async def sse_stream(run_blocking: RunFn) -> StreamingResponse:
    """Run ``run_blocking`` on a thread and stream its emit()s as SSE."""
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(ev_type: str, text: str) -> None:
        # Called from the worker thread — marshal back onto the loop safely.
        loop.call_soon_threadsafe(queue.put_nowait, (ev_type, text))

    async def _run() -> None:
        try:
            result = await loop.run_in_executor(None, run_blocking, emit)
            loop.call_soon_threadsafe(queue.put_nowait, ("result", json.dumps(result)))
        except Exception as exc:  # surface engine failures to the client
            loop.call_soon_threadsafe(
                queue.put_nowait, ("error", json.dumps({"error": str(exc)}))
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)

    async def _gen():
        task = asyncio.create_task(_run())
        try:
            yield _frame("open", json.dumps({"ok": True}))
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                ev_type, text = item
                # "event"/"result"/"error" carry JSON already; token chunks
                # ("content"/"reasoning") are JSON-encoded so newlines/quotes
                # can't break the single-line SSE data field.
                data = text if ev_type in ("event", "result", "error") else json.dumps(text)
                yield _frame(ev_type, data)
        finally:
            await task

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # tell Kong/nginx not to buffer the stream
        },
    )
