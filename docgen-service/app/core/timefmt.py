"""Timestamp serialisation for API responses.

SQLite stores no timezone information even on a ``DateTime(timezone=True)``
column, so a value written as UTC reads back naive. Serialised with a plain
``.isoformat()`` it carries no offset, and a browser's Date parser treats a
timezone-less string as LOCAL time — it never converts, so the raw UTC clock
digits render as if they were already local. The result is every timestamp in
the UI silently wrong by the viewer's offset.

``case_store.py`` carries the same fix for the review services; this is the
docgen-side copy, since the two codebases share no utility module.
"""
from __future__ import annotations

from datetime import datetime, timezone


def iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 with an explicit UTC offset, or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
