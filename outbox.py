"""Transactional outbox for the audit trail.

Every producer service (collateral/insurance/valuation/doc_rev via
case_store.py, policyqa-service, docgen-service) has its own local database.
audit-service is a separate process with a separate database, reached over
HTTP — there is no way to make "the business write" and "the write lands in
audit-service" one atomic operation across that boundary; that would need a
distributed transaction, which nothing here is set up for and which would be
wildly disproportionate for an audit log.

What IS achievable, and is the standard fix for exactly this: write the
*intent* to audit into a plain table in the producer's own database, in the
SAME transaction as the business row it describes (``enqueue()``, called
before ``commit()``). That gets the property that actually matters — the
business row and "this must be audited" either both exist or neither does,
crash-safe, with no network call anywhere near the transaction. A background
loop (``run_relay()``) then delivers pending rows to audit-service whenever
it's reachable, retrying forever on failure instead of the old
fire-and-forget ``audit_client.audit()``, which silently dropped the event on
any hiccup. Delivery itself is only eventually consistent — that's the
honest trade for not needing a distributed transaction.

Usage in a service that already has its own SQLAlchemy Base/engine::

    OUTBOX = outbox_table(Base.metadata)   # module scope, alongside other tables
    Base.metadata.create_all(engine)       # or an Alembic migration — picks it up like any other table

    # inside a request, before commit():
    db.add(case)
    enqueue(db, OUTBOX, service="collateral", action="case.create", token=token, resource=name)
    db.commit()

    # once, at startup:
    asyncio.create_task(run_relay(SessionLocal, OUTBOX))
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import (Column, DateTime, JSON, MetaData, String, Table, Text,
                        insert, inspect, select, text, update)

from audit_client import AUDIT_BASE

logger = logging.getLogger("outbox")


def outbox_table(metadata: MetaData, name: str = "audit_outbox") -> Table:
    """Register the outbox table on a service's own MetaData/Base so its
    normal ``create_all()``/migration path creates it like any other table —
    no shared database, no cross-service schema coupling."""
    return Table(
        name, metadata,
        Column("id", String(32), primary_key=True, default=lambda: uuid.uuid4().hex),
        Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)),
        Column("service", String(80), nullable=False),
        Column("action", String(80), nullable=False),
        Column("resource", String(500), nullable=True),
        Column("subject_type", String(40), nullable=True),
        Column("subject_id", String(64), nullable=True),
        Column("profile_id", String(64), nullable=True),
        Column("detail", JSON, nullable=True),
        # {model, prompt, completion, total} for work that spent tokens; NULL
        # otherwise. Carried as its own column rather than inside `detail` so
        # it survives a producer changing what it audits.
        Column("usage", JSON, nullable=True),
        # The caller's bearer token, so a delayed delivery still gets
        # audit-service's re-verified-identity guarantee instead of a
        # self-reported user_id. Read once at delivery time, never exposed
        # outside this table.
        Column("token", Text, nullable=True),
        Column("sent_at", DateTime(timezone=True), nullable=True),
    )


# Columns added after this table's first release. create_all() only creates
# missing TABLES, never new columns on one that already exists — so a service
# whose outbox predates a column here would fail every enqueue without this.
# Same cheap stand-in for migration tooling as case_store's _ADDED_COLUMNS.
_ADDED_COLUMNS = (("usage", "JSON"),)


def ensure_columns(engine, table_name: str = "audit_outbox") -> None:
    """Bring an existing outbox table up to the current schema.

    Safe to call on every startup, and a no-op before the table exists (the
    caller's ``create_all`` will build it complete).
    """
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns(table_name)}
    with engine.begin() as conn:
        for name, ddl_type in _ADDED_COLUMNS:
            if name not in existing:
                # Quoted: "usage" is a reserved word in several engines.
                conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN "{name}" {ddl_type}'))


def enqueue(
    conn,
    table: Table,
    *,
    service: str,
    action: str,
    token: str | None,
    resource: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    profile_id: str | None = None,
    detail: dict | None = None,
    usage: dict | None = None,
) -> None:
    """Insert one outbox row on the caller's own connection/session. Call
    this BEFORE ``commit()``/``db.commit()`` — same transaction as the
    business write it describes, that's the entire point."""
    conn.execute(insert(table).values(
        id=uuid.uuid4().hex,
        service=service,
        action=action,
        resource=resource,
        subject_type=subject_type,
        subject_id=subject_id,
        profile_id=profile_id,
        detail=detail,
        usage=usage,
        token=token,
    ))


async def run_relay(session_factory, table: Table, *, poll_interval: float = 2.0) -> None:
    """Background loop: deliver unsent rows to audit-service oldest-first,
    forever. Intended to run for the lifetime of the service process via
    ``asyncio.create_task(run_relay(...))`` at startup."""
    while True:
        try:
            await asyncio.to_thread(_deliver_batch, session_factory, table)
        except Exception:
            logger.exception("outbox relay: batch failed")
        await asyncio.sleep(poll_interval)


def _deliver_batch(session_factory, table: Table, *, batch_size: int = 50) -> None:
    db = session_factory()
    try:
        rows = db.execute(
            select(table)
            .where(table.c.sent_at.is_(None))
            .order_by(table.c.created_at)
            .limit(batch_size)
        ).mappings().all()
        for row in rows:
            try:
                resp = httpx.post(
                    f"{AUDIT_BASE}/audit",
                    json={
                        "event_id": row["id"],
                        "service": row["service"],
                        "action": row["action"],
                        "resource": row["resource"],
                        "subject_type": row["subject_type"],
                        "subject_id": row["subject_id"],
                        "profile_id": row["profile_id"],
                        "metadata": row["detail"],
                        "usage": row["usage"],
                    },
                    headers={"Authorization": f"Bearer {row['token']}"} if row["token"] else {},
                    timeout=5.0,
                )
                resp.raise_for_status()
            except Exception:
                logger.warning("outbox relay: delivery failed for %s, will retry", row["id"])
                break  # stop the batch here so delivery order is preserved on retry
            db.execute(
                update(table).where(table.c.id == row["id"]).values(sent_at=datetime.now(timezone.utc))
            )
            db.commit()
    finally:
        db.close()
