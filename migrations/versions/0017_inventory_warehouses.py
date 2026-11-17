"""Warehouses, per-warehouse stock balances and inter-warehouse transfers

Revision ID: 0017_inventory_warehouses
Revises: 0016_mfa_recovery_codes
Create Date: 2026-10-22 12:10:00

analysis/20 §5 requires warehouses and stock moves per location in v1. The
migration adds a warehouse master (one default per company), backfills the
single implicit warehouse every company used so far, gives stock_move its
warehouse, and adds per-warehouse balances plus a transfer document. Item
quantity/value stay the company total so posted inventory value still matches
the GL inventory account by construction.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_inventory_warehouses"
down_revision: str | Sequence[str] | None = "0016_mfa_recovery_codes"
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
    """Create warehouses, balances and transfers, and backfill the old stock model."""
    op.create_table(
        "warehouse",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name_ar", sa.String(length=200), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_warehouse"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "code", name="uq_warehouse_tenant_company_code"
        ),
        sa.UniqueConstraint("tenant_id", "company_id", "id", name="uq_warehouse_tenant_company_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id"],
            ["company.tenant_id", "company.id"],
            name="fk_warehouse_tenant_company",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_warehouse_tenant_id", "warehouse", ["tenant_id"])
    op.create_index("ix_warehouse_company_id", "warehouse", ["company_id"])
    op.create_index("ix_warehouse_tenant_company", "warehouse", ["tenant_id", "company_id"])
    _enable_rls("warehouse")
    op.execute(
        """
        INSERT INTO public.warehouse
            (tenant_id, company_id, code, name_ar, name_en, is_default, is_active)
        SELECT tenant_id, id, 'MAIN', 'المستودع الرئيسي', 'Main warehouse', true, true
          FROM public.company
        """
    )

    op.add_column("stock_move", sa.Column("warehouse_id", sa.Uuid(as_uuid=True), nullable=True))
    op.execute(
        """
        UPDATE public.stock_move AS move
           SET warehouse_id = warehouse.id
          FROM public.warehouse AS warehouse
         WHERE warehouse.tenant_id = move.tenant_id
           AND warehouse.company_id = move.company_id
           AND warehouse.is_default
        """
    )
    op.alter_column("stock_move", "warehouse_id", nullable=False)
    op.create_foreign_key(
        "fk_stock_move_warehouse",
        "stock_move",
        "warehouse",
        ["tenant_id", "company_id", "warehouse_id"],
        ["tenant_id", "company_id", "id"],
    )
    op.create_index(
        "ix_stock_move_warehouse", "stock_move", ["tenant_id", "company_id", "warehouse_id"]
    )

    op.create_table(
        "stock_balance",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("warehouse_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "quantity", sa.Numeric(precision=18, scale=4), server_default="0", nullable=False
        ),
        sa.Column("value_minor", sa.BigInteger(), server_default="0", nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_stock_balance"),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "warehouse_id",
            "item_id",
            name="uq_stock_balance_tenant_company_warehouse_item",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "warehouse_id"],
            ["warehouse.tenant_id", "warehouse.company_id", "warehouse.id"],
            name="fk_stock_balance_warehouse",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_stock_balance_item",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("quantity >= 0", name="ck_stock_balance_quantity_non_negative"),
        sa.CheckConstraint("value_minor >= 0", name="ck_stock_balance_value_non_negative"),
    )
    op.create_index("ix_stock_balance_tenant_id", "stock_balance", ["tenant_id"])
    op.create_index("ix_stock_balance_company_id", "stock_balance", ["company_id"])
    op.create_index(
        "ix_stock_balance_warehouse", "stock_balance", ["tenant_id", "company_id", "warehouse_id"]
    )
    op.create_index(
        "ix_stock_balance_item", "stock_balance", ["tenant_id", "company_id", "item_id"]
    )
    _enable_rls("stock_balance")
    op.execute(
        """
        INSERT INTO public.stock_balance
            (tenant_id, company_id, warehouse_id, item_id, quantity, value_minor)
        SELECT item.tenant_id,
               item.company_id,
               warehouse.id,
               item.id,
               item.quantity_on_hand,
               item.stock_value_minor
          FROM public.item AS item
          JOIN public.warehouse AS warehouse
            ON warehouse.tenant_id = item.tenant_id
           AND warehouse.company_id = item.company_id
           AND warehouse.is_default
        """
    )

    op.create_table(
        "stock_transfer",
        sa.Column("id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("number", sa.String(length=30), nullable=False),
        sa.Column("transfer_date", sa.Date(), nullable=False),
        sa.Column("from_warehouse_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("to_warehouse_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("value_minor", sa.BigInteger(), nullable=False),
        sa.Column("memo", sa.String(length=300), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_stock_transfer"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_stock_transfer_tenant_company_number"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "from_warehouse_id"],
            ["warehouse.tenant_id", "warehouse.company_id", "warehouse.id"],
            name="fk_stock_transfer_from_warehouse",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "to_warehouse_id"],
            ["warehouse.tenant_id", "warehouse.company_id", "warehouse.id"],
            name="fk_stock_transfer_to_warehouse",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_stock_transfer_item",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_stock_transfer_quantity_positive"),
        sa.CheckConstraint(
            "from_warehouse_id <> to_warehouse_id", name="ck_stock_transfer_different_warehouses"
        ),
    )
    op.create_index("ix_stock_transfer_tenant_id", "stock_transfer", ["tenant_id"])
    op.create_index("ix_stock_transfer_company_id", "stock_transfer", ["company_id"])
    op.create_index(
        "ix_stock_transfer_tenant_company_date",
        "stock_transfer",
        ["tenant_id", "company_id", "transfer_date"],
    )
    _enable_rls("stock_transfer")


def downgrade() -> None:
    """Drop transfers, balances, the stock-move warehouse and the master."""
    op.drop_table("stock_transfer")
    op.drop_table("stock_balance")
    op.drop_index("ix_stock_move_warehouse", table_name="stock_move")
    op.drop_constraint("fk_stock_move_warehouse", "stock_move", type_="foreignkey")
    op.drop_column("stock_move", "warehouse_id")
    op.drop_table("warehouse")
