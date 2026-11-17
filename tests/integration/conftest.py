"""Fixtures shared by the PostgreSQL integration tests."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.identity.models import (
    AppUser,
    Company,
    Membership,
    MembershipRole,
    Tenant,
)
from app.modules.identity.security import hash_password
from app.modules.identity.seeding import seed_tenant_roles
from app.modules.inventory.models import Warehouse
from app.modules.ledger.models import AccountingPeriod, FiscalYear
from app.modules.ledger.seeding import seed_company_chart
from app.shared.ids import new_id
from tests.conftest import TEST_DATABASE_URL

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class SeededIdentity:
    """A committed tenant with two companies, one user and a membership."""

    tenant_id: UUID
    tenant_slug: str
    company_a: UUID
    company_b: UUID
    user_id: UUID
    email: str
    password: str


async def seed_identity(
    engine: AsyncEngine,
    *,
    tenant_slug: str,
    email: str,
    password: str,
    role_code: str = "accountant",
) -> SeededIdentity:
    """Commit a test tenant; callers must call :func:`delete_identity` afterwards."""
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        tenant = Tenant(
            name="Test Tenant",
            slug=tenant_slug,
            # The seeded user has no authenticator; MFA policy tests toggle
            # these switches explicitly so the rest of the suite stays usable.
            mfa_sensitive_required=False,
            mfa_posting_required=False,
        )
        session.add(tenant)
        await session.flush()
        company_a = Company(
            tenant_id=tenant.id,
            code="A",
            name_ar="شركة ألف",
            base_currency="USD",
        )
        company_b = Company(
            tenant_id=tenant.id,
            code="B",
            name_ar="شركة باء",
            base_currency="USD",
        )
        user = AppUser(
            tenant_id=tenant.id,
            email=email,
            full_name="Test User",
            password_hash=hash_password(password),
        )
        session.add_all([company_a, company_b, user])
        await session.flush()
        roles = await seed_tenant_roles(session, tenant.id)
        membership = Membership(
            tenant_id=tenant.id,
            app_user_id=user.id,
            company_id=company_a.id,
        )
        session.add(membership)
        await session.flush()
        session.add(
            MembershipRole(
                tenant_id=tenant.id,
                membership_id=membership.id,
                role_id=roles[role_code].id,
            )
        )
        await session.flush()
        return SeededIdentity(
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            company_a=company_a.id,
            company_b=company_b.id,
            user_id=user.id,
            email=email,
            password=password,
        )


LEDGER_TABLES_IN_DELETE_ORDER = (
    # audit_log is append-only since migration 0020; teardown removes it with
    # replication triggers disabled, exactly like the immutable journal lines.
    "audit_log",
    "sales_credit_note_line",
    "sales_credit_note",
    "purchase_debit_note_line",
    "purchase_debit_note",
    "receipt_allocation",
    "supplier_payment_allocation",
    "cheque_event",
    "cheque_allocation",
    "cheque",
    "cash_transfer",
    "bank_statement_line",
    "receipt",
    "supplier_payment",
    "cash_bank_account",
    "stock_count_line",
    "stock_count",
    "stock_transfer",
    "stock_balance",
    "stock_move",
    "warehouse",
    "sales_invoice_line",
    "purchase_bill_line",
    "sales_invoice",
    "purchase_bill",
    "mfa_recovery_code",
    "item",
    "tax_code_rate",
    "tax_code",
    "supplier",
    "customer",
    "idempotency_record",
    "number_sequence",
    "journal_line",
    "journal_entry_document",
    "journal_entry",
    "posting_mapping",
    "accounting_period",
    "fiscal_year",
    "account",
)


async def delete_identity(engine: AsyncEngine, tenant_id: UUID) -> None:
    """Delete a seeded tenant, its ledger rows and everything that cascades.

    Posted journal entries are protected by an immutability trigger, so the
    ledger rows are removed with session replication disabled (the test role is
    a superuser); the trigger is never bypassed by application code.
    """
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL session_replication_role = replica"))
        for table in LEDGER_TABLES_IN_DELETE_ORDER:
            await connection.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :id"),  # noqa: S608 - names come from the fixed tuple above
                {"id": str(tenant_id)},
            )
        await connection.execute(text("SET LOCAL session_replication_role = origin"))
        await connection.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": str(tenant_id)})


@dataclass(frozen=True)
class SeededLedger:
    """A committed company ledger: chart, an open January and a closed February."""

    identity: SeededIdentity
    account_ids: dict[str, UUID]
    fiscal_year_id: UUID
    period_id: UUID
    closed_period_id: UUID
    year_end_period_id: UUID


async def seed_ledger(engine: AsyncEngine, identity: SeededIdentity) -> SeededLedger:
    """Seed the default chart and two periods (January open, February closed)."""
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        chart = await seed_company_chart(
            session, tenant_id=identity.tenant_id, company_id=identity.company_a
        )
        session.add(
            Warehouse(
                tenant_id=identity.tenant_id,
                company_id=identity.company_a,
                code="MAIN",
                name_ar="المستودع الرئيسي",
                name_en="Main warehouse",
                is_default=True,
                is_active=True,
            )
        )
        year = FiscalYear(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            code="FY2026",
            name_ar="السنة 2026",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
        )
        session.add(year)
        await session.flush()
        open_period = AccountingPeriod(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            fiscal_year_id=year.id,
            number=1,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )
        closed_period = AccountingPeriod(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            fiscal_year_id=year.id,
            number=2,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 2, 28),
            status="closed",
        )
        year_end_period = AccountingPeriod(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            fiscal_year_id=year.id,
            number=12,
            start_date=date(2026, 12, 1),
            end_date=date(2026, 12, 31),
        )
        session.add_all([open_period, closed_period, year_end_period])
        await session.flush()
        return SeededLedger(
            identity=identity,
            account_ids={code: account.id for code, account in chart.items()},
            fiscal_year_id=year.id,
            period_id=open_period.id,
            closed_period_id=closed_period.id,
            year_end_period_id=year_end_period.id,
        )


@pytest.fixture
async def identity(engine: AsyncEngine) -> AsyncIterator[SeededIdentity]:
    """A committed tenant; unique per test so parallel tests do not collide."""
    slug = f"test-{new_id().hex[-12:]}"
    seeded = await seed_identity(
        engine,
        tenant_slug=slug,
        email=f"user-{slug}@example.com",
        password="correct horse battery staple",
    )
    try:
        yield seeded
    finally:
        await delete_identity(engine, seeded.tenant_id)


@pytest.fixture(scope="session")
def migrated_database() -> None:
    """Bring almahmoud_test to head once per session with a real Alembic run."""
    environment = os.environ.copy()
    environment["MAHMOUD_DATABASE_URL"] = TEST_DATABASE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")


@pytest.fixture
async def engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """An engine connected as the migration owner (a superuser in development)."""
    database_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        yield database_engine
    finally:
        await database_engine.dispose()
