"""master data, sales invoices, purchase bills and stock moves

Revision ID: 0006_documents
Revises: 0005_posting
Create Date: 2026-10-21 13:30:00

Day 7: the invoice/bill slice needs master data (customer, supplier, item, tax
code), two document tables with their lines, and weighted-average stock moves.
All money columns are integer minor units; quantities are NUMERIC(18, 4); a
document carries both its currency amounts and its base-currency amounts. Every
table is tenant-scoped with the same fail-closed RLS policy and is granted to
the application role.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_documents"
down_revision: str | Sequence[str] | None = "0005_posting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
RLS_TABLES = (
    "customer",
    "supplier",
    "tax_code",
    "item",
    "sales_invoice",
    "sales_invoice_line",
    "purchase_bill",
    "purchase_bill_line",
    "stock_move",
)


def _id_column() -> sa.Column[object]:
    return sa.Column(
        "id", sa.Uuid(as_uuid=True), server_default=sa.text("uuidv7()"), nullable=False
    )


def _tenant_company() -> list[sa.Column[object]]:
    return [
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
    ]


def _timestamps() -> list[sa.Column[object]]:
    return [
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
    ]


def _audit_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("updated_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("row_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
    ]


def _company_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "company_id"],
        ["company.tenant_id", "company.id"],
        name=f"fk_{table}_tenant_company",
        ondelete="CASCADE",
    )


def _create_master_tables() -> None:
    op.create_table(
        "customer",
        _id_column(),
        *_tenant_company(),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name_ar", sa.String(length=200), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("tax_number", sa.String(length=50), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_customer"),
        _company_fk("customer"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "code", name="uq_customer_tenant_company_code"
        ),
        sa.UniqueConstraint("tenant_id", "company_id", "id", name="uq_customer_tenant_company_id"),
    )
    op.create_index("ix_customer_tenant_id", "customer", ["tenant_id"])
    op.create_index("ix_customer_company_id", "customer", ["company_id"])
    op.create_index("ix_customer_tenant_company", "customer", ["tenant_id", "company_id"])

    op.create_table(
        "supplier",
        _id_column(),
        *_tenant_company(),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name_ar", sa.String(length=200), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("tax_number", sa.String(length=50), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_supplier"),
        _company_fk("supplier"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "code", name="uq_supplier_tenant_company_code"
        ),
        sa.UniqueConstraint("tenant_id", "company_id", "id", name="uq_supplier_tenant_company_id"),
    )
    op.create_index("ix_supplier_tenant_id", "supplier", ["tenant_id"])
    op.create_index("ix_supplier_company_id", "supplier", ["company_id"])
    op.create_index("ix_supplier_tenant_company", "supplier", ["tenant_id", "company_id"])

    op.create_table(
        "tax_code",
        _id_column(),
        *_tenant_company(),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name_ar", sa.String(length=200), nullable=False),
        sa.Column("rate_basis_points", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_tax_code"),
        _company_fk("tax_code"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "code", name="uq_tax_code_tenant_company_code"
        ),
        sa.UniqueConstraint("tenant_id", "company_id", "id", name="uq_tax_code_tenant_company_id"),
        sa.CheckConstraint(
            "rate_basis_points >= 0 AND rate_basis_points <= 10000",
            name="ck_tax_code_rate_range",
        ),
    )
    op.create_index("ix_tax_code_tenant_id", "tax_code", ["tenant_id"])
    op.create_index("ix_tax_code_company_id", "tax_code", ["company_id"])
    op.create_index("ix_tax_code_tenant_company", "tax_code", ["tenant_id", "company_id"])

    op.create_table(
        "item",
        _id_column(),
        *_tenant_company(),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name_ar", sa.String(length=200), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("unit_of_measure", sa.String(length=20), nullable=False),
        sa.Column(
            "quantity_on_hand", sa.Numeric(18, 4), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "stock_value_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_item"),
        _company_fk("item"),
        sa.UniqueConstraint("tenant_id", "company_id", "code", name="uq_item_tenant_company_code"),
        sa.UniqueConstraint("tenant_id", "company_id", "id", name="uq_item_tenant_company_id"),
        sa.CheckConstraint("quantity_on_hand >= 0", name="ck_item_quantity_non_negative"),
        sa.CheckConstraint("stock_value_minor >= 0", name="ck_item_value_non_negative"),
    )
    op.create_index("ix_item_tenant_id", "item", ["tenant_id"])
    op.create_index("ix_item_company_id", "item", ["company_id"])
    op.create_index("ix_item_tenant_company", "item", ["tenant_id", "company_id"])


def _invoice_columns(prefix: str, party: str) -> list[sa.Column[object]]:
    return [
        *_tenant_company(),
        sa.Column("number", sa.String(length=40), nullable=False),
        sa.Column(f"{party}_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "invoice_date" if prefix == "sales_invoice" else "bill_date", sa.Date(), nullable=False
        ),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(18, 8), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("subtotal_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("tax_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("total_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "base_subtotal_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("base_tax_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("base_total_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("journal_entry_id", sa.Uuid(as_uuid=True), nullable=True),
        *_timestamps(),
        *_audit_columns(),
    ]


def _line_columns(prefix: str) -> list[sa.Column[object]]:
    return [
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "invoice_id" if prefix == "sales_invoice" else "bill_id",
            sa.Uuid(as_uuid=True),
            nullable=False,
        ),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("tax_code_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("line_subtotal_minor", sa.BigInteger(), nullable=False),
        sa.Column("line_tax_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("line_total_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_line_subtotal_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "base_line_tax_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        *_timestamps(),
    ]


def _create_sales_tables() -> None:
    op.create_table(
        "sales_invoice",
        _id_column(),
        *_invoice_columns("sales_invoice", "customer"),
        sa.Column("cogs_entry_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sales_invoice"),
        _company_fk("sales_invoice"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "customer_id"],
            ["customer.tenant_id", "customer.company_id", "customer.id"],
            name="fk_sales_invoice_customer",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_sales_invoice_journal_entry",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_sales_invoice_tenant_company_number"
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_sales_invoice_tenant_company_id"
        ),
        sa.CheckConstraint("status IN ('draft', 'posted')", name="ck_sales_invoice_status_allowed"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_sales_invoice_currency_iso"),
        sa.CheckConstraint("exchange_rate > 0", name="ck_sales_invoice_exchange_rate_positive"),
        sa.CheckConstraint("subtotal_minor >= 0", name="ck_sales_invoice_subtotal_non_negative"),
        sa.CheckConstraint("tax_minor >= 0", name="ck_sales_invoice_tax_non_negative"),
        sa.CheckConstraint("total_minor >= 0", name="ck_sales_invoice_total_non_negative"),
        sa.CheckConstraint(
            "total_minor = subtotal_minor + tax_minor", name="ck_sales_invoice_total_consistent"
        ),
        sa.CheckConstraint(
            "base_total_minor = base_subtotal_minor + base_tax_minor",
            name="ck_sales_invoice_base_total_consistent",
        ),
    )
    op.create_index("ix_sales_invoice_tenant_id", "sales_invoice", ["tenant_id"])
    op.create_index("ix_sales_invoice_company_id", "sales_invoice", ["company_id"])
    op.create_index(
        "ix_sales_invoice_tenant_company_date",
        "sales_invoice",
        ["tenant_id", "company_id", "invoice_date"],
    )
    op.create_index(
        "ix_sales_invoice_customer", "sales_invoice", ["tenant_id", "company_id", "customer_id"]
    )

    op.create_table(
        "sales_invoice_line",
        _id_column(),
        *_line_columns("sales_invoice"),
        sa.PrimaryKeyConstraint("id", name="pk_sales_invoice_line"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "invoice_id"],
            ["sales_invoice.tenant_id", "sales_invoice.company_id", "sales_invoice.id"],
            name="fk_sales_invoice_line_invoice",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_sales_invoice_line_item",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_sales_invoice_line_tax_code",
        ),
        sa.UniqueConstraint(
            "tenant_id", "invoice_id", "line_number", name="uq_sales_invoice_line_number"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_sales_invoice_line_tenant_id"),
        sa.CheckConstraint("quantity > 0", name="ck_sales_invoice_line_quantity_positive"),
        sa.CheckConstraint(
            "unit_price_minor >= 0", name="ck_sales_invoice_line_price_non_negative"
        ),
        sa.CheckConstraint(
            "line_subtotal_minor >= 0 AND line_tax_minor >= 0",
            name="ck_sales_invoice_line_amounts_non_negative",
        ),
        sa.CheckConstraint(
            "line_total_minor = line_subtotal_minor + line_tax_minor",
            name="ck_sales_invoice_line_total_consistent",
        ),
    )
    op.create_index(
        "ix_sales_invoice_line_invoice", "sales_invoice_line", ["tenant_id", "invoice_id"]
    )


def _create_purchase_tables() -> None:
    op.create_table(
        "purchase_bill",
        _id_column(),
        *_invoice_columns("purchase_bill", "supplier"),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_purchase_bill"),
        _company_fk("purchase_bill"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "supplier_id"],
            ["supplier.tenant_id", "supplier.company_id", "supplier.id"],
            name="fk_purchase_bill_supplier",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_purchase_bill_journal_entry",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_purchase_bill_tenant_company_number"
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_purchase_bill_tenant_company_id"
        ),
        sa.CheckConstraint("status IN ('draft', 'posted')", name="ck_purchase_bill_status_allowed"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_purchase_bill_currency_iso"),
        sa.CheckConstraint("exchange_rate > 0", name="ck_purchase_bill_exchange_rate_positive"),
        sa.CheckConstraint("subtotal_minor >= 0", name="ck_purchase_bill_subtotal_non_negative"),
        sa.CheckConstraint("tax_minor >= 0", name="ck_purchase_bill_tax_non_negative"),
        sa.CheckConstraint("total_minor >= 0", name="ck_purchase_bill_total_non_negative"),
        sa.CheckConstraint(
            "total_minor = subtotal_minor + tax_minor", name="ck_purchase_bill_total_consistent"
        ),
        sa.CheckConstraint(
            "base_total_minor = base_subtotal_minor + base_tax_minor",
            name="ck_purchase_bill_base_total_consistent",
        ),
    )
    op.create_index("ix_purchase_bill_tenant_id", "purchase_bill", ["tenant_id"])
    op.create_index("ix_purchase_bill_company_id", "purchase_bill", ["company_id"])
    op.create_index(
        "ix_purchase_bill_tenant_company_date",
        "purchase_bill",
        ["tenant_id", "company_id", "bill_date"],
    )
    op.create_index(
        "ix_purchase_bill_supplier", "purchase_bill", ["tenant_id", "company_id", "supplier_id"]
    )

    op.create_table(
        "purchase_bill_line",
        _id_column(),
        *_line_columns("purchase_bill"),
        sa.PrimaryKeyConstraint("id", name="pk_purchase_bill_line"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "bill_id"],
            ["purchase_bill.tenant_id", "purchase_bill.company_id", "purchase_bill.id"],
            name="fk_purchase_bill_line_bill",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_purchase_bill_line_item",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_purchase_bill_line_tax_code",
        ),
        sa.UniqueConstraint(
            "tenant_id", "bill_id", "line_number", name="uq_purchase_bill_line_number"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_purchase_bill_line_tenant_id"),
        sa.CheckConstraint("quantity > 0", name="ck_purchase_bill_line_quantity_positive"),
        sa.CheckConstraint(
            "unit_price_minor >= 0", name="ck_purchase_bill_line_price_non_negative"
        ),
        sa.CheckConstraint(
            "line_subtotal_minor >= 0 AND line_tax_minor >= 0",
            name="ck_purchase_bill_line_amounts_non_negative",
        ),
        sa.CheckConstraint(
            "line_total_minor = line_subtotal_minor + line_tax_minor",
            name="ck_purchase_bill_line_total_consistent",
        ),
    )
    op.create_index("ix_purchase_bill_line_bill", "purchase_bill_line", ["tenant_id", "bill_id"])


def _create_stock_move() -> None:
    op.create_table(
        "stock_move",
        _id_column(),
        *_tenant_company(),
        sa.Column("item_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("move_type", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit_cost_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("value_minor", sa.BigInteger(), nullable=False),
        sa.Column("document_type", sa.String(length=40), nullable=False),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_stock_move"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_stock_move_item",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "document_type",
            "document_id",
            "item_id",
            "move_type",
            name="uq_stock_move_document_item_type",
        ),
        sa.CheckConstraint("move_type IN ('receipt', 'issue')", name="ck_stock_move_type_allowed"),
        sa.CheckConstraint("quantity > 0", name="ck_stock_move_quantity_positive"),
        sa.CheckConstraint("value_minor >= 0", name="ck_stock_move_value_non_negative"),
    )
    op.create_index("ix_stock_move_tenant_id", "stock_move", ["tenant_id"])
    op.create_index("ix_stock_move_company_id", "stock_move", ["company_id"])
    op.create_index(
        "ix_stock_move_item",
        "stock_move",
        ["tenant_id", "company_id", "item_id", "occurred_on"],
    )
    op.create_index(
        "ix_stock_move_document",
        "stock_move",
        ["tenant_id", "company_id", "document_type", "document_id"],
    )


def _enable_rls_and_grant() -> None:
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON public.{table} FOR ALL TO PUBLIC "
            f"USING ({TENANT_EXPRESSION}) WITH CHECK ({TENANT_EXPRESSION})"
        )
    tables = ", ".join(f"public.{table}" for table in RLS_TABLES)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {tables} TO ameen_app")


def upgrade() -> None:
    """Create master data, invoice/bill and stock-move tables."""
    _create_master_tables()
    _create_sales_tables()
    _create_purchase_tables()
    _create_stock_move()
    _enable_rls_and_grant()


def downgrade() -> None:
    """Drop the day-7 tables in reverse dependency order."""
    op.drop_table("stock_move")
    op.drop_table("purchase_bill_line")
    op.drop_table("purchase_bill")
    op.drop_table("sales_invoice_line")
    op.drop_table("sales_invoice")
    op.drop_table("item")
    op.drop_table("tax_code")
    op.drop_table("supplier")
    op.drop_table("customer")
