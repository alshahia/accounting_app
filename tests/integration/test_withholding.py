"""Withholding tax at receipt and payment (phase 1, slice 4).

A withholding code is applied when money moves: the payer keeps part of the
gross settlement, so the cash leg is net while the receivable/payable is cleared
in full and the difference sits on account 1250 (receivable) or 2150 (payable).
The rate comes from the tax code row, never from code.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.modules.cashbank.models import CashBankAccount
from app.modules.catalog.models import TaxCode, TaxCodeRate
from app.modules.ledger.models import Account
from app.shared.ids import new_id
from tests.integration.conftest import SeededIdentity, SeededLedger, seed_ledger
from tests.integration.test_documents import MasterData, _account_balances, seed_master

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
INVOICES_URL = "/api/v1/sales/invoices"
BILLS_URL = "/api/v1/purchasing/bills"
RECEIPTS_URL = "/api/v1/cashbank/receipts"
PAYMENTS_URL = "/api/v1/cashbank/payments"
JANUARY = date(2026, 1, 15)


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


async def _seed_cash_account(engine: AsyncEngine, ledger: SeededLedger) -> UUID:
    identity = ledger.identity
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        account = (
            await session.execute(
                select(Account).where(
                    Account.tenant_id == identity.tenant_id,
                    Account.company_id == identity.company_a,
                    Account.code == "1000",
                )
            )
        ).scalar_one()
        cash = CashBankAccount(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            code=f"CB-{new_id().hex[-8:]}",
            name_ar="صندوق تجريبي",
            kind="cash",
            ledger_account_id=account.id,
            currency="USD",
        )
        session.add(cash)
        await session.flush()
        return cash.id


async def _seed_withholding_code(engine: AsyncEngine, ledger: SeededLedger, *, rate: int) -> UUID:
    identity = ledger.identity
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        code = TaxCode(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            code=f"WHT-{new_id().hex[-8:]}",
            name_ar="ضريبة مستقطعة",
            rate_basis_points=rate,
            kind="withholding",
        )
        session.add(code)
        await session.flush()
        session.add(
            TaxCodeRate(
                tenant_id=identity.tenant_id,
                company_id=identity.company_a,
                tax_code_id=code.id,
                rate_basis_points=rate,
                valid_from=date(2000, 1, 1),
            )
        )
        await session.flush()
        return code.id


@pytest.fixture
async def ledger(engine: AsyncEngine, identity: SeededIdentity) -> SeededLedger:
    """The identity fixture plus a chart and January/February periods."""
    return await seed_ledger(engine, identity)


@pytest.fixture
async def master(engine: AsyncEngine, ledger: SeededLedger) -> MasterData:
    """Customer, supplier, tax code and item for the ledger's company."""
    return await seed_master(engine, ledger.identity)


@pytest.fixture
async def cash_account(engine: AsyncEngine, ledger: SeededLedger) -> UUID:
    """A USD cash account linked to the seeded chart."""
    return await _seed_cash_account(engine, ledger)


@pytest.fixture
async def withholding_ten(engine: AsyncEngine, ledger: SeededLedger) -> UUID:
    """A 10% withholding tax code."""
    return await _seed_withholding_code(engine, ledger, rate=1000)


@pytest.fixture
async def withholding_five(engine: AsyncEngine, ledger: SeededLedger) -> UUID:
    """A 5% withholding tax code."""
    return await _seed_withholding_code(engine, ledger, rate=500)


async def _create_invoice(
    client: AsyncClient, headers: dict[str, str], master: MasterData
) -> dict[str, object]:
    response = await client.post(
        INVOICES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_date": JANUARY.isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [{"description": "sale", "quantity": "1", "unit_price_minor": 1000}],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_bill(
    client: AsyncClient, headers: dict[str, str], master: MasterData
) -> dict[str, object]:
    response = await client.post(
        BILLS_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "bill_date": JANUARY.isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [{"description": "purchase", "quantity": "1", "unit_price_minor": 1000}],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_receipt_withholding_clears_invoice_and_books_receivable(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    cash_account: UUID,
    withholding_ten: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    invoice = await _create_invoice(client, headers, master)
    response = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(cash_account),
            "receipt_date": JANUARY.isoformat(),
            "currency": "USD",
            "amount_minor": 1000,
            "post": True,
            "withholding_tax_code_id": str(withholding_ten),
            "allocations": [{"document_id": invoice["id"], "amount_minor": 1000}],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["withholding_tax_code_id"] == str(withholding_ten)
    assert body["withholding_base_minor"] == 1000
    assert body["withholding_amount_minor"] == 100
    assert body["base_withholding_minor"] == 100

    settled = await client.get(f"{INVOICES_URL}/{invoice['id']}", headers=headers)
    assert settled.json()["status"] == "paid"
    async with engine.connect() as connection:
        balances = await _account_balances(
            connection, tenant_id=identity.tenant_id, company_id=identity.company_a
        )
    assert balances["1100"] == 0
    assert balances["1000"] == 900
    assert balances["1250"] == 100


async def test_payment_withholding_clears_bill_and_books_payable(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    cash_account: UUID,
    withholding_five: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    bill = await _create_bill(client, headers, master)
    response = await client.post(
        PAYMENTS_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "cash_bank_account_id": str(cash_account),
            "payment_date": JANUARY.isoformat(),
            "currency": "USD",
            "amount_minor": 1000,
            "post": True,
            "withholding_tax_code_id": str(withholding_five),
            "allocations": [{"document_id": bill["id"], "amount_minor": 1000}],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["withholding_amount_minor"] == 50
    assert body["base_withholding_minor"] == 50

    settled = await client.get(f"{BILLS_URL}/{bill['id']}", headers=headers)
    assert settled.json()["status"] == "paid"
    async with engine.connect() as connection:
        balances = await _account_balances(
            connection, tenant_id=identity.tenant_id, company_id=identity.company_a
        )
    assert balances["2000"] == 0
    assert balances["1000"] == -950
    assert balances["2150"] == -50


async def test_draft_receipt_posts_with_stored_withholding(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    cash_account: UUID,
    withholding_ten: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    invoice = await _create_invoice(client, headers, master)
    created = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(cash_account),
            "receipt_date": JANUARY.isoformat(),
            "currency": "USD",
            "amount_minor": 1000,
            "withholding_tax_code_id": str(withholding_ten),
            "allocations": [{"document_id": invoice["id"], "amount_minor": 1000}],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    receipt_id = created.json()["id"]
    assert created.json()["status"] == "draft"
    posted = await client.post(f"{RECEIPTS_URL}/{receipt_id}/post", headers=headers)
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == "posted"
    async with engine.connect() as connection:
        balances = await _account_balances(
            connection, tenant_id=identity.tenant_id, company_id=identity.company_a
        )
    assert balances["1250"] == 100


async def test_withholding_code_cannot_be_used_on_invoice_line(
    client: AsyncClient,
    ledger: SeededLedger,
    master: MasterData,
    withholding_ten: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    response = await client.post(
        INVOICES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_date": JANUARY.isoformat(),
            "currency": "USD",
            "lines": [
                {
                    "description": "sale",
                    "quantity": "1",
                    "unit_price_minor": 1000,
                    "tax_code_id": str(withholding_ten),
                }
            ],
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "tax_code_not_applicable"


async def test_non_withholding_code_cannot_be_used_at_payment(
    client: AsyncClient,
    ledger: SeededLedger,
    master: MasterData,
    cash_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    bill = await _create_bill(client, headers, master)
    response = await client.post(
        PAYMENTS_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "cash_bank_account_id": str(cash_account),
            "payment_date": JANUARY.isoformat(),
            "currency": "USD",
            "amount_minor": 1000,
            "withholding_tax_code_id": str(master.tax_code_id),
            "allocations": [{"document_id": bill["id"], "amount_minor": 1000}],
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "tax_code_not_withholding"
