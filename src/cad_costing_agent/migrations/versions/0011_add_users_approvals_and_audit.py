"""add users, estimate approval events and the audit log (opt-in access control)

Additive only: four new tables — `users`, `estimate_attributions`,
`approval_events`, `audit_events`. No existing table is altered.

Every existing estimate keeps its exact contents. It gets no attribution row,
which means "not recorded" — these estimates were made before anything recorded
who made them — and is deliberately not backfilled with `local` or any other
guess. An existing estimate has no approval events, so its derived state is
`draft`.

Who created an estimate is a table of its own rather than a column on
`estimates` so the estimate row is untouched: code (and tests) that write an
estimate through the ORM into a database at an older revision keep working.

Creating tables is safe in SQLite and needs no batch mode.

The revision id is 0011 and it revises 0008 for now: 0009 and 0010 are being
added on another branch, and this revision is re-chained onto them at merge.

Revision ID: 0011
Revises: 0008
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0011'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'estimate_attributions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('estimate_id', sa.String(length=32), nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=False),
        sa.Column('authenticated', sa.Boolean(), nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('estimate_id', name='uq_estimate_attribution'),
    )

    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('token_id', sa.String(length=32), nullable=False),
        sa.Column('token_salt', sa.String(length=64), nullable=False),
        sa.Column('token_hash', sa.String(length=128), nullable=False),
        sa.Column('kdf', sa.String(length=32), nullable=False),
        sa.Column('kdf_iterations', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('deactivated_at', sa.DateTime(), nullable=True),
        sa.Column('deactivated_by', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', name='uq_user_id'),
        sa.UniqueConstraint('token_id', name='uq_user_token_id'),
    )

    op.create_table(
        'approval_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('event_id', sa.String(length=32), nullable=False),
        sa.Column('estimate_id', sa.String(length=32), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('actor_id', sa.String(length=64), nullable=False),
        sa.Column('actor_name', sa.String(length=200), nullable=False),
        sa.Column('actor_role', sa.String(length=16), nullable=True),
        sa.Column('authenticated', sa.Boolean(), nullable=False),
        sa.Column('comment', sa.String(length=2000), nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id', name='uq_approval_event_id'),
        sa.UniqueConstraint('estimate_id', 'sequence', name='uq_approval_event_sequence'),
    )
    op.create_index(
        op.f('ix_approval_events_estimate_id'), 'approval_events', ['estimate_id'], unique=False
    )

    op.create_table(
        'audit_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.Column('actor_id', sa.String(length=64), nullable=False),
        sa.Column('actor_role', sa.String(length=16), nullable=True),
        sa.Column('authenticated', sa.Boolean(), nullable=False),
        sa.Column('method', sa.String(length=8), nullable=False),
        sa.Column('route', sa.String(length=200), nullable=True),
        sa.Column('path', sa.String(length=500), nullable=False),
        sa.Column('target_id', sa.String(length=200), nullable=True),
        sa.Column('created_ids', sa.JSON(), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=False),
        sa.Column('outcome', sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_audit_events_actor_id'), 'audit_events', ['actor_id'], unique=False)
    op.create_index(
        op.f('ix_audit_events_target_id'), 'audit_events', ['target_id'], unique=False
    )


def downgrade() -> None:
    """Drops every user, approval event and audit row. Never run automatically."""
    op.drop_index(op.f('ix_audit_events_target_id'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_actor_id'), table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index(op.f('ix_approval_events_estimate_id'), table_name='approval_events')
    op.drop_table('approval_events')
    op.drop_table('users')
    op.drop_table('estimate_attributions')
