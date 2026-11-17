"""Posting account mapping: defaults, effective dating, permissions (phase 1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.ledger.mapping import DEFAULT_MAPPING
from app.modules.ledger.models import PostingMapping
from app.shared.ids import new_id
from tests.integration.conftest import (
    SeededLedger,
    delete_identity,
    seed_identity,
    seed_ledger,
)

pytestmark = pytest.mark.integration

MAPPINGS_URL = "/api/v1/ledger/posting-mappings"
LOGIN_URL = "/api/v1/auth/login"


@pytest.fixture
async def ledger(engine: AsyncEngine, identity: object) -> SeededLedger:
    """The accountant identity fixture plus a chart with default mappings."""
    return await seed_ledger(engine, identity)  # type: ignore[arg-type]


@pytest.fixture
async def admin_ledger(engine: AsyncEngine) -> AsyncIterator[SeededLedger]:
    """A committed company-admin identity with its own chart and mappings."""
    seeded = await seed_identity(
        engine,
        tenant_slug=f"map-{new_id().hex[-12:]}",
        email=f"map-{new_id().hex[-12:]}@example.com",
        password="mapping password 123",
        role_code="company_admin",
    )
    ledger = await seed_ledger(engine, seeded)
    try:
        yield ledger
    finally:
        await delete_identity(engine, seeded.tenant_id)


async def _login(client: AsyncClient, ledger: SeededLedger) -> dict[str, str]:
    response = await client.post(
        LOGIN_URL,
        json={"email": ledger.identity.email, "password": ledger.identity.password},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    assert isinstance(token, str)
    return {
        "Authorization": f"Bearer {token}",
        "X-Company-Id": str(ledger.identity.company_a),
    }


async def test_seeded_company_chart_has_default_mappings(
    client: AsyncClient,
    admin_ledger: SeededLedger,
) -> None:
    headers = await _login(client, admin_ledger)
    response = await client.get(MAPPINGS_URL, headers=headers, params={"on_date": "2026-01-15"})
    assert response.status_code == 200, response.text
    rows = {row["key"]: row for row in response.json()}
    assert set(rows) == {key for key, _code in DEFAULT_MAPPING}
    for key, code in DEFAULT_MAPPING:
        assert rows[key]["account_code"] == code, key
        assert rows[key]["account_id"] is not None
        assert rows[key]["valid_from"] == "2000-01-01"


async def test_mapping_change_is_effective_dated(
    client: AsyncClient,
    admin_ledger: SeededLedger,
    engine: AsyncEngine,
) -> None:
    headers = await _login(client, admin_ledger)
    accounts = await client.get("/api/v1/ledger/accounts", headers=headers)
    cash = next(row for row in accounts.json() if row["code"] == "1000")

    changed = await client.put(
        f"{MAPPINGS_URL}/accounts_receivable",
        headers=headers,
        json={"account_id": cash["id"], "valid_from": "2026-02-01"},
    )
    assert changed.status_code == 200, changed.text
    row = next(item for item in changed.json() if item["key"] == "accounts_receivable")
    assert row["account_code"] == "1000"
    assert row["valid_from"] == "2026-02-01"

    january = await client.get(MAPPINGS_URL, headers=headers, params={"on_date": "2026-01-15"})
    january_row = next(item for item in january.json() if item["key"] == "accounts_receivable")
    assert january_row["account_code"] == "1100"

    february = await client.get(MAPPINGS_URL, headers=headers, params={"on_date": "2026-02-15"})
    february_row = next(item for item in february.json() if item["key"] == "accounts_receivable")
    assert february_row["account_code"] == "1000"

    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(PostingMapping.valid_from, PostingMapping.valid_to).where(
                    PostingMapping.tenant_id == admin_ledger.identity.tenant_id,
                    PostingMapping.key == "accounts_receivable",
                )
            )
        ).all()
    closed = next(row for row in rows if row.valid_from == date(2000, 1, 1))
    assert closed.valid_to == date(2026, 1, 31)


async def test_mapping_rejects_wrong_account_type(
    client: AsyncClient,
    admin_ledger: SeededLedger,
) -> None:
    headers = await _login(client, admin_ledger)
    accounts = await client.get("/api/v1/ledger/accounts", headers=headers)
    cash = next(row for row in accounts.json() if row["code"] == "1000")
    response = await client.put(
        f"{MAPPINGS_URL}/sales_revenue",
        headers=headers,
        json={"account_id": cash["id"], "valid_from": "2026-03-01"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "posting_mapping_type_mismatch"

    unknown = await client.put(
        f"{MAPPINGS_URL}/not_a_role",
        headers=headers,
        json={"account_id": cash["id"]},
    )
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "posting_role_invalid"


async def test_mapping_change_requires_settings_permission(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    response = await client.post(
        LOGIN_URL,
        json={"email": ledger.identity.email, "password": ledger.identity.password},
    )
    token = response.json()["access_token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Company-Id": str(ledger.identity.company_a),
    }
    accounts = await client.get("/api/v1/ledger/accounts", headers=headers)
    cash = next(row for row in accounts.json() if row["code"] == "1000")
    denied = await client.put(
        f"{MAPPINGS_URL}/accounts_receivable",
        headers=headers,
        json={"account_id": cash["id"]},
    )
    assert denied.status_code == 403, denied.text
