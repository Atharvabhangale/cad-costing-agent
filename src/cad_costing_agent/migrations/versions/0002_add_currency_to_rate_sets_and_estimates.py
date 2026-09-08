"""add currency to rate sets and estimates

Adds a 3-letter currency code to both tables with `server_default='INR'`, so
every row that already exists is backfilled as INR-denominated in place. This
is purely additive: no table is dropped or recreated and no stored rate set or
estimate is modified. SQLite cannot ALTER in place, so batch mode rewrites each
table, copying every existing row across.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-31
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('estimates', schema=None) as batch_op:
        batch_op.add_column(sa.Column('currency', sa.String(length=3), server_default='INR', nullable=False))

    with op.batch_alter_table('rate_sets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('currency', sa.String(length=3), server_default='INR', nullable=False))



def downgrade() -> None:
    """Removes the currency columns. Never run automatically."""
    with op.batch_alter_table('rate_sets', schema=None) as batch_op:
        batch_op.drop_column('currency')

    with op.batch_alter_table('estimates', schema=None) as batch_op:
        batch_op.drop_column('currency')

