"""audit outbox: token usage column

Mirrors the `usage` column outbox.py now declares — carried per event so
audit-service can store what a piece of LLM work cost in real columns rather
than mining it out of the producer's `detail` blob.

Revision ID: 9f2b71c4ade8
Revises: 248a03e7168d
Create Date: 2026-09-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f2b71c4ade8'
down_revision: Union[str, Sequence[str], None] = '248a03e7168d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('audit_outbox', sa.Column('usage', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('audit_outbox', 'usage')
