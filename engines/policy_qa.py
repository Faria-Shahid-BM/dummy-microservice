"""Policy Q&A — a stdlib-only retrieval-augmented chat engine (ported from the POC).

Answers questions about a policy document grounded ONLY in retrieved chunks.
No FAISS, no numpy, no pickle at runtime: embeddings come from the injected
``LLMProvider``, vectors are stored as a flat float32 file (stdlib ``array``),
and nearest-neighbour search is plain-Python cosine similarity (fine for the
hundreds-to-low-thousands of chunks a single policy produces).

Two corpora, per the product decision ("both"):
  - a BUNDLED default index (``engines/data/policy_qa_bundled/``), built once
    from the handed-over policy chunks, and
  - an optional PER-PROFILE index built by ingesting that profile's own
    uploaded policy document (the module layer owns the directory).
A profile's own index is used when present; otherwise the bundled one.

Pure logic: no FastAPI, no SQLAlchemy, no config imports. The LLM provider,
model names, index directories and the optional ``emit`` progress callback all
arrive as arguments.

Load-bearing invariant: vectors are UNIT-NORMALIZED AT WRITE TIME, so search
can rank by plain dot product (cosine == dot for unit vectors). ``load_index``
never re-normalizes; ``search`` normalizes only the query vector.
"""
from __future__ import annotations

import array
import json
import operator
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # runtime-pure: engines never import app.core.config
    from app.llm.base import LLMProvider

EmitFn = Callable[[str, str], None]

BUNDLED_DIR: Path = Path(__file__).resolve().parent / "data" / "policy_qa_bundled"

TOP_K = 8
EMBED_BATCH = 64
TARGET_CHUNK_CHARS = 900

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
# Frozen domain IP — loaded verbatim from the legacy inline prompt.
SYSTEM_PROMPT: str = (
    (_PROMPTS_DIR / "policy_qa_system.md").read_text(encoding="utf-8").rstrip("\n")
)
# Legacy inline preamble of the second system message (verbatim).
_CONTEXT_PREAMBLE = "Policy context (use ONLY this to answer):\n\n"

_INDEX_FILES = ("chunks.json", "vectors.bin", "meta.json")


# ── vector helpers (pure python) ─────────────────────────────────────────────

def _unit(vec: Any) -> array.array:
    """Return a unit-length float32 copy of `vec` (so cosine == dot product)."""
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return array.array("f", (x / norm for x in vec))


def _dot(a: Any, b: Any) -> float:
    return sum(map(operator.mul, a, b))


# ── chunking (for uploaded policy documents) ─────────────────────────────────

_HEADING_RE = re.compile(r"^\s*(\d+(\.\d+)*\.?\s+\S|[A-Z][A-Z0-9 ,/&()\-]{4,}$)")


def _looks_like_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 90:
        return False
    return bool(_HEADING_RE.match(s))


def chunk_document(text: str, target_chars: int = TARGET_CHUNK_CHARS) -> list[dict[str, str]]:
    """Split a policy document into [{heading, content}] chunks of ~target_chars,
    carrying the most recent detected heading. Heuristic but robust."""
    if not text or not text.strip():
        return []
    chunks: list[dict[str, str]] = []
    heading = ""
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        body = "\n".join(buf).strip()
        if body:
            chunks.append({"heading": heading, "content": body})
        buf = []
        buf_len = 0

    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if _looks_like_heading(line):
            flush()
            heading = line.strip()
            continue
        if not line.strip():
            if buf_len >= target_chars:
                flush()
            else:
                buf.append("")
            continue
        buf.append(line)
        buf_len += len(line) + 1
        if buf_len >= target_chars:
            flush()
    flush()
    return chunks


# ── embedding ────────────────────────────────────────────────────────────────

def _chunk_embed_text(chunk: dict[str, str]) -> str:
    heading = chunk.get("heading", "")
    content = chunk.get("content", "")
    return f"{heading}\n{content}".strip() if heading else content


def _embed_texts(
    texts: list[str],
    provider: "LLMProvider",
    embed_model: str,
    *,
    batch: int = EMBED_BATCH,
    emit: EmitFn | None = None,
) -> list[array.array]:
    """Embed a list of texts (batched) and return UNIT vectors (write-time norm)."""
    vectors: list[array.array] = []
    total = len(texts)
    for i in range(0, total, batch):
        part = texts[i : i + batch]
        raw = provider.embed(embed_model, part)
        vectors.extend(_unit(v) for v in raw)
        if emit is not None:
            emit(
                "event",
                json.dumps(
                    {"stage": "embed", "done": min(i + batch, total), "total": total},
                    separators=(",", ":"),
                ),
            )
    return vectors


# ── index persistence (chunks.json + vectors.bin + meta.json) ────────────────

@dataclass(frozen=True)
class Index:
    """An in-memory policy index: chunk dicts + aligned unit float32 vectors."""

    chunks: list[dict[str, str]]
    vectors: list[array.array]
    dim: int

    def __len__(self) -> int:
        return len(self.chunks)


_CACHE: dict[str, tuple[float, "Index"]] = {}
_CACHE_LOCK = threading.Lock()


def has_index(index_dir: Path | None) -> bool:
    """True when `index_dir` contains a complete persisted index."""
    if index_dir is None:
        return False
    return all((index_dir / name).exists() for name in _INDEX_FILES)


def _save_index(
    index_dir: Path, chunks: list[dict[str, str]], vectors: list[array.array]
) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "chunks.json").write_text(
        json.dumps(chunks, ensure_ascii=False), encoding="utf-8"
    )
    flat = array.array("f")
    for v in vectors:
        flat.extend(v)
    (index_dir / "vectors.bin").write_bytes(flat.tobytes())
    dim = len(vectors[0]) if vectors else 0
    (index_dir / "meta.json").write_text(
        json.dumps({"count": len(chunks), "dim": dim}), encoding="utf-8"
    )
    with _CACHE_LOCK:
        _CACHE.pop(str(index_dir), None)


def load_index(index_dir: Path) -> Index:
    """Load a persisted index, cached by ``vectors.bin`` mtime (thread-safe).

    POC bug fixed here: the legacy loader derived the vector count from
    ``len(chunks)``, silently truncating (or producing short/empty tail
    vectors) when the files disagreed. We now validate ``meta.count`` against
    both the chunks length and the byte size of ``vectors.bin`` and raise
    ``ValueError`` on any mismatch.

    Raises FileNotFoundError when the index files are absent.
    """
    vbin = index_dir / "vectors.bin"
    cjson = index_dir / "chunks.json"
    mjson = index_dir / "meta.json"
    if not (vbin.exists() and cjson.exists() and mjson.exists()):
        raise FileNotFoundError(f"no policy index at {index_dir}")
    key = str(index_dir)
    mtime = vbin.stat().st_mtime
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]

    chunks = json.loads(cjson.read_text(encoding="utf-8"))
    meta = json.loads(mjson.read_text(encoding="utf-8"))
    count = int(meta.get("count", -1))
    dim = int(meta.get("dim", 0))
    if dim <= 0:
        raise ValueError(f"policy index at {index_dir}: invalid dim {dim} in meta.json")
    if count != len(chunks):
        raise ValueError(
            f"policy index at {index_dir}: meta.count={count} but chunks.json has "
            f"{len(chunks)} chunks"
        )
    flat = array.array("f")
    flat.frombytes(vbin.read_bytes())
    if len(flat) != count * dim:
        raise ValueError(
            f"policy index at {index_dir}: vectors.bin holds {len(flat)} floats, "
            f"expected count*dim={count * dim}"
        )
    vectors = [flat[i * dim : (i + 1) * dim] for i in range(count)]
    index = Index(chunks=chunks, vectors=vectors, dim=dim)
    with _CACHE_LOCK:
        _CACHE[key] = (mtime, index)
    return index


# ── build / ingest ───────────────────────────────────────────────────────────

def build_index(
    source_path: Path,
    index_dir: Path,
    provider: "LLMProvider",
    embed_model: str,
    *,
    emit: EmitFn | None = None,
) -> dict[str, int]:
    """Chunk + embed an extracted policy text file and persist the index.

    ``source_path`` is a UTF-8 text/markdown file (the module layer runs
    pdf/docx extraction through ``app.engines.extraction`` first — the single
    extraction implementation; see ARCHITECTURE.md, POC bug #6).

    Heading-aware ~900-char chunking; embeddings in batches of 64;
    UNIT-NORMALIZED AT WRITE TIME (the invariant that makes dot-product search
    valid). Writes chunks.json + vectors.bin (flat float32) + meta.json.
    Returns ``{"count": <chunks>, "dim": <embedding dim>}``.
    """
    text = source_path.read_text(encoding="utf-8", errors="replace")
    chunks = chunk_document(text)
    if not chunks:
        raise ValueError("No text could be extracted from the document.")
    if emit is not None:
        emit(
            "event",
            json.dumps({"stage": "chunk", "chunks": len(chunks)}, separators=(",", ":")),
        )
    vectors = _embed_texts(
        [_chunk_embed_text(c) for c in chunks], provider, embed_model, emit=emit
    )
    _save_index(index_dir, chunks, vectors)
    dim = len(vectors[0]) if vectors else 0
    if emit is not None:
        emit(
            "event",
            json.dumps(
                {"stage": "saved", "count": len(chunks), "dim": dim},
                separators=(",", ":"),
            ),
        )
    return {"count": len(chunks), "dim": dim}


# ── retrieval + answer ───────────────────────────────────────────────────────

def search(
    index: Index,
    query: str,
    provider: "LLMProvider",
    embed_model: str,
    top_k: int = TOP_K,
) -> list[dict[str, Any]]:
    """Rank the index chunks against `query`; return top_k copies with scores.

    Stored vectors are unit-length (write-time invariant), so ranking by dot
    product equals cosine similarity. Ties preserve chunk order (stable sort),
    keeping the ordering deterministic.
    """
    if not index.chunks:
        return []
    qv = _unit(provider.embed(embed_model, [query])[0])
    scores = [_dot(qv, v) for v in index.vectors]
    order = sorted(range(len(scores)), key=scores.__getitem__, reverse=True)
    return [{**index.chunks[i], "score": scores[i]} for i in order[:top_k]]


def _context_block(retrieved: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for c in retrieved:
        heading = c.get("heading", "")
        content = c.get("content", "")
        parts.append(f"## {heading}\n{content}" if heading else content)
    return "\n\n".join(parts).strip()


def answer(
    question: str,
    history: list[dict[str, Any]],
    index_dir: Path | None,
    provider: "LLMProvider",
    chat_model: str,
    embed_model: str,
    *,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """One grounded Q&A turn: retrieve, then answer strictly from the context.

    Uses the per-profile index at `index_dir` when it exists; otherwise falls
    back to the bundled default index. Keeps the last 8 history turns for
    continuity; temperature 0.1. Returns ``{"answer": str, "sources": [str]}``.
    ``system_prompt`` overrides the shipped grounding system prompt (None ->
    ``SYSTEM_PROMPT``).
    """
    if not question or not question.strip():
        return {"answer": "", "sources": []}

    resolved_dir = index_dir if has_index(index_dir) else BUNDLED_DIR
    index = load_index(resolved_dir)
    retrieved = search(index, question, provider, embed_model, top_k=TOP_K)
    if not retrieved:
        return {"answer": "No policy has been indexed yet for this profile.", "sources": []}

    context = _context_block(retrieved)
    messages: list[dict[str, Any]] = [
        {"role": "system",
         "content": system_prompt if system_prompt is not None else SYSTEM_PROMPT},
        {"role": "system", "content": f"{_CONTEXT_PREAMBLE}{context}"},
    ]
    # keep the last few turns for continuity
    for msg in (history or [])[-8:]:
        role = msg.get("role")
        if role in ("user", "assistant") and msg.get("content"):
            messages.append({"role": role, "content": msg["content"]})
    messages.append({"role": "user", "content": question})

    reply = provider.call(chat_model, messages, temperature=0.1)

    sources: list[str] = []
    for c in retrieved:
        h = (c.get("heading") or "").strip()
        if h and h not in sources:
            sources.append(h)
    return {"answer": reply, "sources": sources}
