"""add manufacturing plan snapshot to estimates

Adds one nullable JSON column, `plan_snapshot`, holding the manufacturing plan
and its operation feedback as they stood when the estimate was created.

Purely additive and safe on an existing database: the column is nullable with no
server default, so every stored estimate keeps its exact current contents and
simply reads back `plan_snapshot = None`. Estimates made before Step 20 had no
plan, and `None` says that truthfully rather than inventing an empty one.

A JSON column rather than child tables, matching how `request_snapshot`,
`result_snapshot` and `rate_set_snapshot` already work: a plan is written once
with its estimate, is never updated in place, and is always read whole. Splitting
it into operation and feedback tables would add joins and a partial-update path
for something that is by design immutable. SQLite cannot ALTER in place, so batch
mode rewrites the table, copying every existing row across.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('estimates', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plan_snapshot', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Removes the plan snapshot column, discarding every stored plan. Never run automatically."""
    with op.batch_alter_table('estimates', schema=None) as batch_op:
        batch_op.drop_column('plan_snapshot')
