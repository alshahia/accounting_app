"""Cash transfers, cheques and bank reconciliation tests (phase 1, day 12).

The tests drive the real API and read posted journal lines back from
PostgreSQL. They cover moving money between two accounts, the received-cheque
lifecycle (in hand, deposited, cleared, bounced), the issued-cheque lifecycle
(issued, cleared, cancelled), and importing and matching bank statement lines
against already-posted documents.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import SeededIdentity, SeededLedger
from tests.integration.test_cashbank import _headers, _seed_cash_account
from tests.integration.test_documents import (
    MasterData,
    _entry_lines,
    seed_master,
)

pytestmark = pytest.mark.integration

TRANSFERS_URL = "/api/v1/cashbank/transfers"
CHEQUES_URL = "/api/v1/cashbank/cheques"
STATEMENTS_URL = "/api/v1/cashbank/bank-statements"
RECEIPTS_URL = "/api/v1/cashbank/receipts"
INVOICES_URL = "/api/v1/sales/invoices"
BILLS_URL = "/api/v1/purchasing/bills"
JANUARY = date(2026, 1, 15)


@pytest.fixture
async def ledger(engine: AsyncEngine, identity: SeededIdentity) -> SeededLedger:
    """The identity fixture plus a chart and January/February periods."""
    from tests.integration.conftest import seed_ledger

    return await seed_ledger(engine, identity)


@pytest.fixture
async def master(engine: AsyncEngine, ledger: SeededLedger) -> MasterData:
    """Customer, supplier, tax code and item for the ledger's company."""
    return await seed_master(engine, ledger.identity)


@pytest.fixture
async def cash_account(engine: AsyncEngine, ledger: SeededLedger) -> UUID:
    """A USD cash account linked to the seeded chart."""
    return await _seed_cash_account(engine, ledger.identity)


@pytest.fixture
async def bank_account(engine: AsyncEngine, ledger: SeededLedger) -> UUID:
    """A USD bank account linked to the seeded chart."""
    return await _seed_cash_account(engine, ledger.identity, code="1010")


async def _create_invoice(
    client: AsyncClient,
    headers: dict[str, str],
    master: MasterData,
    *,
    amount_minor: int,
) -> dict[str, object]:
    response = await client.post(
        INVOICES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_date": JANUARY.isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [
                {"description": "service", "quantity": "1", "unit_price_minor": amount_minor}
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_bill(
    client: AsyncClient, headers: dict[str, str], master: MasterData, *, amount_minor: int
) -> dict[str, object]:
    response = await client.post(
        BILLS_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "bill_date": JANUARY.isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [
                {"description": "supplies", "quantity": "1", "unit_price_minor": amount_minor}
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _lines(
    engine: AsyncEngine, identity: SeededIdentity, entry_id: str | None
) -> dict[str, int]:
    assert entry_id is not None
    async with engine.connect() as connection:
        return await _entry_lines(connection, tenant_id=identity.tenant_id, entry_id=UUID(entry_id))


async def test_cash_transfer_moves_money_between_accounts(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    cash_account: UUID,
    bank_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)

    response = await client.post(
        TRANSFERS_URL,
        json={
            "transfer_date": JANUARY.isoformat(),
            "from_account_id": str(cash_account),
            "to_account_id": str(bank_account),
            "amount_minor": 2500,
            "currency": "USD",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    transfer = response.json()
    assert transfer["number"].startswith("CT-2026-")
    assert transfer["status"] == "posted"
    assert await _lines(engine, identity, transfer["journal_entry_id"]) == {
        "1000": -2500,
        "1010": 2500,
    }

    listing = await client.get(TRANSFERS_URL, headers=headers)
    assert listing.status_code == 200, listing.text
    assert [row["id"] for row in listing.json()] == [transfer["id"]]

    detail = await client.get(f"{TRANSFERS_URL}/{transfer['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == transfer["id"]
    assert body["number"] == transfer["number"]
    assert body["from_account_name_ar"]
    assert body["to_account_name_ar"]
    assert body["journal_entry_id"] == transfer["journal_entry_id"]

    missing = await client.get(f"{TRANSFERS_URL}/{UUID(int=0)}", headers=headers)
    assert missing.status_code == 404, missing.text
    assert missing.json()["code"] == "cash_transfer_not_found"

    # The document register lists the transfer and its row drills back to it.
    register = await client.get(
        "/api/v1/reports/document-register",
        params={
            "start": JANUARY.isoformat(),
            "end": JANUARY.isoformat(),
            "document_type": "cash_transfer",
        },
        headers=headers,
    )
    assert register.status_code == 200, register.text
    rows = register.json()["rows"]
    assert [row["document_id"] for row in rows] == [transfer["id"]]
    assert rows[0]["number"] == transfer["number"]
    linked = await client.get(f"{TRANSFERS_URL}/{rows[0]['document_id']}", headers=headers)
    assert linked.status_code == 200, linked.text
    assert linked.json()["number"] == rows[0]["number"]


async def test_received_cheque_lifecycle_to_cleared(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    bank_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    invoice = await _create_invoice(client, headers, master, amount_minor=5000)

    created = await client.post(
        CHEQUES_URL,
        json={
            "direction": "received",
            "number": "CH-5001",
            "cash_bank_account_id": str(bank_account),
            "issue_date": JANUARY.isoformat(),
            "amount_minor": 5000,
            "customer_id": str(master.customer_id),
            "post": True,
            "allocations": [{"document_id": invoice["id"], "amount_minor": 5000}],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    cheque = created.json()
    assert cheque["status"] == "in_hand"
    assert await _lines(engine, identity, cheque["events"][0]["journal_entry_id"]) == {
        "1100": -5000,
        "1230": 5000,
    }
    invoice_after = await client.get(f"{INVOICES_URL}/{invoice['id']}", headers=headers)
    assert invoice_after.json()["status"] == "paid"

    deposited = await client.post(
        f"{CHEQUES_URL}/{cheque['id']}/deposit",
        json={"event_date": date(2026, 1, 20).isoformat()},
        headers=headers,
    )
    assert deposited.status_code == 200, deposited.text
    deposited_body = deposited.json()
    assert deposited_body["status"] == "deposited"
    deposit_event = deposited_body["events"][-1]
    assert await _lines(engine, identity, deposit_event["journal_entry_id"]) == {
        "1010": 5000,
        "1230": -5000,
    }

    cleared = await client.post(
        f"{CHEQUES_URL}/{cheque['id']}/clear",
        json={"event_date": date(2026, 1, 25).isoformat()},
        headers=headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["status"] == "cleared"
    assert cleared.json()["events"][-1]["journal_entry_id"] is None


async def test_received_cheque_bounce_restores_receivable(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    cash_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    invoice = await _create_invoice(client, headers, master, amount_minor=3000)

    created = await client.post(
        CHEQUES_URL,
        json={
            "direction": "received",
            "number": "CH-5002",
            "cash_bank_account_id": str(cash_account),
            "issue_date": JANUARY.isoformat(),
            "amount_minor": 3000,
            "customer_id": str(master.customer_id),
            "post": True,
            "allocations": [{"document_id": invoice["id"], "amount_minor": 3000}],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    cheque = created.json()

    bounced = await client.post(
        f"{CHEQUES_URL}/{cheque['id']}/bounce",
        json={"event_date": date(2026, 1, 28).isoformat()},
        headers=headers,
    )
    assert bounced.status_code == 200, bounced.text
    body = bounced.json()
    assert body["status"] == "bounced"
    assert await _lines(engine, identity, body["events"][-1]["journal_entry_id"]) == {
        "1230": -3000,
        "1100": 3000,
    }
    invoice_after = await client.get(f"{INVOICES_URL}/{invoice['id']}", headers=headers)
    assert invoice_after.json()["status"] == "posted"


async def test_issued_cheque_lifecycle_to_cleared(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    bank_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    bill = await _create_bill(client, headers, master, amount_minor=4000)

    created = await client.post(
        CHEQUES_URL,
        json={
            "direction": "issued",
            "number": "CH-7001",
            "cash_bank_account_id": str(bank_account),
            "issue_date": JANUARY.isoformat(),
            "amount_minor": 4000,
            "supplier_id": str(master.supplier_id),
            "post": True,
            "allocations": [{"document_id": bill["id"], "amount_minor": 4000}],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    cheque = created.json()
    assert cheque["status"] == "issued"
    assert await _lines(engine, identity, cheque["events"][0]["journal_entry_id"]) == {
        "2000": 4000,
        "2200": -4000,
    }
    bill_after = await client.get(f"{BILLS_URL}/{bill['id']}", headers=headers)
    assert bill_after.json()["status"] == "paid"

    cleared = await client.post(
        f"{CHEQUES_URL}/{cheque['id']}/clear",
        json={"event_date": date(2026, 1, 30).isoformat()},
        headers=headers,
    )
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["status"] == "cleared"
    assert await _lines(engine, identity, body["events"][-1]["journal_entry_id"]) == {
        "2200": 4000,
        "1010": -4000,
    }


async def test_issued_cheque_cancel_restores_payable(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    cash_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    bill = await _create_bill(client, headers, master, amount_minor=1200)

    created = await client.post(
        CHEQUES_URL,
        json={
            "direction": "issued",
            "number": "CH-7002",
            "cash_bank_account_id": str(cash_account),
            "issue_date": JANUARY.isoformat(),
            "amount_minor": 1200,
            "supplier_id": str(master.supplier_id),
            "post": True,
            "allocations": [{"document_id": bill["id"], "amount_minor": 1200}],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    cheque = created.json()

    cancelled = await client.post(
        f"{CHEQUES_URL}/{cheque['id']}/cancel",
        json={"event_date": date(2026, 1, 22).isoformat()},
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["status"] == "cancelled"
    assert await _lines(engine, identity, body["events"][-1]["journal_entry_id"]) == {
        "2200": 1200,
        "2000": -1200,
    }
    bill_after = await client.get(f"{BILLS_URL}/{bill['id']}", headers=headers)
    assert bill_after.json()["status"] == "posted"


async def test_bank_statement_import_and_match_receipt(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    bank_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    receipt = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(bank_account),
            "receipt_date": JANUARY.isoformat(),
            "amount_minor": 700,
            "currency": "USD",
            "post": True,
        },
        headers=headers,
    )
    assert receipt.status_code == 201, receipt.text

    imported = await client.post(
        STATEMENTS_URL,
        json={
            "cash_bank_account_id": str(bank_account),
            "currency": "USD",
            "lines": [
                {
                    "statement_date": JANUARY.isoformat(),
                    "description": "customer deposit",
                    "amount_minor": 700,
                    "reference": "RC-1",
                }
            ],
        },
        headers=headers,
    )
    assert imported.status_code == 201, imported.text
    line = imported.json()[0]
    assert line["status"] == "unmatched"

    matched = await client.post(
        f"{STATEMENTS_URL}/{line['id']}/match",
        json={"source_type": "receipt", "source_id": receipt.json()["id"]},
        headers=headers,
    )
    assert matched.status_code == 200, matched.text
    assert matched.json()["status"] == "matched"
    assert matched.json()["matched_source_type"] == "receipt"

    unmatch = await client.post(f"{STATEMENTS_URL}/{line['id']}/unmatch", headers=headers)
    assert unmatch.status_code == 200, unmatch.text
    assert unmatch.json()["status"] == "unmatched"

    listed = await client.get(f"{STATEMENTS_URL}?status_filter=unmatched", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [line["id"]]
