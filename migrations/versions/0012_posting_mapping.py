"""effective-dated posting account mapping

Revision ID: 0012_posting_mapping
Revises: 0011_mfa
Create Date: 2026-10-21 15:50:00

Phase 1 posted documents to fixed chart codes. Settings must decide which
account each posting role uses, per company, without a code change, and keep
the history so an old document still explains the account it used. Existing
companies are backfilled from their current default chart.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_posting_mapping"
down_revision: str | Sequence[str] | None = "0011_mfa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
BACKFILL = (
    ("accounts_receivable", "1100"),
    ("inventory", "1200"),
    ("cheques_under_collection", "1230"),
    ("withholding_receivable", "1250"),
    ("supplier_advances", "1400"),
    ("accounts_payable", "2000"),
    ("vat_payable", "2100"),
    ("withholding_payable", "2150"),
    ("cheques_issued", "2200"),
    ("customer_advances", "2300"),
    ("sales_revenue", "4000"),
    ("fx_gain", "4800"),
    ("cost_of_sales", "5100"),
    ("fx_loss", "5800"),
)


def upgrade() -> None:
    """Create the mapping table, backfill it and protect it with RLS."""
    op.create_table(
        "posting_mapping",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(50), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_posting_mapping"),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "key",
            "valid_from",
            name="uq_posting_mapping_tenant_company_key_from",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_posting_mapping_tenant_company",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "account_id"],
            ["account.tenant_id", "account.company_id", "account.id"],
            name="fk_posting_mapping_account",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_posting_mapping_valid_range",
        ),
    )
    op.create_index("ix_posting_mapping_tenant_id", "posting_mapping", ["tenant_id"])
    op.create_index("ix_posting_mapping_company_id", "posting_mapping", ["company_id"])
    op.create_index(
        "ix_posting_mapping_lookup",
        "posting_mapping",
        ["tenant_id", "company_id", "key", "valid_from"],
    )
    op.execute("ALTER TABLE public.posting_mapping ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.posting_mapping FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON public.posting_mapping FOR ALL TO PUBLIC "
        f"USING ({TENANT_EXPRESSION}) WITH CHECK ({TENANT_EXPRESSION})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.posting_mapping TO ameen_app")
    for key, code in BACKFILL:
        op.execute(
            sa.text(
                "INSERT INTO posting_mapping "
                "(tenant_id, company_id, key, account_id, valid_from, created_at, updated_at, "
                "row_version) "
                "SELECT a.tenant_id, a.company_id, :key, a.id, DATE '2000-01-01', now(), now(), 1 "
                "FROM account a WHERE a.code = :code "
                "ON CONFLICT ON CONSTRAINT uq_posting_mapping_tenant_company_key_from DO NOTHING"
            ).bindparams(key=key, code=code)
        )


def downgrade() -> None:
    """Drop the posting mapping table."""
    op.drop_table("posting_mapping")
