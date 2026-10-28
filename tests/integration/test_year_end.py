"""Fiscal year-end close (phase 1, slice 3).

Closing a year zeroes every revenue and expense account into retained earnings,
closes the year's periods and marks the fiscal year closed. The tests check the
closing entry itself, the post-close trial balance, repeated close refusal, the
draft-entry block, the closed-last-period block and an empty year.
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

FISCAL_YEARS_URL = "/api/v1/ledger/fiscal-years"
PERIODS_URL = "/api/v1/ledger/periods"
TRIAL_BALANCE_URL = "/api/v1/reports/trial-balance"
JANUARY = date(2026, 1, 15)


@pytest.fixture
async def ledger(engine: AsyncEngine, identity) -> SeededLedger:
    """The identity fixture plus a chart and periods."""
    return await seed_ledger(engine, identity)


@pytest.fixture
async def master(engine: AsyncEngine, ledger: SeededLedger) -> MasterData:
    """Customer, supplier, tax code and item for the ledger's company."""
    return await seed_master(engine, ledger.identity)


async def _close_year(
    client: AsyncClient, headers: dict[str, str], fiscal_year_id: UUID
) -> dict[str, object]:
    response = await client.post(
        f"{FISCAL_YEARS_URL}/{fiscal_year_id}/close",
        json={"reason": "annual close"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _trial_balance(client: AsyncClient, headers: dict[str, str]) -> dict[str, int]:
    response = await client.get(
        TRIAL_BALANCE_URL,
        params={"start": "2026-01-01", "end": "2026-12-31"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return {row["code"]: row["balance_minor"] for row in response.json()["rows"]}


async def test_year_close_posts_closing_entry_and_zeroes_income_statement(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _create_bill(client, headers, master, quantity="10", unit_price_minor=100)
    await _create_invoice(client, headers, master, quantity="2", unit_price_minor=500)

    listed = await client.get(FISCAL_YEARS_URL, headers=headers)
    assert listed.status_code == 200, listed.text
    fiscal_years = listed.json()
    assert len(fiscal_years) == 1
    assert len(fiscal_years[0]["periods"]) == 3
    assert fiscal_years[0]["status"] == "open"

    body = await _close_year(client, headers, ledger.fiscal_year_id)
    assert body["fiscal_year"]["status"] == "closed"
    assert body["periods_closed"] == 2
    closing = body["closing_entry"]
    assert closing is not None
    assert closing["source_type"] == "fiscal_year_close"
    lines = {
        str(UUID(line["account_id"])): line["debit_minor"] - line["credit_minor"]
        for line in closing["lines"]
    }
    accounts = ledger.account_ids
    assert lines == {
        str(accounts["4000"]): 1000,
        str(accounts["5100"]): -200,
        str(accounts["3100"]): -800,
    }

    balances = await _trial_balance(client, headers)
    assert balances["4000"] == 0
    assert balances["5100"] == 0
    assert balances["3100"] == -800
    assert balances["1100"] == 1000
    assert balances["1200"] == 800
    assert balances["2000"] == -1000


async def test_year_close_refuses_second_close(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    await _close_year(client, headers, ledger.fiscal_year_id)
    response = await client.post(
        f"{FISCAL_YEARS_URL}/{ledger.fiscal_year_id}/close",
        json={"reason": "again"},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "fiscal_year_already_closed"


async def test_year_close_refuses_when_drafts_exist(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    draft = await client.post(
        "/api/v1/ledger/journal-entries",
        json={
            "period_id": str(ledger.period_id),
            "entry_date": JANUARY.isoformat(),
            "post": False,
            "lines": [
                {
                    "account_id": str(ledger.account_ids["1000"]),
                    "debit_minor": 100,
                    "credit_minor": 0,
                },
                {
                    "account_id": str(ledger.account_ids["3000"]),
                    "debit_minor": 0,
                    "credit_minor": 100,
                },
            ],
        },
        headers=headers,
    )
    assert draft.status_code == 201, draft.text
    response = await client.post(
        f"{FISCAL_YEARS_URL}/{ledger.fiscal_year_id}/close",
        json={"reason": "annual close"},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "fiscal_year_has_drafts"


async def test_year_close_refuses_when_last_period_is_closed(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    closed = await client.post(
        f"{PERIODS_URL}/{ledger.year_end_period_id}/close",
        json={"reason": "close December"},
        headers=headers,
    )
    assert closed.status_code == 200, closed.text
    response = await client.post(
        f"{FISCAL_YEARS_URL}/{ledger.fiscal_year_id}/close",
        json={"reason": "annual close"},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "fiscal_year_target_period_closed"


async def test_year_close_without_activity_still_closes_periods(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    body = await _close_year(client, headers, ledger.fiscal_year_id)
    assert body["closing_entry"] is None
    assert body["periods_closed"] == 2
    assert body["fiscal_year"]["status"] == "closed"
    balances = await _trial_balance(client, headers)
    assert balances["3100"] == 0
    assert all(value == 0 for value in balances.values())
