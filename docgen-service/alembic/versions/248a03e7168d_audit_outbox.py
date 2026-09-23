"""audit outbox

Revision ID: 248a03e7168d
Revises: b3b6f1062af3
Create Date: 2026-08-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '248a03e7168d'
down_revision: Union[str, Sequence[str], None] = 'b3b6f1062af3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('audit_outbox',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('service', sa.String(length=80), nullable=False),
    sa.Column('action', sa.String(length=80), nullable=False),
    sa.Column('resource', sa.String(length=500), nullable=True),
    sa.Column('subject_type', sa.String(length=40), nullable=True),
    sa.Column('subject_id', sa.String(length=64), nullable=True),
    sa.Column('profile_id', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('token', sa.Text(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('audit_outbox')
