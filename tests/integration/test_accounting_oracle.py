"""Accounting oracle: replicate an externally posted scenario and compare roles.

Provenance of the expected figures (facts only, no copied implementation):

* An Odoo 18 demo database (LGPL-3.0 demo data) was initialised, and a fixed
  scenario was posted there through Odoo's own document workflow by
  `work/scripts/bench_odoo_scenario.py`: two customer invoices (one with 15%
  sales tax), one supplier bill (with 15% purchase tax), a partial customer
  receipt and a partial supplier payment, all in USD in November 2026.
* The posted journal lines were dumped to `work/bench/odoo_scenario.json`
  and mapped to Al-Mahmoud's chart roles by `work/scripts/bench_almahmoud_replicate.py`,
  which then replayed the same documents through Al-Mahmoud's API and compared the
  trial-balance delta. The comparison matched exactly on 2026-10-26.
* The numbers below are that external ledger's role deltas, converted to minor
  units. Only amounts and account roles are vendored here: this test needs no
  Odoo instance, no network, and copies no Odoo code or demo records.

The same scenario is replayed on January 2026 dates so it fits the standard
open-period test fixture; the period label does not change any amount.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.conftest import SeededIdentity, SeededLedger
from tests.integration.test_cashbank import _seed_cash_account
from tests.integration.test_documents import MasterData, _entry_lines, _headers, seed_master

pytestmark = pytest.mark.integration

INVOICES_URL = "/api/v1/sales/invoices"
BILLS_URL = "/api/v1/purchasing/bills"
RECEIPTS_URL = "/api/v1/cashbank/receipts"
PAYMENTS_URL = "/api/v1/cashbank/payments"
TRIAL_BALANCE_URL = "/api/v1/reports/trial-balance"

# External oracle: net debit-positive delta per Al-Mahmoud role after the scenario.
EXPECTED_TRIAL_BALANCE_DELTA = {
    "1010": 90000,  # bank: receipt 1200.00 - payment 300.00
    "1100": 126000,  # receivable: 2000.00 + 460.00 - 1200.00
    "1200": 50000,  # inventory: bill 200.00 + 300.00
    "2000": -24500,  # payable: bill 545.00 - payment 300.00
    "2100": -1500,  # VAT: output 60.00 - input 45.00
    "4000": -240000,  # revenue: 1000.00 + 1000.00 + 400.00
}

# External oracle: the posted entry of each document, net debit-positive.
EXPECTED_ENTRIES = {
    "invoice1": {"4000": -200000, "1100": 200000},
    "invoice2": {"2100": -6000, "4000": -40000, "1100": 46000},
    "bill": {"1200": 50000, "2000": -54500, "2100": 4500},
    "receipt": {"1010": 120000, "1100": -120000},
    "payment": {"1010": -30000, "2000": 30000},
}


@pytest.fixture
async def ledger(engine: AsyncEngine, identity: SeededIdentity) -> SeededLedger:
    """The identity fixture plus a chart and January/February periods."""
    from tests.integration.conftest import seed_ledger

    return await seed_ledger(engine, identity)


@pytest.fixture
async def master(engine: AsyncEngine, ledger: SeededLedger) -> MasterData:
    """Customer, supplier and 15% tax code for the ledger's company."""
    return await seed_master(engine, ledger.identity)


@pytest.fixture
async def bank_account(engine: AsyncEngine, ledger: SeededLedger) -> UUID:
    """A USD bank account linked to the seeded chart."""
    return await _seed_cash_account(engine, ledger.identity, code="1010")


async def _trial_balance(client: AsyncClient, headers: dict[str, str]) -> dict[str, int]:
    response = await client.get(
        TRIAL_BALANCE_URL,
        params={"start": "2026-01-01", "end": "2026-01-31"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return {
        row["code"]: row["debit_minor"] - row["credit_minor"] for row in response.json()["rows"]
    }


async def test_external_oracle_scenario_posts_to_the_expected_roles(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
    bank_account: UUID,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    before = await _trial_balance(client, headers)

    invoice1 = await client.post(
        INVOICES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_date": date(2026, 1, 2).isoformat(),
            "currency": "USD",
            "exchange_rate": "1",
            "post": True,
            "lines": [
                {"description": "Consulting services", "quantity": "1", "unit_price_minor": 100000},
                {
                    "description": "Implementation services",
                    "quantity": "2",
                    "unit_price_minor": 50000,
                },
            ],
        },
        headers=headers,
    )
    assert invoice1.status_code == 201, invoice1.text
    invoice1_body = invoice1.json()

    invoice2 = await client.post(
        INVOICES_URL,
        json={
            "customer_id": str(master.customer_id),
            "invoice_date": date(2026, 1, 3).isoformat(),
            "currency": "USD",
            "exchange_rate": "1",
            "post": True,
            "lines": [
                {
                    "description": "Support services",
                    "quantity": "1",
                    "unit_price_minor": 40000,
                    "tax_code_id": str(master.tax_code_id),
                }
            ],
        },
        headers=headers,
    )
    assert invoice2.status_code == 201, invoice2.text
    invoice2_body = invoice2.json()

    bill = await client.post(
        BILLS_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "bill_date": date(2026, 1, 4).isoformat(),
            "currency": "USD",
            "post": True,
            "lines": [
                {"description": "Hosting expense", "quantity": "1", "unit_price_minor": 20000},
                {
                    "description": "Office supplies",
                    "quantity": "3",
                    "unit_price_minor": 10000,
                    "tax_code_id": str(master.tax_code_id),
                },
            ],
        },
        headers=headers,
    )
    assert bill.status_code == 201, bill.text
    bill_body = bill.json()

    receipt = await client.post(
        RECEIPTS_URL,
        json={
            "customer_id": str(master.customer_id),
            "cash_bank_account_id": str(bank_account),
            "receipt_date": date(2026, 1, 10).isoformat(),
            "amount_minor": 120000,
            "currency": "USD",
            "post": True,
            "allocations": [{"document_id": invoice1_body["id"], "amount_minor": 120000}],
        },
        headers=headers,
    )
    assert receipt.status_code == 201, receipt.text
    receipt_body = receipt.json()

    payment = await client.post(
        PAYMENTS_URL,
        json={
            "supplier_id": str(master.supplier_id),
            "cash_bank_account_id": str(bank_account),
            "payment_date": date(2026, 1, 12).isoformat(),
            "amount_minor": 30000,
            "currency": "USD",
            "post": True,
            "allocations": [{"document_id": bill_body["id"], "amount_minor": 30000}],
        },
        headers=headers,
    )
    assert payment.status_code == 201, payment.text
    payment_body = payment.json()

    created = {
        "invoice1": invoice1_body,
        "invoice2": invoice2_body,
        "bill": bill_body,
        "receipt": receipt_body,
        "payment": payment_body,
    }

    async with engine.connect() as connection:
        for name, expected in EXPECTED_ENTRIES.items():
            entry_id = created[name]["journal_entry_id"]
            assert entry_id is not None, name
            balances = await _entry_lines(
                connection, tenant_id=identity.tenant_id, entry_id=UUID(entry_id)
            )
            assert balances == expected, f"{name} entry {balances} != oracle {expected}"

    after = await _trial_balance(client, headers)
    delta = {
        code: after.get(code, 0) - before.get(code, 0)
        for code in set(after) | set(before)
        if after.get(code, 0) - before.get(code, 0) != 0
    }
    assert delta == EXPECTED_TRIAL_BALANCE_DELTA

    total_debits = sum(value for value in EXPECTED_TRIAL_BALANCE_DELTA.values() if value > 0)
    total_credits = -sum(value for value in EXPECTED_TRIAL_BALANCE_DELTA.values() if value < 0)
    assert total_debits == total_credits == 266000
