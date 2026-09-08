"""add time-assumption profiles

Two additive changes, neither of which touches an existing row's contents:

- a new `time_profiles` table, mirroring `rate_sets`: identity in indexed
  columns, the parameters in one JSON column, unique on
  `(time_profile_id, version)` so a published version cannot be replaced;
- a nullable `time_profile_snapshot` JSON column on `estimates`, holding the
  profile(s) an estimate's predicted times were made under.

The snapshot column is nullable with no server default, so every stored estimate
keeps its exact current contents and reads back `None`. Estimates made before
Step 21 were made under the built-in baseline rather than a published profile,
and `None` says that truthfully instead of back-filling a profile they never
referenced.

Creating a table is safe in SQLite; the column addition uses batch mode, which
rewrites `estimates` and copies every existing row across.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'time_profiles',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('time_profile_id', sa.String(length=64), nullable=False),
        sa.Column('version', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('description', sa.String(length=512), nullable=False),
        sa.Column('is_illustrative', sa.Boolean(), nullable=False),
        sa.Column('assumptions', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('time_profile_id', 'version', name='uq_time_profile_identity'),
    )
    op.create_index(
        op.f('ix_time_profiles_time_profile_id'), 'time_profiles', ['time_profile_id'], unique=False
    )

    with op.batch_alter_table('estimates', schema=None) as batch_op:
        batch_op.add_column(sa.Column('time_profile_snapshot', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Drops the profiles table and every stored profile. Never run automatically."""
    with op.batch_alter_table('estimates', schema=None) as batch_op:
        batch_op.drop_column('time_profile_snapshot')

    op.drop_index(op.f('ix_time_profiles_time_profile_id'), table_name='time_profiles')
    op.drop_table('time_profiles')
