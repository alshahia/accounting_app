"""The phase-1 report pack: P&L, balance sheet, ledger, stock and VAT.

Each report is checked against documents posted through the API, so the numbers
come from the same posting rules the application uses.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import SeededLedger, seed_ledger
from tests.integration.test_documents import MasterData, seed_master
from tests.integration.test_returns import _create_bill, _create_invoice, _headers

pytestmark = pytest.mark.integration

PNL_URL = "/api/v1/reports/profit-and-loss"
BALANCE_SHEET_URL = "/api/v1/reports/balance-sheet"
GENERAL_LEDGER_URL = "/api/v1/reports/general-ledger"
INVENTORY_URL = "/api/v1/reports/inventory-valuation"
STOCK_MOVEMENT_URL = "/api/v1/reports/stock-movement"
TAX_RETURN_URL = "/api/v1/reports/tax-return"
CREDIT_NOTES_URL = "/api/v1/sales/credit-notes"
DEBIT_NOTES_URL = "/api/v1/purchasing/debit-notes"
JANUARY = date(2026, 1, 15)


@pytest.fixture
async def ledger(engine: AsyncEngine, identity) -> SeededLedger:
    """The identity fixture plus a chart and periods."""
    return await seed_ledger(engine, identity)


@pytest.fixture
async def master(engine: AsyncEngine, ledger: SeededLedger) -> MasterData:
    """Customer, supplier, tax code and item for the ledger's company."""
    return await seed_master(engine, ledger.identity)


async def _seed_documents(
    client: AsyncClient, headers: dict[str, str], master: MasterData
) -> tuple[dict[str, object], dict[str, object]]:
    bill = await _create_bill(
        client, headers, master, quantity="10", unit_price_minor=100, tax_code_id=master.tax_code_id
    )
    invoice = await _create_invoice(
        client, headers, master, quantity="2", unit_price_minor=500, tax_code_id=master.tax_code_id
    )
    credit = await client.post(
        CREDIT_NOTES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_id": str(invoice["id"]),
            "credit_note_date": date(2026, 1, 22).isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [
                {
                    "description": "return",
                    "quantity": "1",
                    "unit_price_minor": 500,
                    "item_id": str(master.item_id),
                    "tax_code_id": str(master.tax_code_id),
                    "invoice_line_id": str(invoice["lines"][0]["id"]),
                }
            ],
        },
        headers=headers,
    )
    assert credit.status_code == 201, credit.text
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
                    "tax_code_id": str(master.tax_code_id),
                    "bill_line_id": str(bill["lines"][0]["id"]),
                }
            ],
        },
        headers=headers,
    )
    assert debit.status_code == 201, debit.text
    return bill, invoice


async def test_profit_and_loss(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _seed_documents(client, headers, master)
    response = await client.get(
        PNL_URL, params={"start": "2026-01-01", "end": "2026-01-31"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_revenue_minor"] == 500
    assert body["total_expense_minor"] == 100
    assert body["net_profit_minor"] == 400
    revenue = {row["code"]: row["amount_minor"] for row in body["revenue"]}
    expenses = {row["code"]: row["amount_minor"] for row in body["expenses"]}
    assert revenue == {"4000": 500}
    assert expenses == {"5100": 100}


async def test_statement_comparatives(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _seed_documents(client, headers, master)

    pnl = await client.get(
        PNL_URL,
        params={"start": "2026-02-01", "end": "2026-02-28", "compare": "true"},
        headers=headers,
    )
    assert pnl.status_code == 200, pnl.text
    pnl_body = pnl.json()
    assert pnl_body["compare_start_date"] == "2026-01-04"
    assert pnl_body["compare_end_date"] == "2026-01-31"
    assert pnl_body["total_revenue_minor"] == 0
    assert pnl_body["compare_total_revenue_minor"] == 500
    assert pnl_body["compare_total_expense_minor"] == 100
    assert pnl_body["compare_net_profit_minor"] == 400
    prior_revenue = {row["code"]: row["compare_amount_minor"] for row in pnl_body["revenue"]}
    assert prior_revenue == {"4000": 500}

    sheet = await client.get(
        BALANCE_SHEET_URL,
        params={"as_of": "2027-01-15", "compare": "true"},
        headers=headers,
    )
    assert sheet.status_code == 200, sheet.text
    sheet_body = sheet.json()
    assert sheet_body["compare_as_of"] == "2026-01-15"
    prior = await client.get(BALANCE_SHEET_URL, params={"as_of": "2026-01-15"}, headers=headers)
    assert prior.status_code == 200, prior.text
    prior_body = prior.json()
    assert sheet_body["compare_total_assets_minor"] == prior_body["total_assets_minor"]
    assert sheet_body["compare_total_liabilities_minor"] == prior_body["total_liabilities_minor"]
    assert sheet_body["compare_total_equity_minor"] == prior_body["total_equity_minor"]
    prior_assets = {
        row["code"]: row["compare_amount_minor"]
        for row in sheet_body["assets"]
        if row["compare_amount_minor"]
    }
    assert prior_assets == {row["code"]: row["amount_minor"] for row in prior_body["assets"]}


async def test_balance_sheet_balances(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _seed_documents(client, headers, master)
    response = await client.get(BALANCE_SHEET_URL, params={"as_of": "2026-01-31"}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_assets_minor"] == 1275
    assert body["total_liabilities_minor"] == 875
    assert body["total_equity_minor"] == 400
    assert body["current_earnings_minor"] == 400
    assert body["balanced"] is True
    assets = {row["code"]: row["amount_minor"] for row in body["assets"]}
    liabilities = {row["code"]: row["amount_minor"] for row in body["liabilities"]}
    assert assets == {"1100": 575, "1200": 700}
    assert liabilities == {"2000": 920, "2100": -45}


async def test_general_ledger_running_balance(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _seed_documents(client, headers, master)
    response = await client.get(
        GENERAL_LEDGER_URL,
        params={
            "account_id": str(ledger.account_ids["1100"]),
            "start": "2026-01-01",
            "end": "2026-01-31",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_code"] == "1100"
    assert body["opening_balance_minor"] == 0
    assert body["closing_balance_minor"] == 575
    assert len(body["rows"]) == 2
    assert body["rows"][0]["debit_minor"] == 1150
    assert body["rows"][0]["balance_minor"] == 1150
    assert body["rows"][1]["credit_minor"] == 575
    assert body["rows"][1]["balance_minor"] == 575


async def test_inventory_and_stock_movement(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _seed_documents(client, headers, master)
    valuation = await client.get(INVENTORY_URL, headers=headers)
    assert valuation.status_code == 200, valuation.text
    body = valuation.json()
    assert body["total_value_minor"] == 700
    assert body["rows"][0]["quantity_on_hand"] == "7.0000"
    assert body["rows"][0]["stock_value_minor"] == 700
    assert body["meta"]["generated_by"] == identity.email
    assert body["meta"]["company_id"] == str(identity.company_a)

    movement = await client.get(
        STOCK_MOVEMENT_URL,
        params={"start": "2026-01-01", "end": "2026-01-31"},
        headers=headers,
    )
    assert movement.status_code == 200, movement.text
    moves = movement.json()
    assert len(moves["rows"]) == 4
    assert moves["total_receipt_value_minor"] == 1100
    assert moves["total_issue_value_minor"] == 400
    assert [row["move_type"] for row in moves["rows"]] == [
        "receipt",
        "issue",
        "receipt",
        "issue",
    ]
    assert {row["warehouse_code"] for row in moves["rows"]} == {"MAIN"}
    assert moves["rows"][0]["warehouse_name_ar"] == "المستودع الرئيسي"
    assert moves["meta"]["generated_by"] == identity.email
    assert moves["meta"]["filters"] == {"start": "2026-01-01", "end": "2026-01-31"}


async def test_tax_return(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _seed_documents(client, headers, master)
    response = await client.get(
        TAX_RETURN_URL, params={"start": "2026-01-01", "end": "2026-01-31"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["output_vat_minor"] == 150
    assert body["output_credit_notes_minor"] == 75
    assert body["input_vat_minor"] == 150
    assert body["input_debit_notes_minor"] == 30
    assert body["net_output_vat_minor"] == 75
    assert body["net_input_vat_minor"] == 120
    assert body["payable_minor"] == -45
    assert body["invoice_count"] == 1
    assert body["bill_count"] == 1


DOCUMENT_REGISTER_URL = "/api/v1/reports/document-register"


async def test_document_register_lists_every_document(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _seed_documents(client, headers, master)
    response = await client.get(
        DOCUMENT_REGISTER_URL,
        params={"start": "2026-01-01", "end": "2026-01-31"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["rows"]) == 4
    assert {row["document_type"] for row in body["rows"]} == {
        "sales_invoice",
        "purchase_bill",
        "sales_credit_note",
        "purchase_debit_note",
    }
    dates = [row["document_date"] for row in body["rows"]]
    assert dates == sorted(dates)
    invoice = next(row for row in body["rows"] if row["document_type"] == "sales_invoice")
    assert invoice["number"].startswith("SI-2026-")
    assert invoice["base_amount_minor"] == 1150
    assert body["total_amount_minor"] > 0
    for row in body["rows"]:
        UUID(row["document_id"])

    # Every register row drills to the source document it came from.
    invoice_detail = await client.get(
        f"/api/v1/sales/invoices/{invoice['document_id']}", headers=headers
    )
    assert invoice_detail.status_code == 200, invoice_detail.text
    assert invoice_detail.json()["number"] == invoice["number"]
    bill = next(row for row in body["rows"] if row["document_type"] == "purchase_bill")
    bill_detail = await client.get(
        f"/api/v1/purchasing/bills/{bill['document_id']}", headers=headers
    )
    assert bill_detail.status_code == 200, bill_detail.text
    assert bill_detail.json()["number"] == bill["number"]

    filtered = await client.get(
        DOCUMENT_REGISTER_URL,
        params={
            "start": "2026-01-01",
            "end": "2026-01-31",
            "document_type": "receipt",
        },
        headers=headers,
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["rows"] == []

    invalid = await client.get(
        DOCUMENT_REGISTER_URL,
        params={
            "start": "2026-01-01",
            "end": "2026-01-31",
            "document_type": "purchase_order",
        },
        headers=headers,
    )
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["code"] == "document_type_invalid"

    pdf = await client.get(
        f"{DOCUMENT_REGISTER_URL}.pdf",
        params={"start": "2026-01-01", "end": "2026-01-31"},
        headers=headers,
    )
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert pdf.content.startswith(b"%PDF")

    xlsx = await client.get(
        f"{DOCUMENT_REGISTER_URL}.xlsx",
        params={"start": "2026-01-01", "end": "2026-01-31"},
        headers=headers,
    )
    assert xlsx.status_code == 200, xlsx.text
    assert xlsx.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


async def test_invoice_and_bill_pdf_documents(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    bill, invoice = await _seed_documents(client, headers, master)

    invoice_pdf = await client.get(f"/api/v1/sales/invoices/{invoice['id']}/pdf", headers=headers)
    assert invoice_pdf.status_code == 200, invoice_pdf.text
    assert invoice_pdf.headers["content-type"].startswith("application/pdf")
    assert invoice_pdf.content.startswith(b"%PDF")
    assert invoice["number"] in invoice_pdf.headers["content-disposition"]

    bill_pdf = await client.get(f"/api/v1/purchasing/bills/{bill['id']}/pdf", headers=headers)
    assert bill_pdf.status_code == 200, bill_pdf.text
    assert bill_pdf.content.startswith(b"%PDF")
    assert bill["number"] in bill_pdf.headers["content-disposition"]

    missing = await client.get(
        f"/api/v1/sales/invoices/{ledger.account_ids['1100']}/pdf", headers=headers
    )
    assert missing.status_code == 404, missing.text
