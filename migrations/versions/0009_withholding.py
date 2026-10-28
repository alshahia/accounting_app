"""withholding tax codes and payment withholding

Revision ID: 0009_withholding
Revises: 0008_returns
Create Date: 2026-10-21 14:40:00

A withholding code is a tax code with kind 'withholding'. It applies when money
is received or paid, not on the invoice: the payer keeps part of the gross
settlement and owes it to the tax authority. Receipts debit a withholding
receivable account, supplier payments credit a withholding payable account, and
the cash leg is the net amount. Rates stay data (basis points on the tax code)
because the applicable regimes are still an open question for the local
accountant. The two withholding accounts are seeded for existing companies here
and listed in the chart seeding for new ones.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_withholding"
down_revision: str | Sequence[str] | None = "0008_returns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCOUNTS = (
    ("1250", "ضريبة مستقطعة من العملاء", "Withholding receivable", "asset"),
    ("2150", "ضريبة مستقطعة من الموردين", "Withholding payable", "liability"),
)


def _withholding_columns(prefix: str) -> list[sa.Column[object]]:
    return [
        sa.Column("withholding_tax_code_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "withholding_base_minor",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "withholding_amount_minor",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "base_withholding_minor",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    ]


def _add_withholding(table: str) -> None:
    for column in _withholding_columns(table):
        op.add_column(table, column)
    op.create_foreign_key(
        f"fk_{table}_withholding_tax_code",
        table,
        "tax_code",
        ["tenant_id", "company_id", "withholding_tax_code_id"],
        ["tenant_id", "company_id", "id"],
    )
    op.create_check_constraint(
        f"ck_{table}_withholding_range",
        table,
        "withholding_amount_minor >= 0 AND withholding_amount_minor <= amount_minor",
    )
    op.create_check_constraint(
        f"ck_{table}_base_withholding_range",
        table,
        "base_withholding_minor >= 0 AND base_withholding_minor <= base_amount_minor",
    )


def upgrade() -> None:
    """Add withholding codes, payment withholding columns and the two accounts."""
    op.add_column(
        "tax_code",
        sa.Column("kind", sa.String(length=20), server_default=sa.text("'sales'"), nullable=False),
    )
    op.create_check_constraint(
        "ck_tax_code_kind",
        "tax_code",
        "kind IN ('sales', 'purchase', 'withholding')",
    )
    _add_withholding("receipt")
    _add_withholding("supplier_payment")
    for code, name_ar, name_en, account_type in ACCOUNTS:
        op.execute(
            sa.text(
                "INSERT INTO account (tenant_id, company_id, code, name_ar, name_en, "
                "account_type, is_postable) "
                "SELECT company.tenant_id, company.id, :code, :name_ar, :name_en, "
                ":account_type, true FROM company "
                "ON CONFLICT ON CONSTRAINT uq_account_tenant_company_code DO NOTHING"
            ).bindparams(
                code=code,
                name_ar=name_ar,
                name_en=name_en,
                account_type=account_type,
            )
        )


def downgrade() -> None:
    """Remove the two accounts, the withholding columns and the tax code kind."""
    op.execute("DELETE FROM account WHERE code IN ('1250', '2150')")
    # type_="check" is required: the project's naming convention renders a check
    # constraint as ck_<table>_<given name>, and the given names already carry
    # the prefix, so Alembic can only find the stored name through the convention.
    op.drop_constraint(
        "ck_supplier_payment_base_withholding_range", "supplier_payment", type_="check"
    )
    op.drop_constraint("ck_supplier_payment_withholding_range", "supplier_payment", type_="check")
    op.drop_constraint(
        "fk_supplier_payment_withholding_tax_code", "supplier_payment", type_="foreignkey"
    )
    for column in reversed(_withholding_columns("supplier_payment")):
        op.drop_column("supplier_payment", column.name)
    op.drop_constraint("ck_receipt_base_withholding_range", "receipt", type_="check")
    op.drop_constraint("ck_receipt_withholding_range", "receipt", type_="check")
    op.drop_constraint("fk_receipt_withholding_tax_code", "receipt", type_="foreignkey")
    for column in reversed(_withholding_columns("receipt")):
        op.drop_column("receipt", column.name)
    op.drop_constraint("ck_tax_code_kind", "tax_code", type_="check")
    op.drop_column("tax_code", "kind")
