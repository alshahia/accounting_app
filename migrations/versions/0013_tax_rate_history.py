"""effective-dated tax rates

Revision ID: 0013_tax_rate_history
Revises: 0012_posting_mapping
Create Date: 2026-10-22 09:50:00

Tax rates are data, not code (analysis/20 §5 and §9), and a rate change must
not rewrite what an already-dated document charged. Each tax code gets a rate
history keyed by effective date; posting resolves the rate on the document
date. Existing codes are backfilled with their current rate from 2000-01-01 so
no document changes behaviour on upgrade.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_tax_rate_history"
down_revision: str | Sequence[str] | None = "0012_posting_mapping"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Create the rate history table, backfill it and protect it with RLS."""
    op.create_table(
        "tax_code_rate",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tax_code_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("rate_basis_points", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("updated_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("row_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tax_code_rate"),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "tax_code_id",
            "valid_from",
            name="uq_tax_code_rate_tenant_company_code_from",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_tax_code_rate_tax_code",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "rate_basis_points >= 0 AND rate_basis_points <= 10000",
            name="ck_tax_code_rate_value_range",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_tax_code_rate_valid_range",
        ),
    )
    op.create_index("ix_tax_code_rate_tenant_id", "tax_code_rate", ["tenant_id"])
    op.create_index("ix_tax_code_rate_company_id", "tax_code_rate", ["company_id"])
    op.create_index("ix_tax_code_rate_tax_code_id", "tax_code_rate", ["tax_code_id"])
    op.create_index(
        "ix_tax_code_rate_lookup",
        "tax_code_rate",
        ["tenant_id", "company_id", "tax_code_id", "valid_from"],
    )
    op.execute("ALTER TABLE public.tax_code_rate ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tax_code_rate FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON public.tax_code_rate FOR ALL TO PUBLIC "
        f"USING ({TENANT_EXPRESSION}) WITH CHECK ({TENANT_EXPRESSION})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.tax_code_rate TO ameen_app")
    op.execute(
        "INSERT INTO tax_code_rate "
        "(tenant_id, company_id, tax_code_id, rate_basis_points, valid_from, created_at, "
        "updated_at, row_version) "
        "SELECT tenant_id, company_id, id, rate_basis_points, DATE '2000-01-01', now(), now(), 1 "
        "FROM tax_code"
    )


def downgrade() -> None:
    """Drop the tax rate history table."""
    op.drop_table("tax_code_rate")
