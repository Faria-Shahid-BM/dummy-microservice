"""Shared multi-item batch helper for the LLM-backed review services.

The single-document endpoints (``POST /review``, ``POST /review/stream``) review
exactly one thing per request. This module adds the "N things in one request"
half that the batch endpoints are built on, so collateral (two files per item),
valuation and insurance (one file per item) all behave identically.

An **item** is one unit of review: a mapping of upload *slot* -> file. Slot
names are code-defined literals per service — ``{"legal", "property"}`` for
collateral, ``{"report"}`` for valuation, ``{"policy"}`` for insurance — and the
client sends one list per slot, paired by position: ``legal[i]`` is reviewed
against ``property[i]``.

Event contract (on top of streaming.py's): every ``event`` frame produced inside
an item carries ``item`` (0-based index) and ``item_id``, and three batch-level
stages bracket each item::

    {"stage": "item_start",  "item": 0, "item_id": "...", "files": {slot: name}}
    {"stage": "item_result", "item": 0, "item_id": "...", "result": {...}}
    {"stage": "item_error",  "item": 0, "item_id": "...", "error": "..."}

``item_result`` lands as soon as that item finishes, so a client can render it
while later items are still running. The final ``result`` frame (emitted by
streaming.py from :func:`run_batch`'s return value) repeats every item, as a
backstop for a client that missed a frame.

Items run one at a time on purpose: each engine pipeline already fans out
internally (parallel field extraction, vision OCR), and sequential execution is
what makes an ``item_start`` frame unambiguous — every later frame, including
``content`` token chunks (which are bare strings by contract, with nowhere to
put a tag), belongs to that item until the next ``item_start``.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from fastapi import HTTPException, UploadFile

# emit(type, text): type in {"event", "content", "reasoning"} (engine contract).
EmitFn = Callable[[str, str], None]
# review_one(paths, emit) -> result dict, where `paths` maps every slot to the
# file written for this item.
ReviewFn = Callable[[dict[str, Path], EmitFn | None], Any]


async def collect_items(
    uploads: Mapping[str, Sequence[UploadFile] | None],
    item_ids: Sequence[str] | None = None,
    *,
    fallback_names: Mapping[str, str] | None = None,
) -> list[dict]:
    """Zip per-slot upload lists into positional items, reading the bytes now.

    The bytes are read here (async, in the request) so the blocking pipeline can
    later run on a worker thread without touching the request-scoped uploads.

    ``item_ids`` is optional and only ever echoed back on this item's events —
    it lets a client that submits a subset (e.g. just the rows it hasn't
    reviewed yet) map results onto its own rows. Falls back to the index.

    Raises HTTPException(400) if the slot lists disagree in length, nothing was
    uploaded, or ``item_ids`` doesn't line up with the items.
    """
    fallback_names = fallback_names or {}
    lists = {slot: list(files or []) for slot, files in uploads.items()}
    counts = {slot: len(files) for slot, files in lists.items()}

    if len(set(counts.values())) > 1:
        detail = ", ".join(f"{n} {slot}" for slot, n in counts.items())
        raise HTTPException(
            status_code=400,
            detail=f"uploaded file counts don't match ({detail}) — every item needs "
                   "exactly one file per slot",
        )
    count = next(iter(counts.values()), 0)
    if count == 0:
        raise HTTPException(status_code=400, detail="no documents uploaded")
    if item_ids and len(item_ids) != count:
        raise HTTPException(
            status_code=400,
            detail=f"got {len(item_ids)} item_ids for {count} item(s)",
        )

    items: list[dict] = []
    for i in range(count):
        files = {}
        for slot, uploaded in lists.items():
            upload = uploaded[i]
            name = upload.filename or fallback_names.get(slot, f"{slot}.pdf")
            files[slot] = {"name": name, "bytes": await upload.read()}
        items.append({
            "index": i,
            "item_id": item_ids[i] if item_ids else str(i),
            "files": files,
        })
    return items


def run_batch(items: Sequence[dict], review_one: ReviewFn, emit: EmitFn | None = None) -> dict:
    """Review every item sequentially, isolating failures per item.

    An item that raises does not abort the batch: it gets an ``item_error``
    event and the remaining items still run. Returns
    ``{"results": [{item, item_id, result|error}, ...]}``.
    """
    results: list[dict] = []
    for item in items:
        scope = {"item": item["index"], "item_id": item["item_id"]}
        names = {slot: f["name"] for slot, f in item["files"].items()}
        _emit_event(emit, {"stage": "item_start", **scope, "files": names})

        item_emit = _scoped_emit(emit, scope) if emit else None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                paths = _write_files(Path(tmp), item["files"])
                result = review_one(paths, item_emit)
        except Exception as exc:  # one bad item must not sink the others
            _emit_event(emit, {"stage": "item_error", **scope, "error": str(exc)})
            results.append({**scope, "error": str(exc)})
            continue

        _emit_event(emit, {"stage": "item_result", **scope, "result": result})
        results.append({**scope, "result": result})

    return {"results": results}


def _write_files(root: Path, files: Mapping[str, dict]) -> dict[str, Path]:
    """Materialize one item's uploads, each slot in its own subdirectory so that
    two slots holding identically-named files can't overwrite each other."""
    paths: dict[str, Path] = {}
    for slot, f in files.items():
        slot_dir = root / slot
        slot_dir.mkdir(parents=True, exist_ok=True)
        path = slot_dir / Path(f["name"]).name
        path.write_bytes(f["bytes"])
        paths[slot] = path
    return paths


def _scoped_emit(emit: EmitFn, scope: Mapping[str, Any]) -> EmitFn:
    """Wrap ``emit`` so the engine's own stage events carry the item they belong
    to. Token chunks ("content"/"reasoning") pass through untouched — the data
    field of those frames is a bare string by contract."""

    def scoped(ev_type: str, text: str) -> None:
        if ev_type == "event":
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                text = json.dumps({**payload, **scope}, separators=(",", ":"))
        emit(ev_type, text)

    return scoped


def _emit_event(emit: EmitFn | None, payload: dict) -> None:
    if emit:
        emit("event", json.dumps(payload, separators=(",", ":")))
