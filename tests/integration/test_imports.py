"""CSV master-data import tests (phase 1, slice 4).

The import validates every row and inserts all rows in one transaction; a bad
row or an existing code aborts the whole file.
"""

from __future__ import annotations

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

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
CUSTOMERS_URL = "/api/v1/imports/customers"
SUPPLIERS_URL = "/api/v1/imports/suppliers"
ITEMS_URL = "/api/v1/imports/items"
CUSTOMER_LIST_URL = "/api/v1/catalog/customers"
ITEM_LIST_URL = "/api/v1/catalog/items"


@pytest.fixture
async def company_admin(engine: AsyncEngine) -> SeededIdentity:
    """A committed company-admin identity for import endpoints."""
    seeded = await seed_identity(
        engine,
        tenant_slug=f"imports-{new_id().hex[-12:]}",
        email=f"imports-{new_id().hex[-12:]}@example.com",
        password="imports password 123",
        role_code="company_admin",
    )
    try:
        yield seeded
    finally:
        await delete_identity(engine, seeded.tenant_id)


@pytest.fixture
async def ledger(engine: AsyncEngine, company_admin: SeededIdentity) -> SeededLedger:
    """The company-admin identity plus a chart and periods."""
    return await seed_ledger(engine, company_admin)


async def _headers(client: AsyncClient, identity: SeededIdentity) -> dict[str, str]:
    response = await client.post(
        LOGIN_URL, json={"email": identity.email, "password": identity.password}
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Company-Id": str(identity.company_a),
    }


def _upload(content: str, name: str = "rows.csv") -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, content.encode("utf-8"), "text/csv")}


async def test_import_customers(client: AsyncClient, ledger: SeededLedger) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    suffix = new_id().hex[-8:]
    body = (
        "code,name_ar,name_en,tax_number\n"
        f"C-{suffix}-1,عميل أول,First,TAX-1\n"
        f"C-{suffix}-2,عميل ثان,,TAX-2\n"
    )
    response = await client.post(CUSTOMERS_URL, files=_upload(body), headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"entity": "customer", "created": 2}
    listed = await client.get(CUSTOMER_LIST_URL, headers=headers)
    assert listed.status_code == 200, listed.text
    codes = {row["code"] for row in listed.json()}
    assert {f"C-{suffix}-1", f"C-{suffix}-2"} <= codes


async def test_import_is_all_or_nothing_on_duplicate(
    client: AsyncClient, ledger: SeededLedger
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    suffix = new_id().hex[-8:]
    first = f"code,name_ar\nC-{suffix}-1,عميل\n"
    response = await client.post(CUSTOMERS_URL, files=_upload(first), headers=headers)
    assert response.status_code == 200, response.text
    second = f"code,name_ar\nC-{suffix}-2,جديد\nC-{suffix}-1,مكرر\n"
    duplicate = await client.post(CUSTOMERS_URL, files=_upload(second), headers=headers)
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["code"] == "import_duplicate_code"
    listed = await client.get(CUSTOMER_LIST_URL, headers=headers)
    codes = {row["code"] for row in listed.json()}
    assert f"C-{suffix}-2" not in codes


async def test_import_rejects_missing_required_field(
    client: AsyncClient, ledger: SeededLedger
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    body = "code,name_ar\nC-1,\n"
    response = await client.post(CUSTOMERS_URL, files=_upload(body), headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "import_invalid_row"
    assert "Row 2" in response.json()["detail"]


async def test_import_items_with_opening_quantity(
    client: AsyncClient, ledger: SeededLedger
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    suffix = new_id().hex[-8:]
    body = (
        "code,name_ar,unit_of_measure,quantity_on_hand,stock_value_minor\n"
        f"IT-{suffix}-1,صنف أول,box,5,500\n"
    )
    response = await client.post(ITEMS_URL, files=_upload(body), headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1
    listed = await client.get(ITEM_LIST_URL, headers=headers)
    assert listed.status_code == 200, listed.text
    item = next(row for row in listed.json() if row["code"] == f"IT-{suffix}-1")
    assert item["quantity_on_hand"] == "5.0000"


async def test_import_requires_permission(client: AsyncClient, engine: AsyncEngine) -> None:
    seeded = await seed_identity(
        engine,
        tenant_slug=f"imports-no-{new_id().hex[-12:]}",
        email=f"imports-no-{new_id().hex[-12:]}@example.com",
        password="imports password 123",
        role_code="auditor",
    )
    try:
        headers = await _headers(client, seeded)
        response = await client.post(
            CUSTOMERS_URL,
            files=_upload("code,name_ar\nC-1,عميل\n"),
            headers=headers,
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "permission_denied"
    finally:
        await delete_identity(engine, seeded.tenant_id)


OPENING_BALANCES_URL = "/api/v1/imports/opening-balances"
JOURNAL_LIST_URL = "/api/v1/ledger/journal-entries"


def _opening_form(*, period_id: str, dry_run: bool, post: bool = True) -> dict[str, str]:
    return {
        "period_id": period_id,
        "entry_date": "2026-01-01",
        "post": "true" if post else "false",
        "dry_run": "true" if dry_run else "false",
    }


async def test_opening_balances_dry_run_reports_without_writing(
    client: AsyncClient, ledger: SeededLedger
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    before = await client.get(JOURNAL_LIST_URL, headers=headers)
    assert before.status_code == 200, before.text

    csv_body = (
        "account_code,debit,credit,description\n"
        "1000,1000.00,,Cash opening\n"
        "3000,,1000.00,Capital opening\n"
    )
    response = await client.post(
        OPENING_BALANCES_URL,
        data=_opening_form(period_id=str(ledger.period_id), dry_run=True),
        files=_upload(csv_body),
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True
    assert body["total_debit_minor"] == 100000
    assert body["total_credit_minor"] == 100000
    assert body["line_count"] == 2
    assert body["entry_id"] is None
    assert body["lines"][0]["account_name_ar"] == "النقدية"

    after = await client.get(JOURNAL_LIST_URL, headers=headers)
    assert after.status_code == 200, after.text
    assert len(after.json()) == len(before.json())


async def test_opening_balances_commit_posts_one_balanced_entry(
    client: AsyncClient, ledger: SeededLedger
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    csv_body = "account_code,debit,credit,description\n1000,1500.00,,Cash\n3000,,1500.00,Capital\n"
    response = await client.post(
        OPENING_BALANCES_URL,
        data=_opening_form(period_id=str(ledger.period_id), dry_run=False),
        files=_upload(csv_body),
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entry_id"] is not None
    assert body["entry_number"].startswith("JE-2026-")

    detail = await client.get(f"{JOURNAL_LIST_URL}/{body['entry_id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    entry = detail.json()
    assert entry["status"] == "posted"
    assert entry["source_type"] == "opening_balance"
    assert entry["total_debit_minor"] == 150000
    assert entry["total_credit_minor"] == 150000
    assert len(entry["lines"]) == 2


async def test_opening_balances_reject_unbalanced_file(
    client: AsyncClient, ledger: SeededLedger
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    csv_body = "account_code,debit,credit\n1000,1500.00,\n3000,,1400.00\n"
    response = await client.post(
        OPENING_BALANCES_URL,
        data=_opening_form(period_id=str(ledger.period_id), dry_run=True),
        files=_upload(csv_body),
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "opening_balance_unbalanced"


async def test_opening_balances_reject_unknown_account(
    client: AsyncClient, ledger: SeededLedger
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    csv_body = "account_code,debit,credit\n9999,100.00,\n3000,,100.00\n"
    response = await client.post(
        OPENING_BALANCES_URL,
        data=_opening_form(period_id=str(ledger.period_id), dry_run=True),
        files=_upload(csv_body),
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "chart_incomplete"


async def test_opening_balances_reject_duplicate_account_row(
    client: AsyncClient, ledger: SeededLedger
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    csv_body = "account_code,debit,credit\n1000,100.00,\n1000,,100.00\n"
    response = await client.post(
        OPENING_BALANCES_URL,
        data=_opening_form(period_id=str(ledger.period_id), dry_run=True),
        files=_upload(csv_body),
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "import_invalid_row"
    assert "duplicate account code" in response.json()["detail"]


async def test_opening_balances_reject_closed_period(
    client: AsyncClient, ledger: SeededLedger
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    csv_body = "account_code,debit,credit\n1000,100.00,\n3000,,100.00\n"
    response = await client.post(
        OPENING_BALANCES_URL,
        data={
            "period_id": str(ledger.closed_period_id),
            "entry_date": "2026-02-01",
            "post": "true",
            "dry_run": "false",
        },
        files=_upload(csv_body),
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "period_closed"


async def test_opening_balances_require_permission(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    seeded = await seed_identity(
        engine,
        tenant_slug=f"opening-no-{new_id().hex[-12:]}",
        email=f"opening-no-{new_id().hex[-12:]}@example.com",
        password="opening password 123",
        role_code="auditor",
    )
    try:
        ledger = await seed_ledger(engine, seeded)
        headers = await _headers(client, seeded)
        csv_body = "account_code,debit,credit\n1000,100.00,\n3000,,100.00\n"
        response = await client.post(
            OPENING_BALANCES_URL,
            data=_opening_form(period_id=str(ledger.period_id), dry_run=True),
            files=_upload(csv_body),
            headers=headers,
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "permission_denied"
    finally:
        await delete_identity(engine, seeded.tenant_id)


def _xlsx(rows: list[list[object]]) -> dict[str, tuple[str, bytes, str]]:
    """Build a one-sheet workbook upload for the Excel import path."""
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return {"file": ("rows.xlsx", buffer.getvalue(), content_type)}


async def test_import_customers_from_excel(client: AsyncClient, ledger: SeededLedger) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    suffix = new_id().hex[-8:]
    files = _xlsx(
        [
            ["code", "name_ar", "name_en"],
            [f"X-{suffix}-1", "عميل إكسل", "Excel First"],
        ]
    )
    response = await client.post(CUSTOMERS_URL, files=files, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"entity": "customer", "created": 1}
    listed = await client.get(CUSTOMER_LIST_URL, headers=headers)
    codes = {row["code"] for row in listed.json()}
    assert f"X-{suffix}-1" in codes


async def test_opening_balances_from_excel_dry_run(
    client: AsyncClient, ledger: SeededLedger
) -> None:
    identity = ledger.identity
    headers = await _headers(client, identity)
    files = _xlsx(
        [
            ["account_code", "debit", "credit", "description"],
            ["1000", 1250.5, None, "Cash opening"],
            ["3000", None, 1250.5, "Capital opening"],
        ]
    )
    response = await client.post(
        OPENING_BALANCES_URL,
        data=_opening_form(period_id=str(ledger.period_id), dry_run=True),
        files=files,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["line_count"] == 2
    assert body["total_debit_minor"] == 125050
    assert body["total_credit_minor"] == 125050
    assert body["entry_id"] is None
