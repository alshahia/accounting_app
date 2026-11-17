"""Create the next fiscal year through the audited API (T-49).

The product ships with one fiscal year created at installation. Before
2027-01-01 an administrator must be able to create the next calendar year and
its twelve monthly periods from the running product, and the creation must be
in the audit trail. A duplicate year, an overlapping year and a year outside the
supported range are refused; a read-only role cannot create one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.modules.ledger.models import FiscalYear
from app.shared.ids import new_id
from tests.integration.conftest import (
    SeededIdentity,
    SeededLedger,
    delete_identity,
    seed_identity,
    seed_ledger,
)
from tests.integration.test_returns import _headers

pytestmark = pytest.mark.integration

FISCAL_YEARS_URL = "/api/v1/ledger/fiscal-years"


@pytest.fixture
async def ledger(engine: AsyncEngine, identity: SeededIdentity) -> SeededLedger:
    """The identity fixture plus a chart and periods."""
    return await seed_ledger(engine, identity)


@pytest.fixture
async def auditor(engine: AsyncEngine) -> AsyncIterator[SeededIdentity]:
    """A committed tenant whose user has only the auditor role."""
    slug = f"auditor-{new_id().hex[-12:]}"
    seeded = await seed_identity(
        engine,
        tenant_slug=slug,
        email=f"{slug}@example.com",
        password="correct horse battery staple",
        role_code="auditor",
    )
    try:
        yield seeded
    finally:
        await delete_identity(engine, seeded.tenant_id)


async def _audit_row(
    engine: AsyncEngine, tenant_id: UUID, entity_id: str
) -> dict[str, object] | None:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT action, detail FROM audit_log"
                " WHERE tenant_id = :tenant AND entity_id = :entity"
            ),
            {"tenant": str(tenant_id), "entity": str(entity_id)},
        )
        row = result.first()
    if row is None:
        return None
    return {"action": row[0], "detail": row[1]}


async def test_create_next_fiscal_year_creates_twelve_open_periods(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)

    response = await client.post(FISCAL_YEARS_URL, json={"year": 2027}, headers=headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["code"] == "FY2027"
    assert body["status"] == "open"
    assert body["start_date"] == "2027-01-01"
    assert body["end_date"] == "2027-12-31"
    periods = body["periods"]
    assert len(periods) == 12
    assert [period["number"] for period in periods] == list(range(1, 13))
    assert all(period["status"] == "open" for period in periods)
    assert periods[0]["start_date"] == "2027-01-01"
    assert periods[0]["end_date"] == "2027-01-31"
    assert periods[-1]["start_date"] == "2027-12-01"
    assert periods[-1]["end_date"] == "2027-12-31"

    listed = await client.get(FISCAL_YEARS_URL, headers=headers)
    assert listed.status_code == 200, listed.text
    assert [year["code"] for year in listed.json()] == ["FY2026", "FY2027"]

    audit = await _audit_row(engine, identity.tenant_id, body["id"])
    assert audit is not None
    assert audit["action"] == "ledger.fiscal_year.created"
    detail = audit["detail"]
    assert isinstance(detail, dict)
    assert detail["year"] == 2027
    assert detail["periods"] == 12


async def test_creating_the_same_year_twice_is_refused(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    first = await client.post(FISCAL_YEARS_URL, json={"year": 2027}, headers=headers)
    assert first.status_code == 201, first.text

    second = await client.post(FISCAL_YEARS_URL, json={"year": 2027}, headers=headers)
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "fiscal_year_exists"

    listed = await client.get(FISCAL_YEARS_URL, headers=headers)
    assert len(listed.json()) == 2
    assert sum(1 for year in listed.json() if year["code"] == "FY2027") == 1


async def test_overlapping_year_is_refused(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
) -> None:
    identity = ledger.identity
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        session.add(
            FiscalYear(
                tenant_id=identity.tenant_id,
                company_id=identity.company_a,
                code="FY2026H2",
                name_ar="سنة متداخلة",
                start_date=date(2026, 7, 1),
                end_date=date(2027, 6, 30),
            )
        )
        await session.flush()

    headers = await _headers(client, identity, identity.company_a)
    response = await client.post(FISCAL_YEARS_URL, json={"year": 2027}, headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "fiscal_year_overlap"


@pytest.mark.parametrize("year", [1999, 2101])
async def test_year_out_of_range_is_refused(
    client: AsyncClient,
    ledger: SeededLedger,
    year: int,
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    response = await client.post(FISCAL_YEARS_URL, json={"year": year}, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "fiscal_year_out_of_range"


async def test_viewer_cannot_create_a_fiscal_year(
    client: AsyncClient,
    auditor: SeededIdentity,
) -> None:
    headers = await _headers(client, auditor, auditor.company_a)
    response = await client.post(FISCAL_YEARS_URL, json={"year": 2027}, headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "permission_denied"
