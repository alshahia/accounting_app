"""Warehouse, per-warehouse balance and transfer tests against PostgreSQL.

The stock model keeps the item row as the company total and adds a balance per
warehouse; a transfer moves quantity and value between warehouses without
changing the company total or the GL. These tests drive the service transaction
path and the HTTP surface, including permission denial for a sales user.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.modules.catalog.models import Item
from app.modules.inventory import service
from app.modules.inventory.models import StockBalance, Warehouse
from app.modules.inventory.service import TransferInput
from app.modules.ledger.models import Account, JournalLine
from app.shared.errors import AppError
from app.shared.ids import new_id
from app.shared.unit_of_work import UnitOfWork
from tests.integration.conftest import (
    SeededIdentity,
    SeededLedger,
    delete_identity,
    seed_identity,
    seed_ledger,
)

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
WAREHOUSES_URL = "/api/v1/inventory/warehouses"
BALANCES_URL = "/api/v1/inventory/balances"
TRANSFERS_URL = "/api/v1/inventory/transfers"
ITEMS_URL = "/api/v1/catalog/items"
COUNTS_URL = "/api/v1/inventory/counts"
RECEIPT_DATE = date(2026, 1, 15)


def _unit_of_work(engine: AsyncEngine) -> UnitOfWork:
    return UnitOfWork(async_sessionmaker(engine, expire_on_commit=False))


@pytest.fixture
async def ledger(engine: AsyncEngine, identity: SeededIdentity) -> SeededLedger:
    """The identity fixture plus a chart, a January period and a default warehouse."""
    return await seed_ledger(engine, identity)


@pytest.fixture
async def admin_ledger(engine: AsyncEngine) -> AsyncIterator[SeededLedger]:
    """A committed company-admin identity with its own chart and warehouse."""
    seeded = await seed_identity(
        engine,
        tenant_slug=f"admin-{new_id().hex[-12:]}",
        email=f"admin-{new_id().hex[-12:]}@example.com",
        password="admin password 123",
        role_code="company_admin",
    )
    ledger = await seed_ledger(engine, seeded)
    try:
        yield ledger
    finally:
        await delete_identity(engine, seeded.tenant_id)


@pytest.fixture
async def sales_identity(engine: AsyncEngine) -> AsyncIterator[SeededIdentity]:
    """A committed sales-role user for permission-denial tests."""
    seeded = await seed_identity(
        engine,
        tenant_slug=f"sales-{new_id().hex[-12:]}",
        email=f"sales-{new_id().hex[-12:]}@example.com",
        password="sales password 123",
        role_code="sales",
    )
    try:
        yield seeded
    finally:
        await delete_identity(engine, seeded.tenant_id)


async def _headers(
    client: AsyncClient,
    identity: SeededIdentity,
    company_id: UUID,
) -> dict[str, str]:
    response = await client.post(
        LOGIN_URL,
        json={"email": identity.email, "password": identity.password},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}", "X-Company-Id": str(company_id)}


async def _create_item(engine: AsyncEngine, ledger: SeededLedger, *, code: str = "SKU-1") -> UUID:
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        item = Item(
            tenant_id=ledger.identity.tenant_id,
            company_id=ledger.identity.company_a,
            code=code,
            name_ar="صنف تجريبي",
            name_en="Test item",
            unit_of_measure="piece",
        )
        session.add(item)
        await session.flush()
        return item.id


async def _warehouse_id(
    engine: AsyncEngine, ledger: SeededLedger, *, code: str, default: bool = False
) -> UUID:
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        warehouse = Warehouse(
            tenant_id=ledger.identity.tenant_id,
            company_id=ledger.identity.company_a,
            code=code,
            name_ar=f"مستودع {code}",
            name_en=code,
            is_default=default,
            is_active=True,
        )
        session.add(warehouse)
        await session.flush()
        return warehouse.id


async def _balance(
    engine: AsyncEngine, ledger: SeededLedger, *, warehouse_id: UUID, item_id: UUID
) -> tuple[Decimal, int]:
    async with engine.connect() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        row = (
            await session.execute(
                select(StockBalance).where(
                    StockBalance.tenant_id == ledger.identity.tenant_id,
                    StockBalance.company_id == ledger.identity.company_a,
                    StockBalance.warehouse_id == warehouse_id,
                    StockBalance.item_id == item_id,
                )
            )
        ).scalar_one()
        return row.quantity, row.value_minor


async def _item_totals(engine: AsyncEngine, item_id: UUID) -> tuple[Decimal, int]:
    async with engine.connect() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        item = await session.get(Item, item_id)
        assert item is not None
        return item.quantity_on_hand, item.stock_value_minor


async def test_receive_issue_and_transfer_keep_balances_consistent(
    engine: AsyncEngine, ledger: SeededLedger
) -> None:
    tenant_id = ledger.identity.tenant_id
    company_id = ledger.identity.company_a
    item_id = await _create_item(engine, ledger)
    second_id = await _warehouse_id(engine, ledger, code="SECOND")
    async with engine.connect() as connection:
        default_warehouse = (
            await connection.execute(
                select(Warehouse.id).where(
                    Warehouse.tenant_id == tenant_id,
                    Warehouse.company_id == company_id,
                    Warehouse.is_default.is_(True),
                )
            )
        ).scalar_one()

    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        await service.receive_stock(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            item_id=item_id,
            quantity=Decimal("10"),
            value_minor=10000,
            document_type="test_receipt",
            document_id=new_id(),
            occurred_on=RECEIPT_DATE,
            actor_user_id=ledger.identity.user_id,
        )
    assert await _balance(engine, ledger, warehouse_id=default_warehouse, item_id=item_id) == (
        Decimal("10.0000"),
        10000,
    )

    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        uow = _unit_of_work(engine)
        result = await service.create_stock_transfer(
            uow,
            tenant_id=tenant_id,
            actor_user_id=ledger.identity.user_id,
            request=TransferInput(
                company_id=company_id,
                from_warehouse_id=default_warehouse,
                to_warehouse_id=second_id,
                item_id=item_id,
                quantity=Decimal("4"),
                transfer_date=RECEIPT_DATE,
            ),
        )
    assert result.value_minor == 4000
    assert await _balance(engine, ledger, warehouse_id=default_warehouse, item_id=item_id) == (
        Decimal("6.0000"),
        6000,
    )
    assert await _balance(engine, ledger, warehouse_id=second_id, item_id=item_id) == (
        Decimal("4.0000"),
        4000,
    )
    assert await _item_totals(engine, item_id) == (Decimal("10.0000"), 10000)

    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        await service.issue_stock(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            item_id=item_id,
            quantity=Decimal("4"),
            document_type="test_issue",
            document_id=new_id(),
            occurred_on=RECEIPT_DATE,
            actor_user_id=ledger.identity.user_id,
            warehouse_id=second_id,
        )
    assert await _balance(engine, ledger, warehouse_id=second_id, item_id=item_id) == (
        Decimal("0.0000"),
        0,
    )
    assert await _item_totals(engine, item_id) == (Decimal("6.0000"), 6000)


async def test_transfer_rejects_same_warehouse_and_insufficient_stock(
    engine: AsyncEngine, ledger: SeededLedger
) -> None:
    tenant_id = ledger.identity.tenant_id
    company_id = ledger.identity.company_a
    item_id = await _create_item(engine, ledger, code="SKU-2")
    second_id = await _warehouse_id(engine, ledger, code="SECOND-2")
    async with engine.connect() as connection:
        default_warehouse = (
            await connection.execute(
                select(Warehouse.id).where(
                    Warehouse.tenant_id == tenant_id,
                    Warehouse.company_id == company_id,
                    Warehouse.is_default.is_(True),
                )
            )
        ).scalar_one()

    uow = _unit_of_work(engine)
    with pytest.raises(AppError) as same:
        await service.create_stock_transfer(
            uow,
            tenant_id=tenant_id,
            actor_user_id=ledger.identity.user_id,
            request=TransferInput(
                company_id=company_id,
                from_warehouse_id=default_warehouse,
                to_warehouse_id=default_warehouse,
                item_id=item_id,
                quantity=Decimal("1"),
                transfer_date=RECEIPT_DATE,
            ),
        )
    assert same.value.code == "transfer_same_warehouse"

    uow = _unit_of_work(engine)
    with pytest.raises(AppError) as short:
        await service.create_stock_transfer(
            uow,
            tenant_id=tenant_id,
            actor_user_id=ledger.identity.user_id,
            request=TransferInput(
                company_id=company_id,
                from_warehouse_id=default_warehouse,
                to_warehouse_id=second_id,
                item_id=item_id,
                quantity=Decimal("1"),
                transfer_date=RECEIPT_DATE,
            ),
        )
    assert short.value.code == "insufficient_stock"


async def test_warehouse_crud_and_default_switch(
    client: AsyncClient, admin_ledger: SeededLedger
) -> None:
    identity = admin_ledger.identity
    headers = await _headers(client, identity, identity.company_a)

    listed = await client.get(WAREHOUSES_URL, headers=headers)
    assert listed.status_code == 200, listed.text
    warehouses = listed.json()
    assert len(warehouses) == 1
    default_id = warehouses[0]["id"]
    assert warehouses[0]["code"] == "MAIN"
    assert warehouses[0]["is_default"] is True

    created = await client.post(
        WAREHOUSES_URL,
        json={"code": "BR2", "name_ar": "فرع ثان", "name_en": "Branch two"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    branch = created.json()
    assert branch["code"] == "BR2"
    assert branch["is_default"] is False

    made_default = await client.put(
        f"{WAREHOUSES_URL}/{branch['id']}",
        json={
            "name_ar": "فرع ثان",
            "name_en": "Branch two",
            "is_active": True,
            "is_default": True,
        },
        headers=headers,
    )
    assert made_default.status_code == 200, made_default.text
    assert made_default.json()["is_default"] is True

    refreshed = await client.get(WAREHOUSES_URL, headers=headers)
    defaults = [row for row in refreshed.json() if row["is_default"]]
    assert [row["id"] for row in defaults] == [branch["id"]]
    assert all(row["id"] != default_id for row in defaults)


async def test_balances_endpoint_and_permission_denial(
    client: AsyncClient,
    engine: AsyncEngine,
    admin_ledger: SeededLedger,
    sales_identity: SeededIdentity,
) -> None:
    identity = admin_ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    item_id = await _create_item(engine, admin_ledger, code="SKU-3")
    async with engine.connect() as connection:
        default_warehouse = (
            await connection.execute(
                select(Warehouse.id).where(
                    Warehouse.tenant_id == identity.tenant_id,
                    Warehouse.company_id == identity.company_a,
                    Warehouse.is_default.is_(True),
                )
            )
        ).scalar_one()
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        await service.receive_stock(
            session,
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            item_id=item_id,
            quantity=Decimal("3"),
            value_minor=4500,
            document_type="test_receipt",
            document_id=new_id(),
            occurred_on=RECEIPT_DATE,
            actor_user_id=identity.user_id,
        )

    balances = await client.get(BALANCES_URL, headers=headers)
    assert balances.status_code == 200, balances.text
    rows = [row for row in balances.json() if row["item_id"] == str(item_id)]
    assert len(rows) == 1
    assert rows[0]["warehouse_id"] == str(default_warehouse)
    assert rows[0]["quantity"] == "3.0000"
    assert rows[0]["value_minor"] == 4500

    sales_headers = await _headers(client, sales_identity, sales_identity.company_a)
    denied = await client.post(
        WAREHOUSES_URL,
        json={"code": "NOPE", "name_ar": "مرفوض"},
        headers=sales_headers,
    )
    assert denied.status_code == 403, denied.text


async def test_transfer_api_posts_and_rejects_insufficient(
    client: AsyncClient, engine: AsyncEngine, admin_ledger: SeededLedger
) -> None:
    identity = admin_ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    item_id = await _create_item(engine, admin_ledger, code="SKU-4")
    async with engine.connect() as connection:
        default_warehouse = (
            await connection.execute(
                select(Warehouse.id).where(
                    Warehouse.tenant_id == identity.tenant_id,
                    Warehouse.company_id == identity.company_a,
                    Warehouse.is_default.is_(True),
                )
            )
        ).scalar_one()
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        await service.receive_stock(
            session,
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            item_id=item_id,
            quantity=Decimal("5"),
            value_minor=1000,
            document_type="test_receipt",
            document_id=new_id(),
            occurred_on=RECEIPT_DATE,
            actor_user_id=identity.user_id,
        )
    created = await client.post(
        WAREHOUSES_URL,
        json={"code": "BR3", "name_ar": "فرع ثالث"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    other = created.json()["id"]

    transfer = await client.post(
        TRANSFERS_URL,
        json={
            "transfer_date": RECEIPT_DATE.isoformat(),
            "from_warehouse_id": str(default_warehouse),
            "to_warehouse_id": other,
            "item_id": str(item_id),
            "quantity": "2",
        },
        headers=headers,
    )
    assert transfer.status_code == 201, transfer.text
    body = transfer.json()
    assert body["number"].startswith("ST-2026-")
    assert body["value_minor"] == 400
    assert body["from_warehouse_id"] == str(default_warehouse)
    assert body["to_warehouse_id"] == other

    too_much = await client.post(
        TRANSFERS_URL,
        json={
            "transfer_date": RECEIPT_DATE.isoformat(),
            "from_warehouse_id": str(default_warehouse),
            "to_warehouse_id": other,
            "item_id": str(item_id),
            "quantity": "9",
        },
        headers=headers,
    )
    assert too_much.status_code == 409, too_much.text
    assert too_much.json()["code"] == "insufficient_stock"

    same = await client.post(
        TRANSFERS_URL,
        json={
            "transfer_date": RECEIPT_DATE.isoformat(),
            "from_warehouse_id": other,
            "to_warehouse_id": other,
            "item_id": str(item_id),
            "quantity": "1",
        },
        headers=headers,
    )
    assert same.status_code == 422, same.text


async def _receive(
    engine: AsyncEngine,
    ledger: SeededLedger,
    *,
    item_id: UUID,
    quantity: str,
    value_minor: int,
) -> None:
    async with engine.begin() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        await service.receive_stock(
            session,
            tenant_id=ledger.identity.tenant_id,
            company_id=ledger.identity.company_a,
            item_id=item_id,
            quantity=Decimal(quantity),
            value_minor=value_minor,
            document_type="test_receipt",
            document_id=new_id(),
            occurred_on=RECEIPT_DATE,
            actor_user_id=ledger.identity.user_id,
        )


async def _count_payload(
    engine: AsyncEngine,
    ledger: SeededLedger,
    *,
    item_id: UUID,
    counted: str,
) -> dict[str, object]:
    async with engine.connect() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        warehouse_id = (
            await session.execute(
                select(Warehouse.id).where(
                    Warehouse.tenant_id == ledger.identity.tenant_id,
                    Warehouse.company_id == ledger.identity.company_a,
                    Warehouse.is_default.is_(True),
                )
            )
        ).scalar_one()
    return {
        "count_date": RECEIPT_DATE.isoformat(),
        "warehouse_id": str(warehouse_id),
        "memo": "count test",
        "lines": [{"item_id": str(item_id), "counted_quantity": counted}],
    }


async def _journal_lines(engine: AsyncEngine, entry_id: UUID) -> list[tuple[str, int, int]]:
    async with engine.connect() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        rows = (
            await session.execute(
                select(JournalLine, Account.code)
                .join(Account, Account.id == JournalLine.account_id)
                .where(JournalLine.entry_id == entry_id)
            )
        ).all()
        return [(code, line.debit_minor, line.credit_minor) for line, code in rows]


async def test_stock_count_shortage_posts_adjustment_and_is_final(
    client: AsyncClient, engine: AsyncEngine, admin_ledger: SeededLedger
) -> None:
    identity = admin_ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    item_id = await _create_item(engine, admin_ledger, code="CNT-1")
    await _receive(engine, admin_ledger, item_id=item_id, quantity="10", value_minor=10000)

    payload = await _count_payload(engine, admin_ledger, item_id=item_id, counted="7")
    created = await client.post(COUNTS_URL, json=payload, headers=headers)
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["status"] == "draft"
    assert draft["number"].startswith("SC-2026-")
    assert draft["lines"][0]["system_quantity"] == "10.0000"
    assert draft["lines"][0]["variance_quantity"] == "-3.0000"
    assert draft["journal_entry_id"] is None

    posted = await client.post(f"{COUNTS_URL}/{draft['id']}/post", headers=headers)
    assert posted.status_code == 200, posted.text
    body = posted.json()
    assert body["status"] == "posted"
    assert body["posted_at"] is not None
    assert body["lines"][0]["variance_value_minor"] == -3000
    assert body["journal_entry_id"] is not None

    lines = await _journal_lines(engine, UUID(body["journal_entry_id"]))
    assert ("1200", 0, 3000) in lines
    assert ("5200", 3000, 0) in lines

    rebalanced = await _balance(
        engine,
        admin_ledger,
        warehouse_id=UUID(payload["warehouse_id"]),
        item_id=item_id,
    )
    assert rebalanced == (Decimal("7.0000"), 7000)
    assert await _item_totals(engine, item_id) == (Decimal("7.0000"), 7000)

    again = await client.post(f"{COUNTS_URL}/{draft['id']}/post", headers=headers)
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "count_already_posted"
    edit = await client.put(
        f"{COUNTS_URL}/{draft['id']}",
        json=payload,
        headers=headers,
    )
    assert edit.status_code == 409, edit.text
    removed = await client.delete(f"{COUNTS_URL}/{draft['id']}", headers=headers)
    assert removed.status_code == 409, removed.text


async def test_stock_count_surplus_and_zero_variance(
    client: AsyncClient, engine: AsyncEngine, admin_ledger: SeededLedger
) -> None:
    identity = admin_ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    item_id = await _create_item(engine, admin_ledger, code="CNT-2")
    await _receive(engine, admin_ledger, item_id=item_id, quantity="5", value_minor=5000)

    surplus_payload = await _count_payload(engine, admin_ledger, item_id=item_id, counted="8")
    created = await client.post(COUNTS_URL, json=surplus_payload, headers=headers)
    assert created.status_code == 201, created.text
    posted = await client.post(f"{COUNTS_URL}/{created.json()['id']}/post", headers=headers)
    assert posted.status_code == 200, posted.text
    body = posted.json()
    assert body["lines"][0]["variance_value_minor"] == 3000
    assert body["journal_entry_id"] is not None
    lines = await _journal_lines(engine, UUID(body["journal_entry_id"]))
    assert ("1200", 3000, 0) in lines
    assert ("5200", 0, 3000) in lines
    assert await _item_totals(engine, item_id) == (Decimal("8.0000"), 8000)

    exact_payload = await _count_payload(engine, admin_ledger, item_id=item_id, counted="8")
    exact = await client.post(COUNTS_URL, json=exact_payload, headers=headers)
    assert exact.status_code == 201, exact.text
    exact_posted = await client.post(f"{COUNTS_URL}/{exact.json()['id']}/post", headers=headers)
    assert exact_posted.status_code == 200, exact_posted.text
    exact_body = exact_posted.json()
    assert exact_body["lines"][0]["variance_quantity"] == "0.0000"
    assert exact_body["journal_entry_id"] is None
    assert await _item_totals(engine, item_id) == (Decimal("8.0000"), 8000)


async def test_stock_count_rejects_closed_period_and_unknown_item(
    client: AsyncClient, engine: AsyncEngine, admin_ledger: SeededLedger
) -> None:
    identity = admin_ledger.identity
    headers = await _headers(client, identity, identity.company_a)
    item_id = await _create_item(engine, admin_ledger, code="CNT-3")
    await _receive(engine, admin_ledger, item_id=item_id, quantity="2", value_minor=2000)
    payload = await _count_payload(engine, admin_ledger, item_id=item_id, counted="2")
    payload["count_date"] = "2026-02-10"
    created = await client.post(COUNTS_URL, json=payload, headers=headers)
    assert created.status_code == 201, created.text
    rejected = await client.post(f"{COUNTS_URL}/{created.json()['id']}/post", headers=headers)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "period_closed"

    unknown = await client.post(
        COUNTS_URL,
        json={
            "count_date": RECEIPT_DATE.isoformat(),
            "warehouse_id": payload["warehouse_id"],
            "lines": [{"item_id": str(new_id()), "counted_quantity": "1"}],
        },
        headers=headers,
    )
    assert unknown.status_code == 404, unknown.text


async def test_stock_count_requires_manage_permission(
    client: AsyncClient,
    engine: AsyncEngine,
    admin_ledger: SeededLedger,
    sales_identity: SeededIdentity,
) -> None:
    item_id = await _create_item(engine, admin_ledger, code="CNT-4")
    payload = await _count_payload(engine, admin_ledger, item_id=item_id, counted="0")
    sales_headers = await _headers(client, sales_identity, sales_identity.company_a)
    denied = await client.post(COUNTS_URL, json=payload, headers=sales_headers)
    assert denied.status_code == 403, denied.text
