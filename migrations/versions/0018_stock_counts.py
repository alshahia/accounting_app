"""Physical stock counts and inventory adjustments

Revision ID: 0018_stock_counts
Revises: 0017_inventory_warehouses
Create Date: 2026-10-22 15:30:00

analysis/20 §5 lists stock counts as part of v1 inventory. A count is a draft
document for one warehouse, editable until posting; posting writes the variance
to the per-warehouse balance and the item total and posts one journal entry
against the configurable inventory-adjustment role. The stock-move type check
gains 'count_adjustment' so the stock-movement report shows the adjustment.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_stock_counts"
down_revision: str | Sequence[str] | None = "0017_inventory_warehouses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON public."
        f"{table} FOR ALL TO PUBLIC USING ({TENANT_EXPRESSION}) "
        f"WITH CHECK ({TENANT_EXPRESSION})"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.{table} TO ameen_app")


def upgrade() -> None:
    """Create the count document and allow count-adjustment stock moves."""
    op.create_table(
        "stock_count",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("number", sa.String(length=30), nullable=False),
        sa.Column("count_date", sa.Date(), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("memo", sa.String(length=300), nullable=True),
        sa.Column("journal_entry_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_stock_count"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_stock_count_tenant_company_number"
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_stock_count_tenant_company_id"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "warehouse_id"],
            ["warehouse.tenant_id", "warehouse.company_id", "warehouse.id"],
            name="fk_stock_count_warehouse",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_stock_count_journal_entry",
        ),
        sa.CheckConstraint("status IN ('draft', 'posted')", name="ck_stock_count_status"),
    )
    op.create_index("ix_stock_count_tenant_id", "stock_count", ["tenant_id"])
    op.create_index("ix_stock_count_company_id", "stock_count", ["company_id"])
    op.create_index(
        "ix_stock_count_tenant_company_date",
        "stock_count",
        ["tenant_id", "company_id", "count_date"],
    )
    _enable_rls("stock_count")

    op.create_table(
        "stock_count_line",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("stock_count_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("counted_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("system_quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "variance_quantity",
            sa.Numeric(precision=18, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column("variance_value_minor", sa.BigInteger(), server_default="0", nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_stock_count_line"),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "stock_count_id",
            "item_id",
            name="uq_stock_count_line_tenant_company_count_item",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "stock_count_id"],
            ["stock_count.tenant_id", "stock_count.company_id", "stock_count.id"],
            name="fk_stock_count_line_count",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_stock_count_line_item",
        ),
        sa.CheckConstraint(
            "counted_quantity >= 0", name="ck_stock_count_line_counted_non_negative"
        ),
        sa.CheckConstraint("system_quantity >= 0", name="ck_stock_count_line_system_non_negative"),
    )
    op.create_index("ix_stock_count_line_tenant_id", "stock_count_line", ["tenant_id"])
    op.create_index("ix_stock_count_line_company_id", "stock_count_line", ["company_id"])
    op.create_index(
        "ix_stock_count_line_count",
        "stock_count_line",
        ["tenant_id", "company_id", "stock_count_id"],
    )
    _enable_rls("stock_count_line")

    op.drop_constraint("ck_stock_move_type_allowed", "stock_move", type_="check")
    op.create_check_constraint(
        "ck_stock_move_type_allowed",
        "stock_move",
        "move_type IN ('receipt', 'issue', 'count_adjustment')",
    )


def downgrade() -> None:
    """Drop count documents and restore the receipt/issue move check."""
    op.drop_constraint("ck_stock_move_type_allowed", "stock_move", type_="check")
    op.execute("DELETE FROM public.stock_move WHERE move_type = 'count_adjustment'")
    op.create_check_constraint(
        "ck_stock_move_type_allowed",
        "stock_move",
        "move_type IN ('receipt', 'issue')",
    )
    op.drop_table("stock_count_line")
    op.drop_table("stock_count")
