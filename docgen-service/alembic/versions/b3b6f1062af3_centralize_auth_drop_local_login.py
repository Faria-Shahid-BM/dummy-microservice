"""centralize auth: drop local login/session state

Revision ID: b3b6f1062af3
Revises: 76daf17fe404
Create Date: 2026-08-12 12:56:20.909478

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3b6f1062af3'
down_revision: Union[str, Sequence[str], None] = '76daf17fe404'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the local session-cookie/password-login state — identity and
    roles are now sourced exclusively from the centrally-issued JWT (see
    app/auth/deps.py's current_user()); the users table becomes a read-only
    identity cache with no independently-editable permission fields."""
    op.drop_table('sessions')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_external_subject'))
        batch_op.drop_column('can_edit_config')
        batch_op.drop_column('external_subject')
        batch_op.drop_column('auth_provider')
        batch_op.drop_column('password_hash')


def downgrade() -> None:
    """Reverse: restore the local-login columns and the sessions table."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('password_hash', sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column('auth_provider', sa.String(length=16), nullable=False, server_default='local')
        )
        batch_op.add_column(sa.Column('external_subject', sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column('can_edit_config', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.create_index(
            batch_op.f('ix_users_external_subject'), ['external_subject'], unique=False
        )

    op.create_table(
        'sessions',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('csrf_token', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=400), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sessions_token_hash'), ['token_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_sessions_user_id'), ['user_id'], unique=False)
