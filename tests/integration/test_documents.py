"""Invoice, bill, tax and stock valuation tests (phase 0, day 7).

These tests drive the real API and database: a sales invoice posts a balanced
receivable/revenue/VAT entry, a foreign-currency document converts at its own
rate, duplicate document numbers are refused, a purchase bill receives stock at
base cost, issuing the stock posts cost of sales at weighted-average cost, and a
fully sold item leaves zero value and zero inventory GL. Journal totals are read
back from PostgreSQL rather than trusted from the response.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.modules.catalog.models import Customer, Item, Supplier, TaxCode, TaxCodeRate
from app.modules.inventory.models import StockMove
from app.modules.ledger.models import Account, JournalEntry, JournalLine
from app.modules.purchasing.service import (
    PurchaseBillInput,
    PurchaseLineInput,
    create_purchase_bill,
)
from app.modules.sales.service import SalesInvoiceInput, SalesLineInput, create_sales_invoice
from app.shared.ids import new_id
from app.shared.unit_of_work import UnitOfWork
from tests.integration.conftest import (
    SeededIdentity,
    SeededLedger,
    seed_identity,
    seed_ledger,
)

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
INVOICES_URL = "/api/v1/sales/invoices"
BILLS_URL = "/api/v1/purchasing/bills"
CATALOG_URL = "/api/v1/catalog"
JANUARY = date(2026, 1, 15)
FEBRUARY = date(2026, 2, 15)


@dataclass(frozen=True)
class MasterData:
    """A customer, supplier, 15% tax code and stocked item for one company."""

    customer_id: UUID
    supplier_id: UUID
    tax_code_id: UUID
    item_id: UUID


async def seed_master(engine: AsyncEngine, identity: SeededIdentity) -> MasterData:
    """Commit the day-7 master data directly; API creation is tested separately."""
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        suffix = new_id().hex[-8:]
        customer = Customer(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            code=f"C-{suffix}",
            name_ar="عميل تجريبي",
        )
        supplier = Supplier(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            code=f"S-{suffix}",
            name_ar="مورد تجريبي",
        )
        tax_code = TaxCode(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            code=f"VAT15-{suffix}",
            name_ar="ضريبة 15%",
            rate_basis_points=1500,
        )
        item = Item(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            code=f"IT-{suffix}",
            name_ar="صنف تجريبي",
            unit_of_measure="piece",
        )
        session.add_all([customer, supplier, tax_code, item])
        await session.flush()
        session.add(
            TaxCodeRate(
                tenant_id=identity.tenant_id,
                company_id=identity.company_a,
                tax_code_id=tax_code.id,
                rate_basis_points=1500,
                valid_from=date(2000, 1, 1),
            )
        )
        await session.flush()
        return MasterData(
            customer_id=customer.id,
            supplier_id=supplier.id,
            tax_code_id=tax_code.id,
            item_id=item.id,
        )


@pytest.fixture
async def ledger(engine: AsyncEngine, identity: SeededIdentity) -> SeededLedger:
    """The identity fixture plus a chart and January/February periods."""
    return await seed_ledger(engine, identity)


@pytest.fixture
async def master(engine: AsyncEngine, ledger: SeededLedger) -> MasterData:
    """Customer, supplier, tax code and item for the ledger's company."""
    return await seed_master(engine, ledger.identity)


@pytest.fixture
def unit_of_work(engine: AsyncEngine) -> UnitOfWork:
    """A unit of work over the test database."""
    return UnitOfWork(async_sessionmaker(engine, expire_on_commit=False))


@pytest.fixture
async def company_admin(engine: AsyncEngine) -> SeededIdentity:
    """A company-admin user for catalog endpoints."""
    seeded = await seed_identity(
        engine,
        tenant_slug=f"catalog-{new_id().hex[-12:]}",
        email=f"catalog-{new_id().hex[-12:]}@example.com",
        password="catalog password 123",
        role_code="company_admin",
    )
    from tests.integration.conftest import delete_identity

    try:
        yield seeded
    finally:
        await delete_identity(engine, seeded.tenant_id)


async def _headers(
    client: AsyncClient, identity: SeededIdentity, company_id: UUID
) -> dict[str, str]:
    response = await client.post(
        LOGIN_URL, json={"email": identity.email, "password": identity.password}
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Company-Id": str(company_id),
    }


async def _account_balances(
    session: AsyncConnection, *, tenant_id: UUID, company_id: UUID
) -> dict[str, int]:
    rows = (
        await session.execute(
            select(
                Account.code,
                func.coalesce(func.sum(JournalLine.debit_minor - JournalLine.credit_minor), 0),
            )
            .select_from(JournalLine)
            .join(Account, Account.id == JournalLine.account_id)
            .where(JournalLine.tenant_id == tenant_id, JournalLine.company_id == company_id)
            .group_by(Account.code)
        )
    ).all()
    return {code: int(balance) for code, balance in rows}


async def _entry_lines(
    session: AsyncConnection, *, tenant_id: UUID, entry_id: UUID
) -> dict[str, int]:
    rows = (
        await session.execute(
            select(
                Account.code,
                func.coalesce(func.sum(JournalLine.debit_minor - JournalLine.credit_minor), 0),
            )
            .select_from(JournalLine)
            .join(Account, Account.id == JournalLine.account_id)
            .where(JournalLine.tenant_id == tenant_id, JournalLine.entry_id == entry_id)
            .group_by(Account.code)
        )
    ).all()
    return {code: int(balance) for code, balance in rows}


async def test_sales_invoice_posts_balanced_receivable_revenue_and_vat(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    response = await client.post(
        INVOICES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_date": JANUARY.isoformat(),
            "currency": "USD",
            "exchange_rate": "1",
            "post": True,
            "memo": "two widgets",
            "lines": [
                {
                    "description": "widget",
                    "quantity": "2",
                    "unit_price_minor": 5000,
                    "tax_code_id": str(master.tax_code_id),
                }
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "posted"
    assert body["number"].startswith("SI-2026-")
    assert body["subtotal_minor"] == 10000
    assert body["tax_minor"] == 1500
    assert body["total_minor"] == 11500
    assert body["base_total_minor"] == 11500
    assert body["journal_entry_id"] is not None

    async with engine.connect() as connection:
        balances = await _entry_lines(
            connection, tenant_id=identity.tenant_id, entry_id=UUID(body["journal_entry_id"])
        )
    assert balances == {"1100": 11500, "4000": -10000, "2100": -1500}

    fetched = await client.get(f"{INVOICES_URL}/{body['id']}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["total_minor"] == 11500
    listed = await client.get(INVOICES_URL, headers=headers)
    assert [item["id"] for item in listed.json()] == [body["id"]]


async def test_foreign_currency_invoice_uses_its_own_rate(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    response = await client.post(
        INVOICES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_date": JANUARY.isoformat(),
            "currency": "USD",
            "exchange_rate": "1.5",
            "post": True,
            "lines": [
                {
                    "description": "export",
                    "quantity": "1",
                    "unit_price_minor": 10000,
                    "tax_code_id": str(master.tax_code_id),
                }
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["subtotal_minor"] == 10000
    assert body["base_subtotal_minor"] == 15000
    assert body["base_tax_minor"] == 2250
    assert body["base_total_minor"] == 17250
    async with engine.connect() as connection:
        balances = await _entry_lines(
            connection, tenant_id=identity.tenant_id, entry_id=UUID(body["journal_entry_id"])
        )
    assert balances == {"1100": 17250, "4000": -15000, "2100": -2250}


async def test_duplicate_document_number_is_refused(
    client: AsyncClient,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    payload = {
        "customer_id": str(master.customer_id),
        "invoice_date": JANUARY.isoformat(),
        "number": "SI-2026-000042",
        "lines": [{"description": "one", "quantity": "1", "unit_price_minor": 100}],
    }
    first = await client.post(INVOICES_URL, json=payload, headers=headers)
    assert first.status_code == 201, first.text
    second = await client.post(INVOICES_URL, json=payload, headers=headers)
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "duplicate_document_number"


async def test_purchase_receipt_then_sales_issue_reconciles_stock_and_gl(
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    unit_of_work: UnitOfWork,
) -> None:
    identity = ledger.identity
    bill = await create_purchase_bill(
        unit_of_work,
        tenant_id=identity.tenant_id,
        actor_user_id=identity.user_id,
        request=PurchaseBillInput(
            company_id=identity.company_a,
            supplier_id=master.supplier_id,
            bill_date=JANUARY,
            post=True,
            lines=(
                PurchaseLineInput(
                    description="ten widgets",
                    quantity=Decimal("10"),
                    unit_price_minor=1000,
                    item_id=master.item_id,
                ),
            ),
        ),
    )
    assert bill.bill.status == "posted"
    assert bill.bill.total_minor == 10000

    first_sale = await create_sales_invoice(
        unit_of_work,
        tenant_id=identity.tenant_id,
        actor_user_id=identity.user_id,
        request=SalesInvoiceInput(
            company_id=identity.company_a,
            customer_id=master.customer_id,
            invoice_date=JANUARY,
            post=True,
            lines=(
                SalesLineInput(
                    description="four widgets",
                    quantity=Decimal("4"),
                    unit_price_minor=2000,
                    item_id=master.item_id,
                ),
            ),
        ),
    )
    assert first_sale.cogs_entry is not None

    second_sale = await create_sales_invoice(
        unit_of_work,
        tenant_id=identity.tenant_id,
        actor_user_id=identity.user_id,
        request=SalesInvoiceInput(
            company_id=identity.company_a,
            customer_id=master.customer_id,
            invoice_date=JANUARY,
            post=True,
            lines=(
                SalesLineInput(
                    description="six widgets",
                    quantity=Decimal("6"),
                    unit_price_minor=2000,
                    item_id=master.item_id,
                ),
            ),
        ),
    )

    async with engine.connect() as connection:
        stock = (
            await connection.execute(
                select(Item.quantity_on_hand, Item.stock_value_minor).where(
                    Item.id == master.item_id
                )
            )
        ).one()
        balances = await _account_balances(
            connection, tenant_id=identity.tenant_id, company_id=identity.company_a
        )
        move_count = await connection.scalar(
            select(func.count()).select_from(StockMove).where(StockMove.item_id == master.item_id)
        )
        cogs_balances = await _entry_lines(
            connection,
            tenant_id=identity.tenant_id,
            entry_id=second_sale.cogs_entry.entry.id if second_sale.cogs_entry else UUID(int=0),
        )
    assert stock.quantity_on_hand == 0
    assert stock.stock_value_minor == 0
    assert move_count == 3
    assert balances["1200"] == 0
    assert balances["5100"] == 10000
    assert balances["4000"] == -20000
    assert balances["1100"] == 20000
    assert balances["2000"] == -10000
    assert first_sale.journal_entry is not None
    assert cogs_balances == {"5100": 6000, "1200": -6000}
    assert second_sale.invoice.base_total_minor == 12000


async def test_closed_period_refuses_invoice_posting(
    client: AsyncClient,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    response = await client.post(
        INVOICES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_date": FEBRUARY.isoformat(),
            "post": True,
            "lines": [{"description": "late", "quantity": "1", "unit_price_minor": 100}],
        },
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "period_closed"

    draft = await client.post(
        INVOICES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_date": FEBRUARY.isoformat(),
            "lines": [{"description": "late", "quantity": "1", "unit_price_minor": 100}],
        },
        headers=headers,
    )
    assert draft.status_code == 201, draft.text
    assert draft.json()["status"] == "draft"


async def test_insufficient_stock_rolls_back_the_invoice(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    response = await client.post(
        INVOICES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_date": JANUARY.isoformat(),
            "post": True,
            "lines": [
                {
                    "description": "not in stock",
                    "quantity": "1",
                    "unit_price_minor": 500,
                    "item_id": str(master.item_id),
                }
            ],
        },
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "insufficient_stock"
    async with engine.connect() as connection:
        invoice_count = await connection.scalar(
            select(func.count())
            .select_from(JournalEntry)
            .where(JournalEntry.tenant_id == identity.tenant_id)
        )
    assert invoice_count == 0


async def test_catalog_endpoints_create_and_refuse_duplicate_codes(
    client: AsyncClient,
    company_admin: SeededIdentity,
) -> None:
    headers = await _headers(client, company_admin, company_admin.company_a)
    suffix = new_id().hex[-8:]
    customer = {"code": f"CAT-{suffix}", "name_ar": "عميل", "name_en": "Customer"}
    first = await client.post(f"{CATALOG_URL}/customers", json=customer, headers=headers)
    assert first.status_code == 201, first.text
    duplicate = await client.post(f"{CATALOG_URL}/customers", json=customer, headers=headers)
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["code"] == "duplicate_code"

    tax = await client.post(
        f"{CATALOG_URL}/tax-codes",
        json={
            "code": f"VAT-{suffix}",
            "name_ar": "ضريبة",
            "rate_basis_points": 1500,
            "jurisdiction": "IQ",
        },
        headers=headers,
    )
    assert tax.status_code == 201, tax.text
    assert tax.json()["rate_basis_points"] == 1500
    assert tax.json()["jurisdiction"] == "IQ"
    item = await client.post(
        f"{CATALOG_URL}/items",
        json={"code": f"ITEM-{suffix}", "name_ar": "صنف", "unit_of_measure": "piece"},
        headers=headers,
    )
    assert item.status_code == 201, item.text
    listed = await client.get(f"{CATALOG_URL}/items", headers=headers)
    assert listed.status_code == 200, listed.text
    assert any(row["code"] == f"ITEM-{suffix}" for row in listed.json())
