"""Customer and supplier statements (phase 1, slice 3).

Statements list the posted documents that make up a party's balance in base
currency. The tests drive the API, then check opening balance, each movement and
the closing balance, including a credit note and a receipt/payment.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.shared.ids import new_id
from tests.integration.conftest import SeededLedger, seed_ledger
from tests.integration.test_cashbank import _seed_cash_account
from tests.integration.test_documents import MasterData, seed_master
from tests.integration.test_returns import _create_bill, _create_invoice, _headers

pytestmark = pytest.mark.integration

CUSTOMER_STATEMENT_URL = "/api/v1/reports/customer-statement"
SUPPLIER_STATEMENT_URL = "/api/v1/reports/supplier-statement"
BILLS_URL = "/api/v1/purchasing/bills"
CREDIT_NOTES_URL = "/api/v1/sales/credit-notes"
DEBIT_NOTES_URL = "/api/v1/purchasing/debit-notes"
RECEIPTS_URL = "/api/v1/cashbank/receipts"
PAYMENTS_URL = "/api/v1/cashbank/payments"
JANUARY = date(2026, 1, 15)


@pytest.fixture
async def ledger(engine: AsyncEngine, identity) -> SeededLedger:
    """The identity fixture plus a chart and periods."""
    return await seed_ledger(engine, identity)


@pytest.fixture
async def master(engine: AsyncEngine, ledger: SeededLedger) -> MasterData:
    """Customer, supplier, tax code and item for the ledger's company."""
    return await seed_master(engine, ledger.identity)


async def _credit_note(
    client: AsyncClient,
    headers: dict[str, str],
    master: MasterData,
    invoice: dict[str, object],
    *,
    quantity: str,
    unit_price_minor: int,
) -> dict[str, object]:
    response = await client.post(
        CREDIT_NOTES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_id": str(invoice["id"]),
            "credit_note_date": date(2026, 1, 25).isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [
                {
                    "description": "return",
                    "quantity": quantity,
                    "unit_price_minor": unit_price_minor,
                    "item_id": str(master.item_id),
                    "invoice_line_id": str(invoice["lines"][0]["id"]),
                }
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_customer_statement_lists_movements_and_closing_balance(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    cash_id = await _seed_cash_account(engine, identity)
    await _create_bill(client, headers, master)
    invoice = await _create_invoice(client, headers, master, quantity="2", unit_price_minor=500)
    credit = await _credit_note(
        client, headers, master, invoice, quantity="1", unit_price_minor=500
    )
    receipt = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(cash_id),
            "receipt_date": date(2026, 1, 28).isoformat(),
            "amount_minor": 300,
            "currency": "USD",
            "post": True,
            "allocations": [{"document_id": invoice["id"], "amount_minor": 300}],
        },
        headers=headers,
    )
    assert receipt.status_code == 201, receipt.text

    response = await client.get(
        CUSTOMER_STATEMENT_URL,
        params={
            "customer_id": str(master.customer_id),
            "start": "2026-01-01",
            "end": "2026-01-31",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["opening_balance_minor"] == 0
    assert body["closing_balance_minor"] == 200
    assert body["total_debit_minor"] == 1000
    assert body["total_credit_minor"] == 800
    rows = body["rows"]
    assert [row["document_type"] for row in rows] == ["invoice", "credit_note", "receipt"]
    assert rows[0]["debit_minor"] == 1000
    assert rows[0]["balance_minor"] == 1000
    assert rows[1]["credit_minor"] == 500
    assert rows[1]["balance_minor"] == 500
    assert rows[2]["credit_minor"] == 300
    assert rows[2]["balance_minor"] == 200
    # Each movement carries the id of the document it came from.
    assert rows[0]["document_id"] == invoice["id"]
    assert rows[1]["document_id"] == credit["id"]
    assert rows[2]["document_id"] == receipt.json()["id"]


async def test_customer_statement_opening_balance(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    cash_id = await _seed_cash_account(engine, identity)
    await _create_bill(client, headers, master)
    await _create_invoice(client, headers, master, quantity="2", unit_price_minor=500)
    response = await client.get(
        CUSTOMER_STATEMENT_URL,
        params={
            "customer_id": str(master.customer_id),
            "start": "2026-01-20",
            "end": "2026-01-31",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["opening_balance_minor"] == 1000
    assert body["rows"] == []
    assert body["closing_balance_minor"] == 1000
    assert cash_id is not None


async def test_customer_statement_compares_with_previous_period(
    client: AsyncClient, engine: AsyncEngine, ledger: SeededLedger, master: MasterData
) -> None:
    """The comparison fields describe the equally long period before the statement."""
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    cash_id = await _seed_cash_account(engine, identity)
    await _create_bill(client, headers, master)
    invoice = await _create_invoice(client, headers, master, quantity="2", unit_price_minor=500)
    await _credit_note(client, headers, master, invoice, quantity="1", unit_price_minor=500)
    receipt = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(cash_id),
            "receipt_date": date(2026, 1, 28).isoformat(),
            "amount_minor": 300,
            "currency": "USD",
            "post": True,
            "allocations": [{"document_id": invoice["id"], "amount_minor": 300}],
        },
        headers=headers,
    )
    assert receipt.status_code == 201, receipt.text

    params = {
        "customer_id": str(master.customer_id),
        "start": "2026-02-01",
        "end": "2026-02-28",
    }
    response = await client.get(CUSTOMER_STATEMENT_URL, params=params, headers=headers)
    assert response.status_code == 200, response.text
    plain = response.json()
    assert plain["compare_closing_balance_minor"] is None
    assert plain["opening_balance_minor"] == 200

    compared = await client.get(
        CUSTOMER_STATEMENT_URL, params={**params, "compare": "true"}, headers=headers
    )
    assert compared.status_code == 200, compared.text
    body = compared.json()
    assert body["rows"] == []
    assert body["opening_balance_minor"] == 200
    assert body["closing_balance_minor"] == 200
    assert body["compare_start_date"] == "2026-01-04"
    assert body["compare_end_date"] == "2026-01-31"
    assert body["compare_opening_balance_minor"] == 0
    assert body["compare_total_debit_minor"] == 1000
    assert body["compare_total_credit_minor"] == 800
    assert body["compare_closing_balance_minor"] == 200
    assert body["meta"]["filters"]["compare"] == "true"


async def test_supplier_statement_lists_bills_and_payments(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    cash_id = await _seed_cash_account(engine, identity, code="1010")
    bill = await _create_bill(client, headers, master, quantity="10", unit_price_minor=100)
    debit = await client.post(
        DEBIT_NOTES_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "bill_id": str(bill["id"]),
            "debit_note_date": date(2026, 1, 23).isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [
                {
                    "description": "return",
                    "quantity": "2",
                    "unit_price_minor": 100,
                    "item_id": str(master.item_id),
                    "bill_line_id": str(bill["lines"][0]["id"]),
                }
            ],
        },
        headers=headers,
    )
    assert debit.status_code == 201, debit.text
    payment = await client.post(
        PAYMENTS_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "cash_bank_account_id": str(cash_id),
            "payment_date": date(2026, 1, 28).isoformat(),
            "amount_minor": 500,
            "currency": "USD",
            "post": True,
            "allocations": [{"document_id": bill["id"], "amount_minor": 500}],
        },
        headers=headers,
    )
    assert payment.status_code == 201, payment.text

    response = await client.get(
        SUPPLIER_STATEMENT_URL,
        params={
            "supplier_id": str(master.supplier_id),
            "start": "2026-01-01",
            "end": "2026-01-31",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["opening_balance_minor"] == 0
    assert body["closing_balance_minor"] == 300
    assert body["total_debit_minor"] == 1000
    assert body["total_credit_minor"] == 700
    rows = body["rows"]
    assert [row["document_type"] for row in rows] == ["bill", "debit_note", "payment"]
    assert rows[0]["debit_minor"] == 1000
    assert rows[0]["balance_minor"] == 1000
    assert rows[1]["credit_minor"] == 200
    assert rows[1]["balance_minor"] == 800
    assert rows[2]["credit_minor"] == 500
    assert rows[2]["balance_minor"] == 300


async def test_statement_of_unknown_party_is_not_found(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    response = await client.get(
        CUSTOMER_STATEMENT_URL,
        params={
            "customer_id": str(new_id()),
            "start": "2026-01-01",
            "end": "2026-01-31",
        },
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "customer_not_found"


async def test_statement_invalid_range_is_refused(
    client: AsyncClient,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    response = await client.get(
        CUSTOMER_STATEMENT_URL,
        params={
            "customer_id": str(master.customer_id),
            "start": "2026-01-31",
            "end": "2026-01-01",
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "report_range_invalid"
