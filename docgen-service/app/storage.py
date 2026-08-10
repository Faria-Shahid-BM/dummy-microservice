"""Filesystem layout and safe path handling under ``DATA_DIR``.

Every filesystem touch in the app goes through this module. Paths are always
derived from DB rows (server-generated ids), but every helper still validates
its parts and confines the resolved result to ``settings.data_dir`` as defense
in depth.

Layout (see ARCHITECTURE.md, "Storage"):
    data/profiles/{profile_id}/
      templates/{template_id}/v{n}.docx
      cases/{case_id}/input/, pages/, case_text.md, analysis.md,
                      selected_docs.json, output/{uuid}.docx + .provenance.json
      reviews/{review_id}/uploads + result files
      policy_qa/  (index files)
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.config import settings

if TYPE_CHECKING:  # typing only — services must not import FastAPI at runtime
    from fastapi import UploadFile

CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming chunks

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_PATH_SEPARATORS = ("/", "\\")


class StorageError(Exception):
    """A path escaped or would escape the data directory."""


class UploadError(Exception):
    """An upload was rejected (bad extension or size limit exceeded)."""


def _data_root() -> Path:
    return settings.data_dir.resolve()


def _check_part(part: str) -> str:
    if not part:
        raise StorageError("Empty path segment")
    if any(sep in part for sep in _PATH_SEPARATORS):
        raise StorageError(f"Path segment contains a separator: {part!r}")
    if part.startswith("."):
        raise StorageError(f"Path segment starts with a dot: {part!r}")
    return part


def safe_path(*parts: str) -> Path:
    """Join ``parts`` under DATA_DIR; raise StorageError on any escape attempt.

    Each part must be a single path segment: no separators, no leading dot.
    The joined path is resolved and must remain inside ``settings.data_dir``.
    """
    root = _data_root()
    candidate = root.joinpath(*(_check_part(p) for p in parts)).resolve()
    if not candidate.is_relative_to(root):
        raise StorageError(f"Path escapes the data directory: {candidate}")
    return candidate


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ----------------------------------------------------------------- accessors


def profile_dir(profile_id: str) -> Path:
    """Root directory for one profile (bank/business unit)."""
    return _ensure_dir(safe_path("profiles", profile_id))


def template_version_path(profile_id: str, template_id: str, version_no: int) -> Path:
    """Path of one stored template version file (``v{n}.docx``)."""
    path = safe_path("profiles", profile_id, "templates", template_id, f"v{int(version_no)}.docx")
    _ensure_dir(path.parent)
    return path


def case_dir(profile_id: str, case_id: str) -> Path:
    """Root directory for one docgen case."""
    return _ensure_dir(safe_path("profiles", profile_id, "cases", case_id))


def case_input_dir(profile_id: str, case_id: str) -> Path:
    """Uploaded input files for a case."""
    return _ensure_dir(safe_path("profiles", profile_id, "cases", case_id, "input"))


def case_output_dir(profile_id: str, case_id: str) -> Path:
    """Generated documents + provenance for a case."""
    return _ensure_dir(safe_path("profiles", profile_id, "cases", case_id, "output"))


def review_dir(profile_id: str, review_id: str) -> Path:
    """Uploads and result files for one review (any reviewer module)."""
    return _ensure_dir(safe_path("profiles", profile_id, "reviews", review_id))


def policy_qa_dir(profile_id: str) -> Path:
    """Policy Q&A index directory for a profile."""
    return _ensure_dir(safe_path("profiles", profile_id, "policy_qa"))


# ------------------------------------------------------------------- uploads


def _normalize_suffixes(allowed_suffixes: set[str]) -> set[str]:
    return {s.lower() if s.startswith(".") else f".{s.lower()}" for s in allowed_suffixes}


async def save_upload(
    upload_file: "UploadFile | Any",
    dest: Path,
    *,
    allowed_suffixes: set[str],
    max_mb: int | None = None,
) -> int:
    """Stream an upload to ``dest`` in 1 MiB chunks; return bytes written.

    Validates the original filename's suffix (case-insensitive) against
    ``allowed_suffixes`` and enforces the size cap *during* the stream —
    a partial file is deleted before UploadError is raised. Works with any
    object exposing ``.filename`` and async ``.read(n)``.
    """
    allowed = _normalize_suffixes(allowed_suffixes)
    original = upload_file.filename or ""
    suffix = Path(original).suffix.lower()
    if suffix not in allowed:
        raise UploadError(
            f"File type {suffix or '(none)'!r} is not allowed; "
            f"expected one of: {', '.join(sorted(allowed))}"
        )

    limit_mb = max_mb or settings.max_upload_mb
    limit_bytes = limit_mb * 1024 * 1024

    dest = dest.resolve()
    if not dest.is_relative_to(_data_root()):
        raise StorageError(f"Upload destination escapes the data directory: {dest}")
    _ensure_dir(dest.parent)

    written = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await upload_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                if written + len(chunk) > limit_bytes:
                    raise UploadError(f"Upload exceeds the {limit_mb} MB size limit")
                out.write(chunk)
                written += len(chunk)
    except Exception:
        dest.unlink(missing_ok=True)  # never leave partial files behind
        raise
    return written


def sanitize_display_name(name: str) -> str:
    """Sanitize an original filename for DB storage / display only.

    On-disk names are always server-generated; this only cleans what users see.
    """
    # last segment regardless of client OS separator
    base = re.split(r"[\\/]", name or "")[-1]
    base = _CONTROL_CHARS.sub("", base).strip().lstrip(".")
    return base[:255] or "file"


# ------------------------------------------------------------------ deletion


def delete_tree(path: Path) -> None:
    """Recursively delete ``path``, refusing anything outside DATA_DIR."""
    root = _data_root()
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise StorageError(f"Refusing to delete outside the data directory: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
