"""audit outbox: delivery attempts + dead-letter columns

Mirrors the `attempts` and `failed_at` columns outbox.py now uses so one
undeliverable event can't block the queue forever. docgen manages its schema
with Alembic, so the columns the other services get via ensure_columns() must
be added here explicitly.

Revision ID: a1c3e5f70b21
Revises: 9f2b71c4ade8
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c3e5f70b21'
down_revision: Union[str, Sequence[str], None] = '9f2b71c4ade8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('audit_outbox',
                  sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('audit_outbox',
                  sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('audit_outbox', 'failed_at')
    op.drop_column('audit_outbox', 'attempts')
