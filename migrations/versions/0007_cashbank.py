"""cash/bank accounts, receipts and supplier payments

Revision ID: 0007_cashbank
Revises: 0006_documents
Create Date: 2026-10-21 14:10:00

Phase 1 starts with the money cycle: cash and bank accounts linked to ledger
accounts, customer receipts and supplier payments with invoice/bill allocations.
All money columns are integer minor units; every table is tenant-scoped with the
same fail-closed RLS policy and granted to the application role. Invoice and bill
statuses gain the derived settlement states part_paid and paid.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_cashbank"
down_revision: str | Sequence[str] | None = "0006_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
RLS_TABLES = (
    "cash_bank_account",
    "receipt",
    "receipt_allocation",
    "supplier_payment",
    "supplier_payment_allocation",
)
INVOICE_STATUS_CONSTRAINT = "ck_sales_invoice_ck_sales_invoice_status_allowed"
BILL_STATUS_CONSTRAINT = "ck_purchase_bill_ck_purchase_bill_status_allowed"
# The permission catalogue is code, but the database stores the rows because
# role assignments reference them by code. New codes are additive data.
NEW_PERMISSIONS = (
    ("cashbank.receipt.create", "Create customer receipts"),
    ("cashbank.receipt.post", "Post customer receipts"),
    ("cashbank.payment.create", "Create supplier payments"),
    ("cashbank.payment.post", "Post supplier payments"),
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


def _status_constraint(name: str, allowed: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"status IN ({allowed})", name=name)


def _create_cash_bank_account() -> None:
    op.create_table(
        "cash_bank_account",
        _id_column(),
        *_tenant_company(),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("name_ar", sa.String(length=200), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("ledger_account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_cash_bank_account"),
        _company_fk("cash_bank_account"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "ledger_account_id"],
            ["account.tenant_id", "account.company_id", "account.id"],
            name="fk_cash_bank_account_ledger_account",
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "code", name="uq_cash_bank_account_tenant_company_code"
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_cash_bank_account_tenant_company_id"
        ),
        sa.CheckConstraint("kind IN ('cash', 'bank')", name="ck_cash_bank_account_kind_allowed"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_cash_bank_account_currency_iso"),
    )
    op.create_index("ix_cash_bank_account_tenant_id", "cash_bank_account", ["tenant_id"])
    op.create_index("ix_cash_bank_account_company_id", "cash_bank_account", ["company_id"])
    op.create_index(
        "ix_cash_bank_account_tenant_company", "cash_bank_account", ["tenant_id", "company_id"]
    )


def _create_receipt() -> None:
    op.create_table(
        "receipt",
        _id_column(),
        *_tenant_company(),
        sa.Column("number", sa.String(length=40), nullable=False),
        sa.Column("customer_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("cash_bank_account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(18, 8), server_default=sa.text("1"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "unallocated_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("journal_entry_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Uuid(as_uuid=True), nullable=True),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_receipt"),
        _company_fk("receipt"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "customer_id"],
            ["customer.tenant_id", "customer.company_id", "customer.id"],
            name="fk_receipt_customer",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "cash_bank_account_id"],
            ["cash_bank_account.tenant_id", "cash_bank_account.company_id", "cash_bank_account.id"],
            name="fk_receipt_cash_bank_account",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_receipt_journal_entry",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_receipt_tenant_company_number"
        ),
        sa.UniqueConstraint("tenant_id", "company_id", "id", name="uq_receipt_tenant_company_id"),
        _status_constraint("ck_receipt_status_allowed", "'draft', 'posted'"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_receipt_currency_iso"),
        sa.CheckConstraint("exchange_rate > 0", name="ck_receipt_exchange_rate_positive"),
        sa.CheckConstraint("amount_minor > 0", name="ck_receipt_amount_positive"),
        sa.CheckConstraint("base_amount_minor > 0", name="ck_receipt_base_amount_positive"),
        sa.CheckConstraint(
            "unallocated_minor >= 0 AND unallocated_minor <= amount_minor",
            name="ck_receipt_unallocated_range",
        ),
    )
    op.create_index("ix_receipt_tenant_id", "receipt", ["tenant_id"])
    op.create_index("ix_receipt_company_id", "receipt", ["company_id"])
    op.create_index(
        "ix_receipt_tenant_company_date", "receipt", ["tenant_id", "company_id", "receipt_date"]
    )
    op.create_index("ix_receipt_customer", "receipt", ["tenant_id", "company_id", "customer_id"])

    op.create_table(
        "receipt_allocation",
        _id_column(),
        *_tenant_company(),
        sa.Column("receipt_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("invoice_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("settlement_base_minor", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_receipt_allocation"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "receipt_id"],
            ["receipt.tenant_id", "receipt.company_id", "receipt.id"],
            name="fk_receipt_allocation_receipt",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "invoice_id"],
            ["sales_invoice.tenant_id", "sales_invoice.company_id", "sales_invoice.id"],
            name="fk_receipt_allocation_invoice",
        ),
        sa.UniqueConstraint(
            "tenant_id", "receipt_id", "invoice_id", name="uq_receipt_allocation_receipt_invoice"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_receipt_allocation_tenant_id"),
        sa.CheckConstraint("amount_minor > 0", name="ck_receipt_allocation_amount_positive"),
        sa.CheckConstraint(
            "base_amount_minor >= 0 AND settlement_base_minor >= 0",
            name="ck_receipt_allocation_base_amount_non_negative",
        ),
    )
    op.create_index("ix_receipt_allocation_tenant_id", "receipt_allocation", ["tenant_id"])
    op.create_index(
        "ix_receipt_allocation_invoice",
        "receipt_allocation",
        ["tenant_id", "company_id", "invoice_id"],
    )


def _create_supplier_payment() -> None:
    op.create_table(
        "supplier_payment",
        _id_column(),
        *_tenant_company(),
        sa.Column("number", sa.String(length=40), nullable=False),
        sa.Column("supplier_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("cash_bank_account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(18, 8), server_default=sa.text("1"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "unallocated_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("journal_entry_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Uuid(as_uuid=True), nullable=True),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_supplier_payment"),
        _company_fk("supplier_payment"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "supplier_id"],
            ["supplier.tenant_id", "supplier.company_id", "supplier.id"],
            name="fk_supplier_payment_supplier",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "cash_bank_account_id"],
            ["cash_bank_account.tenant_id", "cash_bank_account.company_id", "cash_bank_account.id"],
            name="fk_supplier_payment_cash_bank_account",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_supplier_payment_journal_entry",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_supplier_payment_tenant_company_number"
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_supplier_payment_tenant_company_id"
        ),
        _status_constraint("ck_supplier_payment_status_allowed", "'draft', 'posted'"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_supplier_payment_currency_iso"),
        sa.CheckConstraint("exchange_rate > 0", name="ck_supplier_payment_exchange_rate_positive"),
        sa.CheckConstraint("amount_minor > 0", name="ck_supplier_payment_amount_positive"),
        sa.CheckConstraint(
            "base_amount_minor > 0", name="ck_supplier_payment_base_amount_positive"
        ),
        sa.CheckConstraint(
            "unallocated_minor >= 0 AND unallocated_minor <= amount_minor",
            name="ck_supplier_payment_unallocated_range",
        ),
    )
    op.create_index("ix_supplier_payment_tenant_id", "supplier_payment", ["tenant_id"])
    op.create_index("ix_supplier_payment_company_id", "supplier_payment", ["company_id"])
    op.create_index(
        "ix_supplier_payment_tenant_company_date",
        "supplier_payment",
        ["tenant_id", "company_id", "payment_date"],
    )
    op.create_index(
        "ix_supplier_payment_supplier",
        "supplier_payment",
        ["tenant_id", "company_id", "supplier_id"],
    )

    op.create_table(
        "supplier_payment_allocation",
        _id_column(),
        *_tenant_company(),
        sa.Column("payment_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("bill_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("settlement_base_minor", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_supplier_payment_allocation"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "payment_id"],
            ["supplier_payment.tenant_id", "supplier_payment.company_id", "supplier_payment.id"],
            name="fk_supplier_payment_allocation_payment",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "bill_id"],
            ["purchase_bill.tenant_id", "purchase_bill.company_id", "purchase_bill.id"],
            name="fk_supplier_payment_allocation_bill",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "payment_id",
            "bill_id",
            name="uq_supplier_payment_allocation_payment_bill",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_supplier_payment_allocation_tenant_id"),
        sa.CheckConstraint(
            "amount_minor > 0", name="ck_supplier_payment_allocation_amount_positive"
        ),
        sa.CheckConstraint(
            "base_amount_minor >= 0 AND settlement_base_minor >= 0",
            name="ck_supplier_payment_allocation_base_amount_non_negative",
        ),
    )
    op.create_index(
        "ix_supplier_payment_allocation_tenant_id", "supplier_payment_allocation", ["tenant_id"]
    )
    op.create_index(
        "ix_supplier_payment_allocation_bill",
        "supplier_payment_allocation",
        ["tenant_id", "company_id", "bill_id"],
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


def _widen_document_statuses() -> None:
    op.execute(f"ALTER TABLE public.sales_invoice DROP CONSTRAINT {INVOICE_STATUS_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE public.sales_invoice ADD CONSTRAINT {INVOICE_STATUS_CONSTRAINT} "
        "CHECK (status IN ('draft', 'posted', 'part_paid', 'paid'))"
    )
    op.execute(f"ALTER TABLE public.purchase_bill DROP CONSTRAINT {BILL_STATUS_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE public.purchase_bill ADD CONSTRAINT {BILL_STATUS_CONSTRAINT} "
        "CHECK (status IN ('draft', 'posted', 'part_paid', 'paid'))"
    )


def _narrow_document_statuses() -> None:
    op.execute(f"ALTER TABLE public.sales_invoice DROP CONSTRAINT {INVOICE_STATUS_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE public.sales_invoice ADD CONSTRAINT {INVOICE_STATUS_CONSTRAINT} "
        "CHECK (status IN ('draft', 'posted'))"
    )
    op.execute(f"ALTER TABLE public.purchase_bill DROP CONSTRAINT {BILL_STATUS_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE public.purchase_bill ADD CONSTRAINT {BILL_STATUS_CONSTRAINT} "
        "CHECK (status IN ('draft', 'posted'))"
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
    """Create the money-cycle tables and widen document statuses."""
    _widen_document_statuses()
    _insert_permissions()
    _create_cash_bank_account()
    _create_receipt()
    _create_supplier_payment()
    _enable_rls_and_grant()


def downgrade() -> None:
    """Drop the money-cycle tables and restore the phase-0 document statuses."""
    op.drop_table("supplier_payment_allocation")
    op.drop_table("supplier_payment")
    op.drop_table("receipt_allocation")
    op.drop_table("receipt")
    op.drop_table("cash_bank_account")
    _narrow_document_statuses()
