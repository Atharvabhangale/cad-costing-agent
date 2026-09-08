"""initial schema

Captures the schema as it stood at the end of Step 11: `rate_sets` keyed by a
unique `(rate_set_id, version)`, and `estimates` with an autoincrement `id`
primary key plus a unique, indexed `estimate_id`.

A database created before Alembic was adopted already has exactly this shape,
so `db.run_migrations` stamps such a database at this revision rather than
re-running it. Nothing here alters or drops existing data.

Revision ID: 0001
Revises:
Create Date: 2026-08-31
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('estimates',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('estimate_id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('part_id', sa.String(length=64), nullable=False),
    sa.Column('part_name', sa.String(length=128), nullable=False),
    sa.Column('material', sa.String(length=64), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('rate_set_id', sa.String(length=64), nullable=True),
    sa.Column('rate_set_version', sa.String(length=32), nullable=True),
    sa.Column('total_material_cost', sa.Float(), nullable=False),
    sa.Column('total_operation_cost', sa.Float(), nullable=False),
    sa.Column('total_additional_cost', sa.Float(), nullable=False),
    sa.Column('total_part_cost', sa.Float(), nullable=False),
    sa.Column('request_snapshot', sa.JSON(), nullable=False),
    sa.Column('result_snapshot', sa.JSON(), nullable=False),
    sa.Column('rate_set_snapshot', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('estimates', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_estimates_estimate_id'), ['estimate_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_estimates_part_id'), ['part_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_estimates_rate_set_id'), ['rate_set_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_estimates_source'), ['source'], unique=False)

    op.create_table('rate_sets',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('rate_set_id', sa.String(length=64), nullable=False),
    sa.Column('version', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('description', sa.String(length=512), nullable=False),
    sa.Column('is_illustrative', sa.Boolean(), nullable=False),
    sa.Column('rates', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('rate_set_id', 'version', name='uq_rate_set_identity')
    )
    with op.batch_alter_table('rate_sets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_rate_sets_rate_set_id'), ['rate_set_id'], unique=False)



def downgrade() -> None:
    """Drops both tables and every row in them. Never run automatically."""
    with op.batch_alter_table('rate_sets', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_rate_sets_rate_set_id'))

    op.drop_table('rate_sets')
    with op.batch_alter_table('estimates', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_estimates_source'))
        batch_op.drop_index(batch_op.f('ix_estimates_rate_set_id'))
        batch_op.drop_index(batch_op.f('ix_estimates_part_id'))
        batch_op.drop_index(batch_op.f('ix_estimates_estimate_id'))

    op.drop_table('estimates')
