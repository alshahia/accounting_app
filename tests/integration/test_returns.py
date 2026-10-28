"""Credit notes and debit notes (phase 1): returns, caps and reversals.

The tests create a posted bill and invoice through the API, then return goods
with a credit note and a debit note. They check the posted journal lines, the
stock position, the receivable/payable balances, the no-double-return cap and
the customer/supplier match rule.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.modules.catalog.models import Customer
from app.shared.ids import new_id
from tests.integration.conftest import (
    SeededIdentity,
    SeededLedger,
    seed_identity,
    seed_ledger,
)
from tests.integration.test_documents import (
    MasterData,
    _account_balances,
    _entry_lines,
    seed_master,
)

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
INVOICES_URL = "/api/v1/sales/invoices"
BILLS_URL = "/api/v1/purchasing/bills"
CREDIT_NOTES_URL = "/api/v1/sales/credit-notes"
DEBIT_NOTES_URL = "/api/v1/purchasing/debit-notes"
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


@pytest.fixture
async def ledger(engine: AsyncEngine, identity: SeededIdentity) -> SeededLedger:
    """The identity fixture plus a chart and January/February periods."""
    return await seed_ledger(engine, identity)


@pytest.fixture
async def master(engine: AsyncEngine, ledger: SeededLedger) -> MasterData:
    """Customer, supplier, tax code and item for the ledger's company."""
    return await seed_master(engine, ledger.identity)


async def _create_bill(
    client: AsyncClient,
    headers: dict[str, str],
    master: MasterData,
    *,
    quantity: str = "10",
    unit_price_minor: int = 100,
    tax_code_id: UUID | None = None,
) -> dict[str, object]:
    line: dict[str, object] = {
        "description": "supplies",
        "quantity": quantity,
        "unit_price_minor": unit_price_minor,
        "item_id": str(master.item_id),
    }
    if tax_code_id is not None:
        line["tax_code_id"] = str(tax_code_id)
    response = await client.post(
        BILLS_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "bill_date": JANUARY.isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [line],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_invoice(
    client: AsyncClient,
    headers: dict[str, str],
    master: MasterData,
    *,
    quantity: str = "2",
    unit_price_minor: int = 500,
    tax_code_id: UUID | None = None,
) -> dict[str, object]:
    line: dict[str, object] = {
        "description": "sale",
        "quantity": quantity,
        "unit_price_minor": unit_price_minor,
        "item_id": str(master.item_id),
    }
    if tax_code_id is not None:
        line["tax_code_id"] = str(tax_code_id)
    response = await client.post(
        INVOICES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_date": JANUARY.isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [line],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_credit_note_returns_stock_and_relieves_receivable(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _create_bill(client, headers, master)
    invoice = await _create_invoice(client, headers, master)
    invoice_line = invoice["lines"][0]
    assert invoice["total_minor"] == 1000

    credit = await client.post(
        CREDIT_NOTES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_id": str(invoice["id"]),
            "credit_note_date": date(2026, 1, 20).isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [
                {
                    "description": "returned one",
                    "quantity": "1",
                    "unit_price_minor": 500,
                    "item_id": str(master.item_id),
                    "invoice_line_id": str(invoice_line["id"]),
                }
            ],
        },
        headers=headers,
    )
    assert credit.status_code == 201, credit.text
    body = credit.json()
    assert body["status"] == "posted"
    assert body["number"].startswith("CN-2026-")
    assert body["total_minor"] == 500

    async with engine.connect() as connection:
        lines = await _entry_lines(
            connection, tenant_id=identity.tenant_id, entry_id=UUID(body["journal_entry_id"])
        )
        balances = await _account_balances(
            connection, tenant_id=identity.tenant_id, company_id=identity.company_a
        )
        item = (
            await connection.execute(
                text("SELECT quantity_on_hand, stock_value_minor FROM item WHERE id = :id"),
                {"id": str(master.item_id)},
            )
        ).one()
    assert lines == {"4000": 500, "1100": -500, "1200": 100, "5100": -100}
    assert balances["1100"] == 500
    assert balances["4000"] == -500
    assert balances["1200"] == 900
    assert balances["5100"] == 100
    assert int(item.quantity_on_hand) == 9
    assert int(item.stock_value_minor) == 900


async def test_credit_note_cannot_return_more_than_invoiced(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _create_bill(client, headers, master)
    invoice = await _create_invoice(client, headers, master, quantity="2")
    invoice_line = invoice["lines"][0]

    def payload(quantity: str) -> dict[str, object]:
        return {
            "customer_id": str(master.customer_id),
            "invoice_id": str(invoice["id"]),
            "credit_note_date": date(2026, 1, 20).isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [
                {
                    "description": "return",
                    "quantity": quantity,
                    "unit_price_minor": 500,
                    "item_id": str(master.item_id),
                    "invoice_line_id": str(invoice_line["id"]),
                }
            ],
        }

    first = await client.post(CREDIT_NOTES_URL, json=payload("2"), headers=headers)
    assert first.status_code == 201, first.text
    second = await client.post(CREDIT_NOTES_URL, json=payload("1"), headers=headers)
    assert second.status_code == 422, second.text
    assert second.json()["code"] == "credit_note_quantity_exceeds_invoice"


async def test_debit_note_returns_stock_and_relieves_payable(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    bill = await _create_bill(
        client,
        headers,
        master,
        quantity="10",
        unit_price_minor=100,
        tax_code_id=master.tax_code_id,
    )
    bill_line = bill["lines"][0]
    assert bill["total_minor"] == 1150

    debit = await client.post(
        DEBIT_NOTES_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "bill_id": str(bill["id"]),
            "debit_note_date": date(2026, 1, 20).isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [
                {
                    "description": "returned two",
                    "quantity": "2",
                    "unit_price_minor": 100,
                    "item_id": str(master.item_id),
                    "tax_code_id": str(master.tax_code_id),
                    "bill_line_id": str(bill_line["id"]),
                }
            ],
        },
        headers=headers,
    )
    assert debit.status_code == 201, debit.text
    body = debit.json()
    assert body["status"] == "posted"
    assert body["number"].startswith("DN-2026-")
    assert body["total_minor"] == 230

    async with engine.connect() as connection:
        lines = await _entry_lines(
            connection, tenant_id=identity.tenant_id, entry_id=UUID(body["journal_entry_id"])
        )
        balances = await _account_balances(
            connection, tenant_id=identity.tenant_id, company_id=identity.company_a
        )
        item = (
            await connection.execute(
                text("SELECT quantity_on_hand, stock_value_minor FROM item WHERE id = :id"),
                {"id": str(master.item_id)},
            )
        ).one()
    assert lines == {"2000": 230, "1200": -200, "2100": -30}
    assert balances["2000"] == -920
    assert balances["1200"] == 800
    assert balances["2100"] == 120
    assert int(item.quantity_on_hand) == 8
    assert int(item.stock_value_minor) == 800


async def test_debit_note_cannot_return_more_than_billed(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    bill = await _create_bill(client, headers, master, quantity="3")
    bill_line = bill["lines"][0]

    def payload(quantity: str) -> dict[str, object]:
        return {
            "supplier_id": str(master.supplier_id),
            "bill_id": str(bill["id"]),
            "debit_note_date": date(2026, 1, 20).isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [
                {
                    "description": "return",
                    "quantity": quantity,
                    "unit_price_minor": 100,
                    "item_id": str(master.item_id),
                    "bill_line_id": str(bill_line["id"]),
                }
            ],
        }

    first = await client.post(DEBIT_NOTES_URL, json=payload("3"), headers=headers)
    assert first.status_code == 201, first.text
    second = await client.post(DEBIT_NOTES_URL, json=payload("1"), headers=headers)
    assert second.status_code == 422, second.text
    assert second.json()["code"] == "debit_note_quantity_exceeds_bill"


async def test_credit_note_customer_must_match_invoice(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _create_bill(client, headers, master)
    invoice = await _create_invoice(client, headers, master)
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        other_customer = Customer(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            code=f"C-{new_id().hex[-8:]}",
            name_ar="عميل آخر",
        )
        session.add(other_customer)
        await session.flush()
    response = await client.post(
        CREDIT_NOTES_URL,
        json={
            "customer_id": str(other_customer.id),
            "invoice_id": str(invoice["id"]),
            "credit_note_date": date(2026, 1, 20).isoformat(),
            "currency": "USD",
            "lines": [
                {
                    "description": "return",
                    "quantity": "1",
                    "unit_price_minor": 500,
                    "item_id": str(master.item_id),
                    "invoice_line_id": str(invoice["lines"][0]["id"]),
                }
            ],
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "credit_note_customer_mismatch"


async def test_returns_permission_split(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    seeded = await seed_identity(
        engine,
        tenant_slug=f"returns-perm-{new_id().hex[-12:]}",
        email=f"returns-perm-{new_id().hex[-12:]}@example.com",
        password="returns permission 123",
        role_code="purchasing",
    )
    try:
        login = await client.post(
            LOGIN_URL, json={"email": seeded.email, "password": seeded.password}
        )
        assert login.status_code == 200, login.text
        headers = {
            "Authorization": f"Bearer {login.json()['access_token']}",
            "X-Company-Id": str(seeded.company_a),
        }
        response = await client.post(
            CREDIT_NOTES_URL,
            json={
                "customer_id": str(new_id()),
                "credit_note_date": JANUARY.isoformat(),
                "currency": "USD",
                "lines": [{"description": "x", "quantity": "1", "unit_price_minor": 1}],
            },
            headers=headers,
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "permission_denied"
    finally:
        from tests.integration.conftest import delete_identity

        await delete_identity(engine, seeded.tenant_id)
