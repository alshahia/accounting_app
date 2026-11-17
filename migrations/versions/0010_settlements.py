"""cheques, cash transfers and bank reconciliation

Revision ID: 0010_settlements
Revises: 0009_withholding
Create Date: 2026-10-21 15:05:00

Phase 1 needs the rest of the cash and bank surface: a posted transfer between
two cash/bank accounts, cheques received and issued with a posted lifecycle
(in hand, deposited, cleared, bounced, cancelled), and imported bank statement
lines that can be matched to posted money documents without editing them.

A cheque lifecycle event is a journal entry whose source is the event row, so a
cheque can post several entries while the one-entry-per-source invariant still
holds. Cheque allocations are like receipt/payment allocations but can be
reversed by a bounce or cancellation; the reversed flag keeps the posted history
instead of deleting it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_settlements"
down_revision: str | Sequence[str] | None = "0009_withholding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_EXPRESSION = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
RLS_TABLES = ("cash_transfer", "cheque", "cheque_allocation", "cheque_event", "bank_statement_line")
NEW_PERMISSIONS = (
    ("cashbank.transfer.create", "Create cash and bank transfers"),
    ("cashbank.transfer.post", "Post cash and bank transfers"),
    ("cashbank.cheque.create", "Create received and issued cheques"),
    ("cashbank.cheque.post", "Post cheque lifecycle events"),
    ("cashbank.reconcile", "Import and match bank statement lines"),
)
ROLE_GRANTS = {
    "tenant_admin": {code for code, _ in NEW_PERMISSIONS},
    "company_admin": {code for code, _ in NEW_PERMISSIONS},
    "accountant": {
        "cashbank.transfer.create",
        "cashbank.transfer.post",
        "cashbank.cheque.create",
        "cashbank.cheque.post",
        "cashbank.reconcile",
    },
    "sales": {"cashbank.cheque.create", "cashbank.cheque.post"},
    "purchasing": {"cashbank.cheque.create", "cashbank.cheque.post"},
}
ACCOUNTS = (
    ("1230", "شيكات برسم التحصيل", "Cheques under collection", "asset"),
    ("2200", "شيكات صادرة", "Cheques issued", "liability"),
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


def _account_fk(table: str, column: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "company_id", column],
        ["cash_bank_account.tenant_id", "cash_bank_account.company_id", "cash_bank_account.id"],
        name=name,
    )


def _create_cash_transfer() -> None:
    op.create_table(
        "cash_transfer",
        _id_column(),
        *_tenant_company(),
        sa.Column("number", sa.String(40), nullable=False),
        sa.Column("transfer_date", sa.Date(), nullable=False),
        sa.Column("from_account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("to_account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(18, 8), server_default=sa.text("1"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("memo", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), server_default=sa.text("'posted'"), nullable=False),
        sa.Column("journal_entry_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_by", sa.Uuid(as_uuid=True), nullable=True),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_cash_transfer"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "number", name="uq_cash_transfer_tenant_company_number"
        ),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_cash_transfer_tenant_company_id"
        ),
        _company_fk("cash_transfer"),
        _account_fk("cash_transfer", "from_account_id", "fk_cash_transfer_from_account"),
        _account_fk("cash_transfer", "to_account_id", "fk_cash_transfer_to_account"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_cash_transfer_journal_entry",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("status IN ('posted')", name="ck_cash_transfer_status_allowed"),
        sa.CheckConstraint(
            "from_account_id <> to_account_id", name="ck_cash_transfer_accounts_differ"
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_cash_transfer_currency_iso"),
        sa.CheckConstraint("exchange_rate > 0", name="ck_cash_transfer_exchange_rate_positive"),
        sa.CheckConstraint("amount_minor > 0", name="ck_cash_transfer_amount_positive"),
        sa.CheckConstraint("base_amount_minor > 0", name="ck_cash_transfer_base_amount_positive"),
    )
    op.create_index("ix_cash_transfer_tenant_id", "cash_transfer", ["tenant_id"])
    op.create_index("ix_cash_transfer_company_id", "cash_transfer", ["company_id"])
    op.create_index(
        "ix_cash_transfer_tenant_company_date",
        "cash_transfer",
        ["tenant_id", "company_id", "transfer_date"],
    )


def _create_cheque() -> None:
    op.create_table(
        "cheque",
        _id_column(),
        *_tenant_company(),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("number", sa.String(40), nullable=False),
        sa.Column("customer_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("supplier_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("cash_bank_account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("bank_name", sa.String(200), nullable=True),
        sa.Column("party_name", sa.String(200), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(18, 8), server_default=sa.text("1"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "unallocated_minor", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("memo", sa.String(500), nullable=True),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_cheque"),
        sa.UniqueConstraint("tenant_id", "company_id", "id", name="uq_cheque_tenant_company_id"),
        _company_fk("cheque"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "customer_id"],
            ["customer.tenant_id", "customer.company_id", "customer.id"],
            name="fk_cheque_customer",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "supplier_id"],
            ["supplier.tenant_id", "supplier.company_id", "supplier.id"],
            name="fk_cheque_supplier",
        ),
        _account_fk("cheque", "cash_bank_account_id", "fk_cheque_cash_bank_account"),
        sa.CheckConstraint(
            "direction IN ('received', 'issued')", name="ck_cheque_direction_allowed"
        ),
        sa.CheckConstraint(
            "status IN ('in_hand', 'deposited', 'cleared', 'issued', 'bounced', 'cancelled')",
            name="ck_cheque_status_allowed",
        ),
        sa.CheckConstraint(
            "(direction = 'received' AND customer_id IS NOT NULL AND supplier_id IS NULL) "
            "OR (direction = 'issued' AND supplier_id IS NOT NULL AND customer_id IS NULL)",
            name="ck_cheque_party_matches_direction",
        ),
        sa.CheckConstraint(
            "(direction = 'received' AND status IN "
            "('in_hand', 'deposited', 'cleared', 'bounced', 'cancelled')) "
            "OR (direction = 'issued' AND status IN ('issued', 'cleared', 'bounced', 'cancelled'))",
            name="ck_cheque_status_matches_direction",
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_cheque_currency_iso"),
        sa.CheckConstraint("exchange_rate > 0", name="ck_cheque_exchange_rate_positive"),
        sa.CheckConstraint("amount_minor > 0", name="ck_cheque_amount_positive"),
        sa.CheckConstraint("base_amount_minor > 0", name="ck_cheque_base_amount_positive"),
        sa.CheckConstraint(
            "unallocated_minor >= 0 AND unallocated_minor <= amount_minor",
            name="ck_cheque_unallocated_range",
        ),
        sa.CheckConstraint("due_date >= issue_date", name="ck_cheque_due_date_after_issue"),
    )
    op.create_index("ix_cheque_tenant_id", "cheque", ["tenant_id"])
    op.create_index("ix_cheque_company_id", "cheque", ["company_id"])
    op.create_index(
        "ix_cheque_tenant_company_status", "cheque", ["tenant_id", "company_id", "status"]
    )
    op.create_index(
        "ix_cheque_tenant_company_due", "cheque", ["tenant_id", "company_id", "due_date"]
    )


def _create_cheque_allocation() -> None:
    op.create_table(
        "cheque_allocation",
        _id_column(),
        *_tenant_company(),
        sa.Column("cheque_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("invoice_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("bill_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("base_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("settlement_base_minor", sa.BigInteger(), nullable=False),
        sa.Column("is_reversed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_cheque_allocation"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_cheque_allocation_tenant_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "cheque_id"],
            ["cheque.tenant_id", "cheque.company_id", "cheque.id"],
            name="fk_cheque_allocation_cheque",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "invoice_id"],
            ["sales_invoice.tenant_id", "sales_invoice.company_id", "sales_invoice.id"],
            name="fk_cheque_allocation_invoice",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "bill_id"],
            ["purchase_bill.tenant_id", "purchase_bill.company_id", "purchase_bill.id"],
            name="fk_cheque_allocation_bill",
        ),
        sa.CheckConstraint(
            "(invoice_id IS NOT NULL AND bill_id IS NULL) OR "
            "(invoice_id IS NULL AND bill_id IS NOT NULL)",
            name="ck_cheque_allocation_one_document",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_cheque_allocation_amount_positive"),
        sa.CheckConstraint(
            "base_amount_minor >= 0 AND settlement_base_minor >= 0",
            name="ck_cheque_allocation_base_amount_non_negative",
        ),
        sa.CheckConstraint(
            "is_reversed = false OR reversed_at IS NOT NULL",
            name="ck_cheque_allocation_reversal_stamped",
        ),
    )
    op.create_index("ix_cheque_allocation_tenant_id", "cheque_allocation", ["tenant_id"])
    op.create_index("ix_cheque_allocation_company_id", "cheque_allocation", ["company_id"])
    op.create_index(
        "ix_cheque_allocation_invoice",
        "cheque_allocation",
        ["tenant_id", "company_id", "invoice_id"],
    )
    op.create_index(
        "ix_cheque_allocation_bill", "cheque_allocation", ["tenant_id", "company_id", "bill_id"]
    )


def _create_cheque_event() -> None:
    op.create_table(
        "cheque_event",
        _id_column(),
        *_tenant_company(),
        sa.Column("cheque_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("journal_entry_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("notes", sa.String(500), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_cheque_event"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_cheque_event_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_cheque_event_tenant_company_id"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "cheque_id"],
            ["cheque.tenant_id", "cheque.company_id", "cheque.id"],
            name="fk_cheque_event_cheque",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "company_id", "journal_entry_id"],
            ["journal_entry.tenant_id", "journal_entry.company_id", "journal_entry.id"],
            name="fk_cheque_event_journal_entry",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "event_type IN ('received', 'deposited', 'cleared', 'bounced', 'issued', 'cancelled')",
            name="ck_cheque_event_type_allowed",
        ),
    )
    op.create_index("ix_cheque_event_tenant_id", "cheque_event", ["tenant_id"])
    op.create_index("ix_cheque_event_company_id", "cheque_event", ["company_id"])
    op.create_index(
        "ix_cheque_event_cheque", "cheque_event", ["tenant_id", "company_id", "cheque_id"]
    )


def _create_bank_statement_line() -> None:
    op.create_table(
        "bank_statement_line",
        _id_column(),
        *_tenant_company(),
        sa.Column("cash_bank_account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("reference", sa.String(100), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'unmatched'"), nullable=False),
        sa.Column("matched_source_type", sa.String(30), nullable=True),
        sa.Column("matched_source_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("matched_by", sa.Uuid(as_uuid=True), nullable=True),
        *_timestamps(),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_bank_statement_line"),
        sa.UniqueConstraint(
            "tenant_id", "company_id", "id", name="uq_bank_statement_line_tenant_company_id"
        ),
        _company_fk("bank_statement_line"),
        _account_fk(
            "bank_statement_line",
            "cash_bank_account_id",
            "fk_bank_statement_line_cash_bank_account",
        ),
        sa.CheckConstraint(
            "status IN ('unmatched', 'matched')", name="ck_bank_statement_line_status_allowed"
        ),
        sa.CheckConstraint("amount_minor <> 0", name="ck_bank_statement_line_amount_non_zero"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_bank_statement_line_currency_iso"),
        sa.CheckConstraint(
            "(status = 'unmatched' AND matched_source_type IS NULL AND matched_source_id IS NULL) "
            "OR (status = 'matched' AND matched_source_type IS NOT NULL "
            "AND matched_source_id IS NOT NULL)",
            name="ck_bank_statement_line_match_fields_consistent",
        ),
    )
    op.create_index("ix_bank_statement_line_tenant_id", "bank_statement_line", ["tenant_id"])
    op.create_index("ix_bank_statement_line_company_id", "bank_statement_line", ["company_id"])
    op.create_index(
        "ix_bank_statement_line_account_date",
        "bank_statement_line",
        ["tenant_id", "company_id", "cash_bank_account_id", "statement_date"],
    )


def _insert_permissions() -> None:
    for code, description in NEW_PERMISSIONS:
        module, action = code.split(".", 1)
        op.execute(
            sa.text(
                "INSERT INTO permission (code, module, action, description) "
                "VALUES (:code, :module, :action, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(code=code, module=module, action=action, description=description)
        )
    for role_code, grants in ROLE_GRANTS.items():
        for permission_code in sorted(grants):
            op.execute(
                sa.text(
                    "INSERT INTO role_permission (tenant_id, role_id, permission_code) "
                    "SELECT r.tenant_id, r.id, :permission_code FROM role r "
                    "JOIN tenant t ON t.id = r.tenant_id "
                    "WHERE r.code = :role_code "
                    "ON CONFLICT DO NOTHING"
                ).bindparams(role_code=role_code, permission_code=permission_code)
            )


def _seed_accounts() -> None:
    for code, name_ar, name_en, account_type in ACCOUNTS:
        op.execute(
            sa.text(
                "INSERT INTO account "
                "(tenant_id, company_id, code, name_ar, name_en, account_type, is_postable, "
                "is_active) "
                "SELECT tenant_id, id, :code, :name_ar, :name_en, :account_type, true, true "
                "FROM company "
                "ON CONFLICT ON CONSTRAINT uq_account_tenant_company_code DO NOTHING"
            ).bindparams(code=code, name_ar=name_ar, name_en=name_en, account_type=account_type)
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
    """Create the settlement tables and seed the extra chart accounts."""
    _create_cash_transfer()
    _create_cheque()
    _create_cheque_allocation()
    _create_cheque_event()
    _create_bank_statement_line()
    _insert_permissions()
    _seed_accounts()
    _enable_rls_and_grant()


def downgrade() -> None:
    """Drop the settlement tables and remove the seeded rows."""
    op.drop_table("bank_statement_line")
    op.drop_table("cheque_event")
    op.drop_table("cheque_allocation")
    op.drop_table("cheque")
    op.drop_table("cash_transfer")
    for code, *_ in ACCOUNTS:
        op.execute(
            sa.text(
                "DELETE FROM account WHERE code = :code "
                "AND id NOT IN (SELECT account_id FROM journal_line)"
            ).bindparams(code=code)
        )
    for code, _description in NEW_PERMISSIONS:
        op.execute(
            sa.text("DELETE FROM role_permission WHERE permission_code = :code").bindparams(
                code=code
            )
        )
        op.execute(sa.text("DELETE FROM permission WHERE code = :code").bindparams(code=code))
