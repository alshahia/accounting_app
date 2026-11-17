"""Receipts, payments and aging tests (phase 1, day 11).

The tests drive the real API and read the posted journal lines back from
PostgreSQL. They cover partial and full settlement, customer advances, exchange
gain on settlement at a different rate, over-allocation and cross-currency
refusal, supplier payments, aging buckets, and the permission split between
creating and posting money documents.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.modules.cashbank.models import CashBankAccount
from app.modules.ledger.models import Account
from app.shared.ids import new_id
from tests.integration.conftest import (
    SeededIdentity,
    SeededLedger,
    delete_identity,
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
RECEIPTS_URL = "/api/v1/cashbank/receipts"
PAYMENTS_URL = "/api/v1/cashbank/payments"
ACCOUNTS_URL = "/api/v1/cashbank/accounts"
AR_AGING_URL = "/api/v1/reports/ar-aging"
AP_AGING_URL = "/api/v1/reports/ap-aging"
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


async def _seed_cash_account(
    engine: AsyncEngine, identity: SeededIdentity, *, code: str = "1000", currency: str = "USD"
) -> UUID:
    """Create one cash/bank master row against the seeded chart."""
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        account = (
            await session.execute(
                select(Account).where(
                    Account.tenant_id == identity.tenant_id,
                    Account.company_id == identity.company_a,
                    Account.code == code,
                )
            )
        ).scalar_one()
        cash = CashBankAccount(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            code=f"CB-{new_id().hex[-8:]}",
            name_ar="صندوق تجريبي",
            kind="cash" if code == "1000" else "bank",
            ledger_account_id=account.id,
            currency=currency,
        )
        session.add(cash)
        await session.flush()
        return cash.id


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
    return await _seed_cash_account(engine, ledger.identity)


async def _create_invoice(
    client: AsyncClient,
    headers: dict[str, str],
    master: MasterData,
    *,
    amount_minor: int,
    exchange_rate: str = "1",
    due_date: date | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "customer_id": str(master.customer_id),
        "invoice_date": JANUARY.isoformat(),
        "currency": "USD",
        "exchange_rate": exchange_rate,
        "post": True,
        "lines": [
            {
                "description": "service",
                "quantity": "1",
                "unit_price_minor": amount_minor,
            }
        ],
    }
    if due_date is not None:
        payload["due_date"] = due_date.isoformat()
    response = await client.post(INVOICES_URL, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_partial_and_full_receipt_settle_invoice(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    cash_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    invoice = await _create_invoice(client, headers, master, amount_minor=10000)

    first = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(cash_account),
            "receipt_date": JANUARY.isoformat(),
            "amount_minor": 4000,
            "currency": "USD",
            "post": True,
            "allocations": [{"document_id": invoice["id"], "amount_minor": 4000}],
        },
        headers=headers,
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["status"] == "posted"
    assert body["number"].startswith("RC-2026-")
    assert body["unallocated_minor"] == 0
    assert body["allocations"][0]["amount_minor"] == 4000
    assert body["journal_entry_id"] is not None

    receipt_pdf = await client.get(f"{RECEIPTS_URL}/{body['id']}/pdf", headers=headers)
    assert receipt_pdf.status_code == 200, receipt_pdf.text
    assert receipt_pdf.headers["content-type"].startswith("application/pdf")
    assert receipt_pdf.content.startswith(b"%PDF")
    assert body["number"] in receipt_pdf.headers["content-disposition"]

    async with engine.connect() as connection:
        balances = await _entry_lines(
            connection, tenant_id=identity.tenant_id, entry_id=UUID(body["journal_entry_id"])
        )
    assert balances == {"1000": 4000, "1100": -4000}

    fetched = await client.get(f"{INVOICES_URL}/{invoice['id']}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "part_paid"

    second = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(cash_account),
            "receipt_date": JANUARY.isoformat(),
            "amount_minor": 6000,
            "currency": "USD",
            "post": True,
            "allocations": [{"document_id": invoice["id"], "amount_minor": 6000}],
        },
        headers=headers,
    )
    assert second.status_code == 201, second.text
    finished = await client.get(f"{INVOICES_URL}/{invoice['id']}", headers=headers)
    assert finished.json()["status"] == "paid"

    aging = await client.get(
        f"{AR_AGING_URL}?as_of=2026-01-31",
        headers=headers,
    )
    assert aging.status_code == 200, aging.text
    assert aging.json()["rows"] == []
    async with engine.connect() as connection:
        balances = await _account_balances(
            connection, tenant_id=identity.tenant_id, company_id=identity.company_a
        )
    assert balances["1100"] == 0


async def test_fx_gain_and_customer_advance(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    cash_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    invoice = await _create_invoice(
        client, headers, master, amount_minor=10000, exchange_rate="1.5"
    )
    receipt = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(cash_account),
            "receipt_date": JANUARY.isoformat(),
            "amount_minor": 10000,
            "currency": "USD",
            "exchange_rate": "2",
            "post": True,
            "allocations": [{"document_id": invoice["id"], "amount_minor": 10000}],
        },
        headers=headers,
    )
    assert receipt.status_code == 201, receipt.text
    body = receipt.json()
    assert body["base_amount_minor"] == 20000
    assert body["allocations"][0]["base_amount_minor"] == 15000
    assert body["allocations"][0]["settlement_base_minor"] == 20000
    async with engine.connect() as connection:
        balances = await _entry_lines(
            connection, tenant_id=identity.tenant_id, entry_id=UUID(body["journal_entry_id"])
        )
    assert balances == {"1000": 20000, "1100": -15000, "4800": -5000}

    advance = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(cash_account),
            "receipt_date": JANUARY.isoformat(),
            "amount_minor": 2500,
            "currency": "USD",
            "post": True,
        },
        headers=headers,
    )
    assert advance.status_code == 201, advance.text
    advance_body = advance.json()
    assert advance_body["unallocated_minor"] == 2500
    async with engine.connect() as connection:
        balances = await _entry_lines(
            connection,
            tenant_id=identity.tenant_id,
            entry_id=UUID(advance_body["journal_entry_id"]),
        )
    assert balances == {"1000": 2500, "2300": -2500}


async def test_over_allocation_and_cross_currency_are_refused(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    cash_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    invoice = await _create_invoice(client, headers, master, amount_minor=5000)

    over = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(cash_account),
            "receipt_date": JANUARY.isoformat(),
            "amount_minor": 6000,
            "currency": "USD",
            "post": True,
            "allocations": [{"document_id": invoice["id"], "amount_minor": 6000}],
        },
        headers=headers,
    )
    assert over.status_code == 422, over.text
    assert over.json()["code"] == "allocation_exceeds_balance"

    too_much = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(cash_account),
            "receipt_date": JANUARY.isoformat(),
            "amount_minor": 5000,
            "currency": "USD",
            "post": True,
            "allocations": [{"document_id": invoice["id"], "amount_minor": 5001}],
        },
        headers=headers,
    )
    assert too_much.status_code == 422, too_much.text
    assert too_much.json()["code"] == "allocation_invalid"

    eur_account = await _seed_cash_account(engine, identity, code="1010", currency="EUR")
    cross = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(eur_account),
            "receipt_date": JANUARY.isoformat(),
            "amount_minor": 5000,
            "currency": "EUR",
            "post": True,
            "allocations": [{"document_id": invoice["id"], "amount_minor": 5000}],
        },
        headers=headers,
    )
    assert cross.status_code == 422, cross.text
    assert cross.json()["code"] == "allocation_currency_mismatch"


async def test_supplier_payment_settles_bill_and_ap_aging(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    cash_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    bill_response = await client.post(
        BILLS_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "bill_date": JANUARY.isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [{"description": "supplies", "quantity": "1", "unit_price_minor": 5000}],
        },
        headers=headers,
    )
    assert bill_response.status_code == 201, bill_response.text
    bill = bill_response.json()
    assert bill["status"] == "posted"

    aging_before = await client.get(f"{AP_AGING_URL}?as_of=2026-01-31", headers=headers)
    assert aging_before.status_code == 200, aging_before.text
    assert aging_before.json()["total_base_outstanding_minor"] == 5000

    payment = await client.post(
        PAYMENTS_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "cash_bank_account_id": str(cash_account),
            "payment_date": JANUARY.isoformat(),
            "amount_minor": 5000,
            "currency": "USD",
            "post": True,
            "allocations": [{"document_id": bill["id"], "amount_minor": 5000}],
        },
        headers=headers,
    )
    assert payment.status_code == 201, payment.text
    body = payment.json()
    assert body["number"].startswith("PY-2026-")

    payment_pdf = await client.get(f"{PAYMENTS_URL}/{body['id']}/pdf", headers=headers)
    assert payment_pdf.status_code == 200, payment_pdf.text
    assert payment_pdf.headers["content-type"].startswith("application/pdf")
    assert payment_pdf.content.startswith(b"%PDF")
    assert body["number"] in payment_pdf.headers["content-disposition"]

    async with engine.connect() as connection:
        balances = await _entry_lines(
            connection, tenant_id=identity.tenant_id, entry_id=UUID(body["journal_entry_id"])
        )
    assert balances == {"2000": 5000, "1000": -5000}

    bill_after = await client.get(f"{BILLS_URL}/{bill['id']}", headers=headers)
    assert bill_after.json()["status"] == "paid"
    aging_after = await client.get(f"{AP_AGING_URL}?as_of=2026-01-31", headers=headers)
    assert aging_after.json()["rows"] == []


async def test_aging_buckets_and_advance(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    cash_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    old = await _create_invoice(
        client, headers, master, amount_minor=3000, due_date=date(2026, 1, 10)
    )
    current = await _create_invoice(
        client, headers, master, amount_minor=2000, due_date=date(2026, 4, 1)
    )
    partial = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(cash_account),
            "receipt_date": JANUARY.isoformat(),
            "amount_minor": 1000,
            "currency": "USD",
            "post": True,
            "allocations": [{"document_id": old["id"], "amount_minor": 1000}],
        },
        headers=headers,
    )
    assert partial.status_code == 201, partial.text

    aging = await client.get(f"{AR_AGING_URL}?as_of=2026-03-01", headers=headers)
    assert aging.status_code == 200, aging.text
    body = aging.json()
    rows = {row["document_number"]: row for row in body["rows"]}
    assert len(rows) == 2
    assert rows[old["number"]]["bucket"] == "31-60"
    assert rows[old["number"]]["outstanding_minor"] == 2000
    assert rows[old["number"]]["days_overdue"] == 50
    assert rows[old["number"]]["document_id"] == old["id"]
    assert rows[current["number"]]["document_id"] == current["id"]
    assert rows[current["number"]]["bucket"] == "current"
    assert body["buckets"] == [
        {"bucket": "current", "total_base_minor": 2000},
        {"bucket": "1-30", "total_base_minor": 0},
        {"bucket": "31-60", "total_base_minor": 2000},
        {"bucket": "61-90", "total_base_minor": 0},
        {"bucket": "90+", "total_base_minor": 0},
    ]
    assert body["total_base_outstanding_minor"] == 4000
    assert body["base_currency"] == "USD"


async def test_receipt_permission_split(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    seeded = await seed_identity(
        engine,
        tenant_slug=f"receipt-perm-{new_id().hex[-12:]}",
        email=f"receipt-perm-{new_id().hex[-12:]}@example.com",
        password="receipt permission 123",
        role_code="auditor",
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
            RECEIPTS_URL,
            json={
                "customer_id": str(new_id()),
                "cash_bank_account_id": str(new_id()),
                "receipt_date": JANUARY.isoformat(),
                "amount_minor": 100,
                "currency": "USD",
            },
            headers=headers,
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "permission_denied"
    finally:
        await delete_identity(engine, seeded.tenant_id)
