"""add DFM threshold profiles

One additive change: a new `dfm_profiles` table, mirroring `time_profiles`
(identity in indexed columns, the thresholds and their cited sources in one JSON
column, unique on `(dfm_profile_id, version)` so a published version cannot be
replaced). Nothing existing is touched, so every stored estimate, rate set, time
profile, quotation and actual keeps its exact contents. DFM flags are advisory
and are not stored against estimates.

Creating a table is safe in SQLite and needs no batch mode.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-27
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'dfm_profiles',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('dfm_profile_id', sa.String(length=64), nullable=False),
        sa.Column('version', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('description', sa.String(length=512), nullable=False),
        sa.Column('is_illustrative', sa.Boolean(), nullable=False),
        sa.Column('rules', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dfm_profile_id', 'version', name='uq_dfm_profile_identity'),
    )
    op.create_index(
        op.f('ix_dfm_profiles_dfm_profile_id'), 'dfm_profiles', ['dfm_profile_id'], unique=False
    )


def downgrade() -> None:
    """Drops every stored DFM profile. Never run automatically."""
    op.drop_index(op.f('ix_dfm_profiles_dfm_profile_id'), table_name='dfm_profiles')
    op.drop_table('dfm_profiles')
