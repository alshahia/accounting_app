"""Effective-dated tax rates: configuration changes do not rewrite old documents."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.shared.ids import new_id
from tests.integration.conftest import (
    SeededIdentity,
    SeededLedger,
    delete_identity,
    seed_identity,
    seed_ledger,
)
from tests.integration.test_documents import MasterData, seed_master

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
INVOICES_URL = "/api/v1/sales/invoices"
TAX_CODES_URL = "/api/v1/catalog/tax-codes"
JANUARY = date(2026, 1, 15)
JANUARY_LATE = date(2026, 1, 25)
JANUARY_EARLY = date(2026, 1, 5)


@pytest.fixture
async def identity(engine: AsyncEngine) -> SeededIdentity:
    seeded = await seed_identity(
        engine,
        tenant_slug=f"tax-rates-{new_id().hex[-12:]}",
        email=f"tax-rates-{new_id().hex[-12:]}@example.com",
        password="tax rates password 123",
        role_code="tenant_admin",
    )
    try:
        yield seeded
    finally:
        await delete_identity(engine, seeded.tenant_id)


@pytest.fixture
async def ledger(engine: AsyncEngine, identity: SeededIdentity) -> SeededLedger:
    return await seed_ledger(engine, identity)


@pytest.fixture
async def master(engine: AsyncEngine, ledger: SeededLedger) -> MasterData:
    return await seed_master(engine, ledger.identity)


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


def _invoice_payload(master: MasterData, tax_code_id: UUID, on_date: date) -> dict[str, object]:
    return {
        "customer_id": str(master.customer_id),
        "invoice_date": on_date.isoformat(),
        "currency": "USD",
        "post": True,
        "lines": [
            {
                "description": "rate test",
                "quantity": "1",
                "unit_price_minor": 10000,
                "tax_code_id": str(tax_code_id),
            }
        ],
    }


async def test_rate_change_is_effective_dated_and_history_is_readable(
    client: AsyncClient,
    identity: SeededIdentity,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    headers = await _headers(client, identity, identity.company_a)
    created = await client.post(
        TAX_CODES_URL,
        json={
            "code": f"VAT-{new_id().hex[-6:]}",
            "name_ar": "ضريبة اختبارية",
            "rate_basis_points": 1500,
            "kind": "sales",
            "jurisdiction": "IQ",
            "valid_from": "2026-01-01",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    tax_code_id = created.json()["id"]

    changed = await client.put(
        f"{TAX_CODES_URL}/{tax_code_id}/rate",
        json={"rate_basis_points": 500, "valid_from": "2026-01-20"},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["rate_basis_points"] == 500

    history = await client.get(f"{TAX_CODES_URL}/{tax_code_id}/rates", headers=headers)
    assert history.status_code == 200, history.text
    rows = history.json()
    assert [(row["rate_basis_points"], row["valid_from"], row["valid_to"]) for row in rows] == [
        (500, "2026-01-20", None),
        (1500, "2026-01-01", "2026-01-19"),
    ]

    january = await client.post(
        INVOICES_URL,
        json=_invoice_payload(master, tax_code_id, JANUARY),
        headers=headers,
    )
    assert january.status_code == 201, january.text
    assert january.json()["tax_minor"] == 1500

    late_january = await client.post(
        INVOICES_URL,
        json=_invoice_payload(master, tax_code_id, JANUARY_LATE),
        headers=headers,
    )
    assert late_january.status_code == 201, late_january.text
    assert late_january.json()["tax_minor"] == 500


async def test_invalid_rate_dates_are_refused(
    client: AsyncClient,
    identity: SeededIdentity,
    ledger: SeededLedger,
    master: MasterData,
) -> None:
    headers = await _headers(client, identity, identity.company_a)
    created = await client.post(
        TAX_CODES_URL,
        json={
            "code": f"VAT-{new_id().hex[-6:]}",
            "name_ar": "ضريبة اختبارية",
            "rate_basis_points": 1500,
            "kind": "sales",
            "valid_from": "2026-01-10",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    tax_code_id = created.json()["id"]

    duplicate = await client.put(
        f"{TAX_CODES_URL}/{tax_code_id}/rate",
        json={"rate_basis_points": 500, "valid_from": "2026-01-10"},
        headers=headers,
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["code"] == "tax_rate_duplicate"

    missing = await client.post(
        INVOICES_URL,
        json=_invoice_payload(master, tax_code_id, JANUARY_EARLY),
        headers=headers,
    )
    assert missing.status_code == 409, missing.text
    assert missing.json()["code"] == "tax_rate_missing"
