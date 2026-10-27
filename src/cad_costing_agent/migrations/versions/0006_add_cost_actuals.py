"""add actual costs recorded against estimates

One additive change: a new `cost_actuals` table. Nothing existing is touched, so
every stored estimate, rate set, time profile and quotation keeps its exact
contents.

A row is a cost somebody outside this application stated for an estimate's part
— an ERP actual, a historical quotation — with where it came from. It is never
read by costing. Only the total is required; the breakdown columns are nullable
because "not supplied" is a different statement from zero.

There is no update path in the repository. A correction is a new row naming the
one it supersedes, and `supersedes_actual_id` is unique so each record is
corrected at most once.

Creating a table is safe in SQLite and needs no batch mode.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'cost_actuals',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('actual_id', sa.String(length=32), nullable=False),
        sa.Column('estimate_id', sa.String(length=32), nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.Column('source', sa.String(length=200), nullable=False),
        sa.Column('cost_basis', sa.String(length=16), nullable=False),
        sa.Column('batch_quantity', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('total_cost', sa.Float(), nullable=False),
        sa.Column('material_cost', sa.Float(), nullable=True),
        sa.Column('machining_cost', sa.Float(), nullable=True),
        sa.Column('additional_cost', sa.Float(), nullable=True),
        sa.Column('cost_date', sa.Date(), nullable=True),
        sa.Column('client_part_number', sa.String(length=64), nullable=True),
        sa.Column('drawing_revision', sa.String(length=32), nullable=True),
        sa.Column('notes', sa.String(length=2000), nullable=False),
        sa.Column('supersedes_actual_id', sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('actual_id', name='uq_cost_actual_id'),
        sa.UniqueConstraint('supersedes_actual_id', name='uq_cost_actual_supersedes'),
    )
    op.create_index(
        op.f('ix_cost_actuals_estimate_id'), 'cost_actuals', ['estimate_id'], unique=False
    )


def downgrade() -> None:
    """Drops every recorded actual cost. Never run automatically."""
    op.drop_index(op.f('ix_cost_actuals_estimate_id'), table_name='cost_actuals')
    op.drop_table('cost_actuals')
