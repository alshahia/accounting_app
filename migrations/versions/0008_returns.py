"""sales credit notes and purchase debit notes

Revision ID: 0008_returns
Revises: 0007_cashbank
Create Date: 2026-10-21 14:20:00

Goods returns both reverse an original document and put stock back or take it
out of inventory. A sales credit note links to the posted invoice (optional for
service lines), returns stocked lines at the cost the invoice issued them, and
credits receivable, revenue, output VAT and cost of sales. A purchase debit note
mirrors it against a bill. Both are tenant-scoped with the same forced RLS and
app-role grants as the rest of the schema.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_returns"
down_revision: str | Sequence[str] | None = "0007_cashbank"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
RLS_TABLES = (
    "sales_credit_note",
    "sales_credit_note_line",
    "purchase_debit_note",
    "purchase_debit_note_line",
)
NEW_PERMISSIONS = (
    ("sales.credit_note.create", "Create customer credit notes"),
    ("sales.credit_note.post", "Post customer credit notes"),
    ("purchasing.debit_note.create", "Create supplier debit notes"),
    ("purchasing.debit_note.post", "Post supplier debit notes"),
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


def _amount_columns(prefix: str) -> list[sa.Column[object]]:
    return [
        sa.Column(f"{prefix}subtotal_minor", sa.BigInteger(), nullable=False),
        sa.Column(f"{prefix}tax_minor", sa.BigInteger(), nullable=False),
        sa.Column(f"{prefix}total_minor", sa.BigInteger(), nullable=False),
        sa.Column(f"base_{prefix}subtotal_minor", sa.BigInteger(), nullable=False),
        sa.Column(f"base_{prefix}tax_minor", sa.BigInteger(), nullable=False),
        sa.Column(f"base_{prefix}total_minor", sa.BigInteger(), nullable=False),
    ]


def _add_line_unique_constraints() -> None:
    # The phase-0 line tables only had (tenant_id, id); the fact tables below
    # reference lines by (tenant_id, company_id, id).
    op.create_unique_constraint(
        "uq_sales_invoice_line_tenant_company_id",
        "sales_invoice_line",
        ["tenant_id", "company_id", "id"],
    )
    op.create_unique_constraint(
        "uq_purchase_bill_line_tenant_company_id",
        "purchase_bill_line",
        ["tenant_id", "company_id", "id"],
    )


def _drop_line_unique_constraints() -> None:
    op.drop_constraint(
        "uq_sales_invoice_line_tenant_company_id", "sales_invoice_line", type_="unique"
    )
    op.drop_constraint(
        "uq_purchase_bill_line_tenant_company_id", "purchase_bill_line", type_="unique"
    )


def _create_sales_credit_note() -> None:
    op.create_table(
        "sales_credit_note",
        _id_column(),
        *_tenant_company(),
        sa.Column("number", sa.String(length=40), nullable=False),
        sa.Column("customer_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("invoice_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("credit_note_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(18, 8), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("memo", sa.String(length=500), nullable=True),
        *_amount_columns(""),
        sa.Column("journal_entry_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Uuid(as_uuid=True), nullable=True),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_sales_credit_note"),
        _company_fk("sales_credit_note"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "customer_id"],
            ["customer.tenant_id", "customer.company_id", "customer.id"],
            name="fk_sales_credit_note_customer",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "invoice_id"],
            ["sales_invoice.tenant_id", "sales_invoice.company_id", "sales_invoice.id"],
            name="fk_sales_credit_note_invoice",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_sales_credit_note_journal_entry",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_sales_credit_note_tenant_company_number"
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_sales_credit_note_tenant_company_id"
        ),
        sa.CheckConstraint("status IN ('draft', 'posted')", name="ck_sales_credit_note_status"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_sales_credit_note_currency_iso"),
        sa.CheckConstraint("exchange_rate > 0", name="ck_sales_credit_note_exchange_rate_positive"),
        sa.CheckConstraint(
            "subtotal_minor >= 0 AND tax_minor >= 0 AND total_minor >= 0",
            name="ck_sales_credit_note_amounts_non_negative",
        ),
        sa.CheckConstraint(
            "total_minor = subtotal_minor + tax_minor",
            name="ck_sales_credit_note_total_consistent",
        ),
        sa.CheckConstraint(
            "base_total_minor = base_subtotal_minor + base_tax_minor",
            name="ck_sales_credit_note_base_total_consistent",
        ),
    )
    op.create_index("ix_sales_credit_note_tenant_id", "sales_credit_note", ["tenant_id"])
    op.create_index("ix_sales_credit_note_company_id", "sales_credit_note", ["company_id"])
    op.create_index(
        "ix_sales_credit_note_tenant_company_date",
        "sales_credit_note",
        ["tenant_id", "company_id", "credit_note_date"],
    )
    op.create_index(
        "ix_sales_credit_note_customer",
        "sales_credit_note",
        ["tenant_id", "company_id", "customer_id"],
    )
    op.create_index(
        "ix_sales_credit_note_invoice",
        "sales_credit_note",
        ["tenant_id", "company_id", "invoice_id"],
    )

    op.create_table(
        "sales_credit_note_line",
        _id_column(),
        *_tenant_company(),
        sa.Column("credit_note_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("invoice_line_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("tax_code_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("line_subtotal_minor", sa.BigInteger(), nullable=False),
        sa.Column("line_tax_minor", sa.BigInteger(), nullable=False),
        sa.Column("line_total_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_line_subtotal_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_line_tax_minor", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_sales_credit_note_line"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "credit_note_id"],
            [
                "sales_credit_note.tenant_id",
                "sales_credit_note.company_id",
                "sales_credit_note.id",
            ],
            name="fk_sales_credit_note_line_note",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "invoice_line_id"],
            [
                "sales_invoice_line.tenant_id",
                "sales_invoice_line.company_id",
                "sales_invoice_line.id",
            ],
            name="fk_sales_credit_note_line_invoice_line",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_sales_credit_note_line_item",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_sales_credit_note_line_tax_code",
        ),
        sa.UniqueConstraint(
            "tenant_id", "credit_note_id", "line_number", name="uq_sales_credit_note_line_number"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_sales_credit_note_line_tenant_id"),
        sa.CheckConstraint("quantity > 0", name="ck_sales_credit_note_line_quantity_positive"),
        sa.CheckConstraint(
            "unit_price_minor >= 0", name="ck_sales_credit_note_line_price_non_negative"
        ),
        sa.CheckConstraint(
            "line_total_minor = line_subtotal_minor + line_tax_minor",
            name="ck_sales_credit_note_line_total_consistent",
        ),
    )
    op.create_index("ix_sales_credit_note_line_tenant_id", "sales_credit_note_line", ["tenant_id"])
    op.create_index(
        "ix_sales_credit_note_line_note",
        "sales_credit_note_line",
        ["tenant_id", "credit_note_id"],
    )
    op.create_index(
        "ix_sales_credit_note_line_invoice_line",
        "sales_credit_note_line",
        ["tenant_id", "company_id", "invoice_line_id"],
    )


def _create_purchase_debit_note() -> None:
    op.create_table(
        "purchase_debit_note",
        _id_column(),
        *_tenant_company(),
        sa.Column("number", sa.String(length=40), nullable=False),
        sa.Column("supplier_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("bill_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("debit_note_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(18, 8), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("memo", sa.String(length=500), nullable=True),
        *_amount_columns(""),
        sa.Column("journal_entry_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Uuid(as_uuid=True), nullable=True),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_purchase_debit_note"),
        _company_fk("purchase_debit_note"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "supplier_id"],
            ["supplier.tenant_id", "supplier.company_id", "supplier.id"],
            name="fk_purchase_debit_note_supplier",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "bill_id"],
            ["purchase_bill.tenant_id", "purchase_bill.company_id", "purchase_bill.id"],
            name="fk_purchase_debit_note_bill",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_purchase_debit_note_journal_entry",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "company_id",
            "number",
            name="uq_purchase_debit_note_tenant_company_number",
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_purchase_debit_note_tenant_company_id"
        ),
        sa.CheckConstraint("status IN ('draft', 'posted')", name="ck_purchase_debit_note_status"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_purchase_debit_note_currency_iso"),
        sa.CheckConstraint(
            "exchange_rate > 0", name="ck_purchase_debit_note_exchange_rate_positive"
        ),
        sa.CheckConstraint(
            "subtotal_minor >= 0 AND tax_minor >= 0 AND total_minor >= 0",
            name="ck_purchase_debit_note_amounts_non_negative",
        ),
        sa.CheckConstraint(
            "total_minor = subtotal_minor + tax_minor",
            name="ck_purchase_debit_note_total_consistent",
        ),
        sa.CheckConstraint(
            "base_total_minor = base_subtotal_minor + base_tax_minor",
            name="ck_purchase_debit_note_base_total_consistent",
        ),
    )
    op.create_index("ix_purchase_debit_note_tenant_id", "purchase_debit_note", ["tenant_id"])
    op.create_index("ix_purchase_debit_note_company_id", "purchase_debit_note", ["company_id"])
    op.create_index(
        "ix_purchase_debit_note_tenant_company_date",
        "purchase_debit_note",
        ["tenant_id", "company_id", "debit_note_date"],
    )
    op.create_index(
        "ix_purchase_debit_note_supplier",
        "purchase_debit_note",
        ["tenant_id", "company_id", "supplier_id"],
    )
    op.create_index(
        "ix_purchase_debit_note_bill",
        "purchase_debit_note",
        ["tenant_id", "company_id", "bill_id"],
    )

    op.create_table(
        "purchase_debit_note_line",
        _id_column(),
        *_tenant_company(),
        sa.Column("debit_note_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("bill_line_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("tax_code_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("line_subtotal_minor", sa.BigInteger(), nullable=False),
        sa.Column("line_tax_minor", sa.BigInteger(), nullable=False),
        sa.Column("line_total_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_line_subtotal_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_line_tax_minor", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_purchase_debit_note_line"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "debit_note_id"],
            [
                "purchase_debit_note.tenant_id",
                "purchase_debit_note.company_id",
                "purchase_debit_note.id",
            ],
            name="fk_purchase_debit_note_line_note",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "bill_line_id"],
            [
                "purchase_bill_line.tenant_id",
                "purchase_bill_line.company_id",
                "purchase_bill_line.id",
            ],
            name="fk_purchase_debit_note_line_bill_line",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "item_id"],
            ["item.tenant_id", "item.company_id", "item.id"],
            name="fk_purchase_debit_note_line_item",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "tax_code_id"],
            ["tax_code.tenant_id", "tax_code.company_id", "tax_code.id"],
            name="fk_purchase_debit_note_line_tax_code",
        ),
        sa.UniqueConstraint(
            "tenant_id", "debit_note_id", "line_number", name="uq_purchase_debit_note_line_number"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_purchase_debit_note_line_tenant_id"),
        sa.CheckConstraint("quantity > 0", name="ck_purchase_debit_note_line_quantity_positive"),
        sa.CheckConstraint(
            "unit_price_minor >= 0", name="ck_purchase_debit_note_line_price_non_negative"
        ),
        sa.CheckConstraint(
            "line_total_minor = line_subtotal_minor + line_tax_minor",
            name="ck_purchase_debit_note_line_total_consistent",
        ),
    )
    op.create_index(
        "ix_purchase_debit_note_line_tenant_id", "purchase_debit_note_line", ["tenant_id"]
    )
    op.create_index(
        "ix_purchase_debit_note_line_note",
        "purchase_debit_note_line",
        ["tenant_id", "debit_note_id"],
    )
    op.create_index(
        "ix_purchase_debit_note_line_bill_line",
        "purchase_debit_note_line",
        ["tenant_id", "company_id", "bill_line_id"],
    )


def _insert_permissions() -> None:
    for code, description in NEW_PERMISSIONS:
        module, action = code.split(".", 1)
        op.execute(
            sa.text(
                "INSERT INTO permission (code, module, action, description) "
                "VALUES (:code, :module, :action, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(
                code=code,
                module=module,
                action=action,
                description=description,
            )
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
    """Create the returns tables and their permissions."""
    _insert_permissions()
    _add_line_unique_constraints()
    _create_sales_credit_note()
    _create_purchase_debit_note()
    _enable_rls_and_grant()


def downgrade() -> None:
    """Drop the returns tables."""
    op.drop_table("purchase_debit_note_line")
    op.drop_table("purchase_debit_note")
    op.drop_table("sales_credit_note_line")
    op.drop_table("sales_credit_note")
    _drop_line_unique_constraints()
