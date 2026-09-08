"""add issued quotations

One additive change: a new `quotations` table. Nothing existing is touched, so
every stored estimate keeps its exact contents and every historical record stays
valid.

The table holds only what a quotation adds to the estimate it presents — the
metadata a person typed onto the document, and the totals as issued. The figures
themselves are **not** copied: they are read back from the estimate's own
immutable snapshots every time the document is rendered, so there is one source
for them rather than two that could disagree. The stored totals are there to be
checked against a re-render, which is what makes "this is the same document"
verifiable rather than asserted.

`quote_number` is unique, and there is deliberately no update path anywhere in
the repository: an issued quotation is a document that was sent, and editing one
would mean changing something a customer already has. A correction is a new
quotation, exactly as a rate change is a new rate set version.

Creating a table is safe in SQLite and needs no batch mode.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'quotations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('quote_number', sa.String(length=64), nullable=False),
        sa.Column('estimate_id', sa.String(length=32), nullable=False),
        sa.Column('issued_at', sa.DateTime(), nullable=False),
        sa.Column('customer_name', sa.String(length=200), nullable=False),
        sa.Column('project_name', sa.String(length=200), nullable=False),
        sa.Column('quote_reference', sa.String(length=64), nullable=False),
        sa.Column('prepared_by', sa.String(length=200), nullable=False),
        sa.Column('notes', sa.String(length=2000), nullable=False),
        sa.Column('validity', sa.String(length=200), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('total_at_issue', sa.Float(), nullable=True),
        sa.Column('per_unit_at_issue', sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('quote_number', name='uq_quotation_number'),
    )
    op.create_index(
        op.f('ix_quotations_estimate_id'), 'quotations', ['estimate_id'], unique=False
    )
    op.create_index(
        op.f('ix_quotations_quote_number'), 'quotations', ['quote_number'], unique=False
    )


def downgrade() -> None:
    """Drops every issued quotation. Never run automatically."""
    op.drop_index(op.f('ix_quotations_quote_number'), table_name='quotations')
    op.drop_index(op.f('ix_quotations_estimate_id'), table_name='quotations')
    op.drop_table('quotations')
