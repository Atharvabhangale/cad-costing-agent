"""say what kind of figure a recorded cost is: an actual or a supplier quote

Three additive columns on `cost_actuals`:

- `figure_kind` — `actual` or `supplier_quote`. Not nullable and with no default:
  like `cost_basis`, the kind is stated by whoever records the figure.
- `supplier_name`, `quote_reference` — nullable free text, for a quote only.

The existing `source` column is untouched. It is the free text saying where a
figure came from, which is a different question from what kind of figure it is.

Every row that already exists becomes `actual`. That is what each of them was
entered as and what the application has shown it as: before this revision the
only thing that could be recorded was an actual cost, and its variance was
worded as one. A row that was in truth a quotation is put right the way any
record is — by a correction naming it — not by this migration guessing.

Done in three steps so that no default is left behind in the schema: the column
is added nullable, the existing rows are filled, and only then is it made
required. SQLite cannot alter a column in place, so the last step runs in batch
mode, which rebuilds the table and copies every row across.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-12
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('cost_actuals', sa.Column('figure_kind', sa.String(length=16), nullable=True))
    op.add_column('cost_actuals', sa.Column('supplier_name', sa.String(length=200), nullable=True))
    op.add_column(
        'cost_actuals', sa.Column('quote_reference', sa.String(length=100), nullable=True)
    )
    op.execute("UPDATE cost_actuals SET figure_kind = 'actual' WHERE figure_kind IS NULL")
    with op.batch_alter_table('cost_actuals', schema=None) as batch_op:
        batch_op.alter_column('figure_kind', existing_type=sa.String(length=16), nullable=False)


def downgrade() -> None:
    """Removes the columns, and with them which records were quotes. Never run automatically."""
    with op.batch_alter_table('cost_actuals', schema=None) as batch_op:
        batch_op.drop_column('quote_reference')
        batch_op.drop_column('supplier_name')
        batch_op.drop_column('figure_kind')
