"""Schema and security properties of the identity migration (integration)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

TENANT_TABLES = (
    "tenant",
    "company",
    "branch",
    "app_user",
    "membership",
    "role",
    "role_permission",
    "membership_role",
    "auth_session",
    "mfa_recovery_code",
    "audit_log",
    "account",
    "fiscal_year",
    "accounting_period",
    "journal_entry",
    "journal_line",
    "journal_entry_document",
    "number_sequence",
    "idempotency_record",
    "customer",
    "supplier",
    "tax_code",
    "tax_code_rate",
    "item",
    "sales_invoice",
    "sales_invoice_line",
    "purchase_bill",
    "purchase_bill_line",
    "stock_move",
    "warehouse",
    "stock_balance",
    "stock_transfer",
    "stock_count",
    "stock_count_line",
    "cash_bank_account",
    "receipt",
    "receipt_allocation",
    "supplier_payment",
    "supplier_payment_allocation",
    "sales_credit_note",
    "sales_credit_note_line",
    "purchase_debit_note",
    "purchase_debit_note_line",
    "posting_mapping",
)


async def test_migration_reaches_identity_head(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))

    assert revision == "0023_rename_application_role"


async def test_application_role_is_renamed_and_keeps_its_grants(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        role_exists = await connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'almahmoud_app')")
        )
        can_read_users = await connection.scalar(
            text("SELECT has_table_privilege('almahmoud_app', 'public.app_user', 'SELECT')")
        )
        can_write_users = await connection.scalar(
            text("SELECT has_table_privilege('almahmoud_app', 'public.app_user', 'DELETE')")
        )

    assert role_exists is True
    assert can_read_users is True
    assert can_write_users is True


async def test_tenant_tables_have_forced_row_level_security(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    """
                    SELECT c.relname,
                           c.relrowsecurity,
                           c.relforcerowsecurity,
                           (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relname = ANY(:tables)
                    """
                ),
                {"tables": list(TENANT_TABLES)},
            )
        ).all()

    assert {row.relname for row in rows} == set(TENANT_TABLES)
    for row in rows:
        assert row.relrowsecurity, row.relname
        assert row.relforcerowsecurity, row.relname
        assert row.policies == 1, row.relname


async def test_application_role_cannot_bypass_rls(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        role = (
            await connection.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin "
                    "FROM pg_roles WHERE rolname = 'almahmoud_app'"
                )
            )
        ).one()

    assert not role.rolsuper
    assert not role.rolbypassrls
    assert not role.rolcreatedb
    assert not role.rolcreaterole
    assert not role.rolcanlogin
