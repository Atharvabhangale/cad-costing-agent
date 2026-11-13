"""add emission-factor catalogs for the CO2e estimate

One additive change: a new `emission_factor_catalogs` table, mirroring
`dfm_profiles` (identity in indexed columns, the factors and their cited sources
in one JSON column, unique on `(emission_catalog_id, version)` so a published
version cannot be replaced). Nothing existing is touched, so every stored
estimate, rate set, profile, quotation and actual keeps its exact contents. A
CO2e figure is computed on request from an estimate's own snapshots and is not
stored.

This revises 0008 directly. Revision id 0009 is deliberately unused: the
factory label and per-machine overhead of Iteration 9 live in a rate set
version's existing JSON `rates` column, so they needed no schema change.

Revision ID: 0010
Revises: 0008
Create Date: 2026-11-13
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0010'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'emission_factor_catalogs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('emission_catalog_id', sa.String(length=64), nullable=False),
        sa.Column('version', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('description', sa.String(length=512), nullable=False),
        sa.Column('is_illustrative', sa.Boolean(), nullable=False),
        sa.Column('factors', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('emission_catalog_id', 'version', name='uq_emission_catalog_identity'),
    )
    op.create_index(
        op.f('ix_emission_factor_catalogs_emission_catalog_id'),
        'emission_factor_catalogs',
        ['emission_catalog_id'],
        unique=False,
    )


def downgrade() -> None:
    """Drops every stored emission-factor catalog. Never run automatically."""
    op.drop_index(
        op.f('ix_emission_factor_catalogs_emission_catalog_id'),
        table_name='emission_factor_catalogs',
    )
    op.drop_table('emission_factor_catalogs')
