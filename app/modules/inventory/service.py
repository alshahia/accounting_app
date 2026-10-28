"""Weighted-average stock receipts and issues (phase 0, day 7).

All functions run inside the caller's transaction and lock the item row, so two
documents cannot both consume the same stock. A receipt's value is the exact
base-currency amount its document posts to inventory, which keeps the inventory
GL balance equal to the sum of item values. An issue's cost is the proportional
share of the item's current value, and issuing the whole quantity leaves zero
value, so a fully sold item carries no residual.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import Item
from app.modules.identity.audit import record_audit
from app.modules.inventory.models import (
    StockBalance,
    StockCount,
    StockCountLine,
    StockMove,
    StockTransfer,
    Warehouse,
)
from app.modules.ledger.mapping import resolve_posting_accounts
from app.modules.ledger.models import AccountingPeriod
from app.modules.ledger.service import JournalLineInput, post_entry_in_session
from app.shared.errors import AppError
from app.shared.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class StockIssue:
    """The issue move and the exact base-currency cost it removed from stock."""

    move: StockMove
    value_minor: int


@dataclass(frozen=True)
class TransferInput:
    """A request to move stock from one warehouse to another."""

    company_id: UUID
    from_warehouse_id: UUID
    to_warehouse_id: UUID
    item_id: UUID
    quantity: Decimal
    transfer_date: date
    memo: str | None = None


@dataclass(frozen=True)
class TransferResult:
    """A posted transfer and the value it moved between warehouses."""

    transfer: StockTransfer
    value_minor: int


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CountLineInput:
    """One counted item inside a stock-count request."""

    item_id: UUID
    counted_quantity: Decimal


@dataclass(frozen=True)
class CountInput:
    """A stock-count request for one warehouse."""

    company_id: UUID
    warehouse_id: UUID
    count_date: date
    lines: tuple[CountLineInput, ...]
    memo: str | None = None


@dataclass(frozen=True)
class CountResult:
    """A stock count, its lines and the journal entry posting its variance (when any)."""

    count: StockCount
    lines: tuple[StockCountLine, ...]
    journal_entry_id: UUID | None


def _conflict(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Conflict", status_code=409, detail=detail)


def _unprocessable(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Unprocessable entity", status_code=422, detail=detail)


def _round_minor(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def _lock_item(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    item_id: UUID,
) -> Item:
    item = (
        await session.execute(
            select(Item)
            .where(
                Item.tenant_id == tenant_id,
                Item.company_id == company_id,
                Item.id == item_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise AppError(
            code="item_not_found",
            title="Not found",
            status_code=404,
            detail="The item does not exist in the selected company.",
        )
    if not item.is_active:
        raise AppError(
            code="item_inactive",
            title="Unprocessable entity",
            status_code=422,
            detail=f"Item {item.code} is not active.",
        )
    return item


async def ensure_default_warehouse(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> UUID:
    """Return the company's default warehouse, creating MAIN when none exists."""
    warehouse_id = await session.scalar(
        select(Warehouse.id).where(
            Warehouse.tenant_id == tenant_id,
            Warehouse.company_id == company_id,
            Warehouse.is_default.is_(True),
            Warehouse.is_active.is_(True),
        )
    )
    if warehouse_id is not None:
        return warehouse_id
    existing = await session.scalar(
        select(Warehouse.id)
        .where(
            Warehouse.tenant_id == tenant_id,
            Warehouse.company_id == company_id,
            Warehouse.is_active.is_(True),
        )
        .order_by(Warehouse.created_at)
    )
    if existing is not None:
        warehouse = await session.get(Warehouse, existing)
        if warehouse is not None:
            warehouse.is_default = True
        return existing
    warehouse = Warehouse(
        tenant_id=tenant_id,
        company_id=company_id,
        code="MAIN",
        name_ar="المستودع الرئيسي",
        name_en="Main warehouse",
        is_default=True,
        is_active=True,
    )
    session.add(warehouse)
    await session.flush()
    return warehouse.id


async def _lock_warehouse(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    warehouse_id: UUID,
    active_required: bool = True,
) -> Warehouse:
    warehouse = (
        await session.execute(
            select(Warehouse)
            .where(
                Warehouse.tenant_id == tenant_id,
                Warehouse.company_id == company_id,
                Warehouse.id == warehouse_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if warehouse is None:
        raise AppError(
            code="warehouse_not_found",
            title="Not found",
            status_code=404,
            detail="The warehouse does not exist in the selected company.",
        )
    if active_required and not warehouse.is_active:
        raise AppError(
            code="warehouse_inactive",
            title="Unprocessable entity",
            status_code=422,
            detail=f"Warehouse {warehouse.code} is not active.",
        )
    return warehouse


async def _lock_balance(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    warehouse_id: UUID,
    item_id: UUID,
) -> StockBalance:
    balance = (
        await session.execute(
            select(StockBalance)
            .where(
                StockBalance.tenant_id == tenant_id,
                StockBalance.company_id == company_id,
                StockBalance.warehouse_id == warehouse_id,
                StockBalance.item_id == item_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if balance is None:
        balance = StockBalance(
            tenant_id=tenant_id,
            company_id=company_id,
            warehouse_id=warehouse_id,
            item_id=item_id,
            quantity=Decimal("0"),
            value_minor=0,
        )
        session.add(balance)
        await session.flush()
    return balance


async def _allocate_transfer_number(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    transfer_date: date,
) -> str:
    period_key = f"{transfer_date.year:04d}"
    next_value = await session.scalar(
        text(
            """
            INSERT INTO number_sequence (tenant_id, company_id, scope, period_key, next_value)
            VALUES (:tenant_id, :company_id, 'stock_transfer', :period_key, 1)
            ON CONFLICT ON CONSTRAINT uq_number_sequence_tenant_company_scope_period
            DO UPDATE SET next_value = number_sequence.next_value + 1, updated_at = now()
            RETURNING next_value
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "company_id": str(company_id),
            "period_key": period_key,
        },
    )
    if next_value is None:
        raise AppError(
            code="transfer_number_conflict",
            title="Conflict",
            status_code=409,
            detail="Could not allocate a stock transfer number.",
        )
    return f"ST-{period_key}-{int(next_value):06d}"


async def receive_stock(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    item_id: UUID,
    quantity: Decimal,
    value_minor: int,
    document_type: str,
    document_id: UUID,
    occurred_on: date,
    actor_user_id: UUID | None,
    warehouse_id: UUID | None = None,
) -> StockMove:
    """Add quantity and value to an item in one warehouse and record the move."""
    if quantity <= 0:
        raise AppError(
            code="stock_quantity_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="A stock receipt quantity must be positive.",
        )
    if value_minor < 0:
        raise AppError(
            code="stock_value_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="A stock receipt value cannot be negative.",
        )
    item = await _lock_item(session, tenant_id=tenant_id, company_id=company_id, item_id=item_id)
    target = warehouse_id or await ensure_default_warehouse(
        session, tenant_id=tenant_id, company_id=company_id
    )
    await _lock_warehouse(session, tenant_id=tenant_id, company_id=company_id, warehouse_id=target)
    balance = await _lock_balance(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        warehouse_id=target,
        item_id=item.id,
    )
    unit_cost = _round_minor(Decimal(value_minor) / quantity)
    move = StockMove(
        tenant_id=tenant_id,
        company_id=company_id,
        warehouse_id=target,
        item_id=item.id,
        move_type="receipt",
        quantity=quantity,
        unit_cost_minor=unit_cost,
        value_minor=value_minor,
        document_type=document_type,
        document_id=document_id,
        occurred_on=occurred_on,
        created_by=actor_user_id,
    )
    session.add(move)
    balance.quantity = balance.quantity + quantity
    balance.value_minor = balance.value_minor + value_minor
    item.quantity_on_hand = item.quantity_on_hand + quantity
    item.stock_value_minor = item.stock_value_minor + value_minor
    await session.flush()
    return move


async def issue_stock(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    item_id: UUID,
    quantity: Decimal,
    document_type: str,
    document_id: UUID,
    occurred_on: date,
    actor_user_id: UUID | None,
    warehouse_id: UUID | None = None,
) -> StockIssue:
    """Remove quantity from one warehouse at the item's weighted-average cost."""
    if quantity <= 0:
        raise AppError(
            code="stock_quantity_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="A stock issue quantity must be positive.",
        )
    item = await _lock_item(session, tenant_id=tenant_id, company_id=company_id, item_id=item_id)
    source = warehouse_id or await ensure_default_warehouse(
        session, tenant_id=tenant_id, company_id=company_id
    )
    await _lock_warehouse(session, tenant_id=tenant_id, company_id=company_id, warehouse_id=source)
    balance = await _lock_balance(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        warehouse_id=source,
        item_id=item.id,
    )
    if quantity > balance.quantity:
        raise AppError(
            code="insufficient_stock",
            title="Conflict",
            status_code=409,
            detail=(
                f"Item {item.code} has {balance.quantity} on hand in this warehouse; "
                f"cannot issue {quantity}."
            ),
        )
    if quantity == balance.quantity:
        value = balance.value_minor
    else:
        value = _round_minor(Decimal(balance.value_minor) * quantity / balance.quantity)
        value = min(value, balance.value_minor)
    unit_cost = _round_minor(Decimal(value) / quantity)
    move = StockMove(
        tenant_id=tenant_id,
        company_id=company_id,
        warehouse_id=source,
        item_id=item.id,
        move_type="issue",
        quantity=quantity,
        unit_cost_minor=unit_cost,
        value_minor=value,
        document_type=document_type,
        document_id=document_id,
        occurred_on=occurred_on,
        created_by=actor_user_id,
    )
    session.add(move)
    balance.quantity = balance.quantity - quantity
    balance.value_minor = balance.value_minor - value
    if balance.quantity == 0:
        balance.value_minor = 0
    item.quantity_on_hand = item.quantity_on_hand - quantity
    item.stock_value_minor = item.stock_value_minor - value
    if item.quantity_on_hand == 0:
        item.stock_value_minor = 0
    await session.flush()
    return StockIssue(move=move, value_minor=value)


async def _move_between_warehouses(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    request: TransferInput,
    actor_user_id: UUID,
) -> TransferResult:
    if request.quantity <= 0:
        raise AppError(
            code="stock_quantity_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="A stock transfer quantity must be positive.",
        )
    if request.from_warehouse_id == request.to_warehouse_id:
        raise AppError(
            code="transfer_same_warehouse",
            title="Unprocessable entity",
            status_code=422,
            detail="A stock transfer must move stock to a different warehouse.",
        )
    # Lock both warehouses in a stable order so opposite transfers cannot deadlock.
    for warehouse_id in sorted((request.from_warehouse_id, request.to_warehouse_id), key=str):
        await _lock_warehouse(
            session, tenant_id=tenant_id, company_id=company_id, warehouse_id=warehouse_id
        )
    item = await _lock_item(
        session, tenant_id=tenant_id, company_id=company_id, item_id=request.item_id
    )
    source = await _lock_balance(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        warehouse_id=request.from_warehouse_id,
        item_id=item.id,
    )
    if request.quantity > source.quantity:
        raise AppError(
            code="insufficient_stock",
            title="Conflict",
            status_code=409,
            detail=(
                f"Item {item.code} has {source.quantity} on hand in the source warehouse; "
                f"cannot transfer {request.quantity}."
            ),
        )
    if request.quantity == source.quantity:
        value_minor = source.value_minor
    else:
        value_minor = _round_minor(Decimal(source.value_minor) * request.quantity / source.quantity)
        value_minor = min(value_minor, source.value_minor)
    target = await _lock_balance(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        warehouse_id=request.to_warehouse_id,
        item_id=item.id,
    )
    number = await _allocate_transfer_number(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        transfer_date=request.transfer_date,
    )
    transfer = StockTransfer(
        tenant_id=tenant_id,
        company_id=company_id,
        number=number,
        transfer_date=request.transfer_date,
        from_warehouse_id=request.from_warehouse_id,
        to_warehouse_id=request.to_warehouse_id,
        item_id=item.id,
        quantity=request.quantity,
        value_minor=value_minor,
        memo=request.memo,
        posted_at=_now(),
        created_by=actor_user_id,
        updated_by=actor_user_id,
    )
    session.add(transfer)
    source.quantity = source.quantity - request.quantity
    source.value_minor = source.value_minor - value_minor
    if source.quantity == 0:
        source.value_minor = 0
    target.quantity = target.quantity + request.quantity
    target.value_minor = target.value_minor + value_minor
    await session.flush()
    return TransferResult(transfer=transfer, value_minor=value_minor)


async def create_stock_transfer(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: TransferInput,
) -> TransferResult:
    """Post an inter-warehouse transfer in one retryable transaction."""

    async def operation(session: AsyncSession) -> TransferResult:
        result = await _move_between_warehouses(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            request=request,
            actor_user_id=actor_user_id,
        )
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="inventory.transfer.posted",
            entity_type="stock_transfer",
            entity_id=result.transfer.id,
            detail={
                "number": result.transfer.number,
                "quantity": str(result.transfer.quantity),
                "value_minor": result.value_minor,
                "from_warehouse_id": str(request.from_warehouse_id),
                "to_warehouse_id": str(request.to_warehouse_id),
            },
        )
        return result

    return await unit_of_work.run(operation, tenant_id=tenant_id)


async def list_warehouses(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> list[Warehouse]:
    """List the company's warehouses, default first."""
    return list(
        (
            await session.execute(
                select(Warehouse)
                .where(
                    Warehouse.tenant_id == tenant_id,
                    Warehouse.company_id == company_id,
                )
                .order_by(Warehouse.is_default.desc(), Warehouse.code)
            )
        )
        .scalars()
        .all()
    )


async def create_warehouse(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    code: str,
    name_ar: str,
    name_en: str | None,
    is_default: bool,
) -> Warehouse:
    """Create a warehouse and keep exactly one default."""

    async def operation(session: AsyncSession) -> Warehouse:
        existing = await session.scalar(
            select(Warehouse.id).where(
                Warehouse.tenant_id == tenant_id,
                Warehouse.company_id == company_id,
            )
        )
        warehouse = Warehouse(
            tenant_id=tenant_id,
            company_id=company_id,
            code=code,
            name_ar=name_ar,
            name_en=name_en,
            is_default=is_default or existing is None,
            is_active=True,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        if warehouse.is_default:
            await session.execute(
                text(
                    "UPDATE warehouse SET is_default = false "
                    "WHERE tenant_id = :tenant_id AND company_id = :company_id"
                ),
                {"tenant_id": str(tenant_id), "company_id": str(company_id)},
            )
        session.add(warehouse)
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="inventory.warehouse.created",
            entity_type="warehouse",
            entity_id=warehouse.id,
            detail={"code": code, "is_default": warehouse.is_default},
        )
        return warehouse

    return await unit_of_work.run(operation, tenant_id=tenant_id)


async def update_warehouse(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    warehouse_id: UUID,
    name_ar: str,
    name_en: str | None,
    is_active: bool,
    is_default: bool,
) -> Warehouse:
    """Rename, deactivate or re-default a warehouse."""

    async def operation(session: AsyncSession) -> Warehouse:
        warehouse = await _lock_warehouse(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            warehouse_id=warehouse_id,
            active_required=False,
        )
        if not is_active and warehouse.is_default:
            raise AppError(
                code="default_warehouse_required",
                title="Conflict",
                status_code=409,
                detail="Choose another default warehouse before deactivating this one.",
            )
        warehouse.name_ar = name_ar
        warehouse.name_en = name_en
        warehouse.is_active = is_active
        if is_default and not warehouse.is_default:
            await session.execute(
                text(
                    "UPDATE warehouse SET is_default = false "
                    "WHERE tenant_id = :tenant_id AND company_id = :company_id "
                    "AND id <> :id"
                ),
                {
                    "tenant_id": str(tenant_id),
                    "company_id": str(company_id),
                    "id": str(warehouse.id),
                },
            )
            warehouse.is_default = True
        warehouse.updated_by = actor_user_id
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="inventory.warehouse.updated",
            entity_type="warehouse",
            entity_id=warehouse.id,
            detail={"is_active": is_active, "is_default": warehouse.is_default},
        )
        return warehouse

    return await unit_of_work.run(operation, tenant_id=tenant_id)


async def list_balances(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    warehouse_id: UUID | None = None,
) -> list[tuple[UUID, UUID, str, str, Decimal, int]]:
    """Return (warehouse, item, code, name, quantity, value) rows, item order."""
    query = (
        select(
            StockBalance.warehouse_id,
            StockBalance.item_id,
            Item.code,
            Item.name_ar,
            StockBalance.quantity,
            StockBalance.value_minor,
        )
        .join(
            Item,
            (Item.tenant_id == StockBalance.tenant_id)
            & (Item.company_id == StockBalance.company_id)
            & (Item.id == StockBalance.item_id),
        )
        .where(
            StockBalance.tenant_id == tenant_id,
            StockBalance.company_id == company_id,
        )
        .order_by(Item.code, StockBalance.warehouse_id)
    )
    if warehouse_id is not None:
        query = query.where(StockBalance.warehouse_id == warehouse_id)
    rows = (await session.execute(query)).all()
    return [
        (
            row.warehouse_id,
            row.item_id,
            row.code,
            row.name_ar,
            row.quantity,
            row.value_minor,
        )
        for row in rows
    ]


async def _allocate_count_number(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    count_date: date,
) -> str:
    period_key = f"{count_date.year:04d}"
    next_value = await session.scalar(
        text(
            """
            INSERT INTO number_sequence (tenant_id, company_id, scope, period_key, next_value)
            VALUES (:tenant_id, :company_id, 'stock_count', :period_key, 1)
            ON CONFLICT ON CONSTRAINT uq_number_sequence_tenant_company_scope_period
            DO UPDATE SET next_value = number_sequence.next_value + 1, updated_at = now()
            RETURNING next_value
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "company_id": str(company_id),
            "period_key": period_key,
        },
    )
    if next_value is None:
        raise _conflict("count_number_conflict", "Could not allocate a stock count number.")
    return f"SC-{period_key}-{int(next_value):06d}"


async def _resolve_open_period(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, on_date: date
) -> AccountingPeriod:
    period = (
        await session.execute(
            select(AccountingPeriod).where(
                AccountingPeriod.tenant_id == tenant_id,
                AccountingPeriod.company_id == company_id,
                AccountingPeriod.start_date <= on_date,
                AccountingPeriod.end_date >= on_date,
            )
        )
    ).scalar_one_or_none()
    if period is None:
        raise AppError(
            code="period_not_found",
            title="Not found",
            status_code=404,
            detail=f"No accounting period covers {on_date.isoformat()}.",
        )
    if period.status != "open":
        raise _conflict("period_closed", "The accounting period for this date is closed.")
    return period


async def _load_count_lines(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    count_id: UUID,
) -> list[StockCountLine]:
    return list(
        (
            await session.execute(
                select(StockCountLine)
                .where(
                    StockCountLine.tenant_id == tenant_id,
                    StockCountLine.company_id == company_id,
                    StockCountLine.stock_count_id == count_id,
                )
                .order_by(StockCountLine.created_at, StockCountLine.id)
            )
        )
        .scalars()
        .all()
    )


async def _load_count(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    count_id: UUID,
    for_update: bool = False,
) -> StockCount:
    query = select(StockCount).where(
        StockCount.tenant_id == tenant_id,
        StockCount.company_id == company_id,
        StockCount.id == count_id,
    )
    if for_update:
        query = query.with_for_update()
    count = (await session.execute(query)).scalar_one_or_none()
    if count is None:
        raise AppError(
            code="stock_count_not_found",
            title="Not found",
            status_code=404,
            detail="The stock count does not exist in the selected company.",
        )
    return count


def _check_count_lines(lines: tuple[CountLineInput, ...]) -> None:
    if not lines:
        raise _unprocessable("count_lines_invalid", "A stock count needs at least one line.")
    if len(lines) > 500:
        raise _unprocessable("count_lines_invalid", "A stock count is limited to 500 lines.")
    seen: set[UUID] = set()
    for line in lines:
        if line.item_id in seen:
            raise _unprocessable(
                "count_lines_invalid", "Each item may appear only once in a stock count."
            )
        seen.add(line.item_id)
        if line.counted_quantity < 0:
            raise _unprocessable("count_quantity_invalid", "A counted quantity cannot be negative.")


async def _replace_count_lines(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    count: StockCount,
    lines: tuple[CountLineInput, ...],
    actor_user_id: UUID,
) -> list[StockCountLine]:
    await session.execute(
        delete(StockCountLine).where(
            StockCountLine.tenant_id == tenant_id,
            StockCountLine.company_id == company_id,
            StockCountLine.stock_count_id == count.id,
        )
    )
    items = {
        item.id: item
        for item in (
            await session.execute(
                select(Item).where(
                    Item.tenant_id == tenant_id,
                    Item.company_id == company_id,
                    Item.id.in_([line.item_id for line in lines]),
                )
            )
        )
        .scalars()
        .all()
    }
    if len(items) != len(lines):
        raise AppError(
            code="item_not_found",
            title="Not found",
            status_code=404,
            detail="One or more counted items do not exist in the selected company.",
        )
    for item in items.values():
        if not item.is_active:
            raise _unprocessable("item_inactive", f"Item {item.code} is not active.")
    balances = {
        row.item_id: row
        for row in (
            await session.execute(
                select(StockBalance).where(
                    StockBalance.tenant_id == tenant_id,
                    StockBalance.company_id == company_id,
                    StockBalance.warehouse_id == count.warehouse_id,
                )
            )
        )
        .scalars()
        .all()
    }
    records: list[StockCountLine] = []
    for line in lines:
        balance = balances.get(line.item_id)
        system_quantity = balance.quantity if balance is not None else Decimal("0")
        record = StockCountLine(
            tenant_id=tenant_id,
            company_id=company_id,
            stock_count_id=count.id,
            item_id=line.item_id,
            counted_quantity=line.counted_quantity,
            system_quantity=system_quantity,
            variance_quantity=line.counted_quantity - system_quantity,
            variance_value_minor=0,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(record)
        records.append(record)
    await session.flush()
    return records


async def create_stock_count(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: CountInput,
) -> CountResult:
    """Create an editable draft count for one warehouse."""
    _check_count_lines(request.lines)

    async def operation(session: AsyncSession) -> CountResult:
        await _lock_warehouse(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            warehouse_id=request.warehouse_id,
        )
        number = await _allocate_count_number(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            count_date=request.count_date,
        )
        count = StockCount(
            tenant_id=tenant_id,
            company_id=request.company_id,
            number=number,
            count_date=request.count_date,
            warehouse_id=request.warehouse_id,
            status="draft",
            memo=request.memo,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(count)
        await session.flush()
        lines = await _replace_count_lines(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            count=count,
            lines=request.lines,
            actor_user_id=actor_user_id,
        )
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="inventory.count.created",
            entity_type="stock_count",
            entity_id=count.id,
            detail={"number": number, "lines": len(lines)},
        )
        return CountResult(count=count, lines=tuple(lines), journal_entry_id=None)

    return await unit_of_work.run(operation, tenant_id=tenant_id)


async def update_stock_count(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    count_id: UUID,
    request: CountInput,
) -> CountResult:
    """Replace a draft count's lines; posted counts are immutable."""
    _check_count_lines(request.lines)

    async def operation(session: AsyncSession) -> CountResult:
        count = await _load_count(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            count_id=count_id,
            for_update=True,
        )
        if count.status != "draft":
            raise _conflict("count_already_posted", "A posted stock count cannot be edited.")
        if count.warehouse_id != request.warehouse_id:
            raise _unprocessable(
                "count_warehouse_immutable", "The warehouse of an existing count cannot change."
            )
        count.count_date = request.count_date
        count.memo = request.memo
        count.updated_by = actor_user_id
        lines = await _replace_count_lines(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            count=count,
            lines=request.lines,
            actor_user_id=actor_user_id,
        )
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="inventory.count.updated",
            entity_type="stock_count",
            entity_id=count.id,
            detail={"number": count.number, "lines": len(lines)},
        )
        return CountResult(count=count, lines=tuple(lines), journal_entry_id=None)

    return await unit_of_work.run(operation, tenant_id=tenant_id)


async def post_stock_count(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    count_id: UUID,
) -> CountResult:
    """Apply the counted quantities and post the variance to the adjustment account."""

    async def operation(session: AsyncSession) -> CountResult:
        count = await _load_count(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            count_id=count_id,
            for_update=True,
        )
        if count.status != "draft":
            raise _conflict("count_already_posted", "This stock count is already posted.")
        await _lock_warehouse(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            warehouse_id=count.warehouse_id,
        )
        period = await _resolve_open_period(
            session, tenant_id=tenant_id, company_id=company_id, on_date=count.count_date
        )
        accounts = await resolve_posting_accounts(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            on_date=count.count_date,
            keys=("inventory", "inventory_adjustment"),
        )
        lines = await _load_count_lines(
            session, tenant_id=tenant_id, company_id=company_id, count_id=count.id
        )
        if not lines:
            raise _unprocessable("count_lines_invalid", "This stock count has no lines.")
        net_delta = 0
        for line in lines:
            item = await _lock_item(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                item_id=line.item_id,
            )
            balance = await _lock_balance(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                warehouse_id=count.warehouse_id,
                item_id=line.item_id,
            )
            current_quantity = balance.quantity
            current_value = balance.value_minor
            variance = line.counted_quantity - current_quantity
            delta = 0
            if variance < 0:
                shortage = -variance
                if current_quantity > 0:
                    delta = -_round_minor(Decimal(current_value) * shortage / current_quantity)
                    delta = max(delta, -current_value)
            elif variance > 0:
                if current_quantity > 0:
                    unit_cost = Decimal(current_value) / current_quantity
                elif item.quantity_on_hand > 0:
                    unit_cost = Decimal(item.stock_value_minor) / item.quantity_on_hand
                else:
                    unit_cost = Decimal("0")
                delta = max(_round_minor(unit_cost * variance), 0)
            new_quantity = line.counted_quantity
            new_value = current_value + delta
            if new_quantity == 0:
                new_value = 0
            new_value = max(new_value, 0)
            delta = new_value - current_value
            line.system_quantity = current_quantity
            line.variance_quantity = variance
            line.variance_value_minor = delta
            balance.quantity = new_quantity
            balance.value_minor = new_value
            item.quantity_on_hand = item.quantity_on_hand + variance
            item.stock_value_minor = item.stock_value_minor + delta
            if item.quantity_on_hand == 0:
                item.stock_value_minor = 0
            item.stock_value_minor = max(item.stock_value_minor, 0)
            net_delta += delta
            if variance != 0:
                unit_cost = (
                    _round_minor(Decimal(abs(delta)) / abs(variance)) if variance != 0 else 0
                )
                session.add(
                    StockMove(
                        tenant_id=tenant_id,
                        company_id=company_id,
                        warehouse_id=count.warehouse_id,
                        item_id=line.item_id,
                        move_type="count_adjustment",
                        quantity=abs(variance),
                        unit_cost_minor=unit_cost,
                        value_minor=abs(delta),
                        document_type="stock_count",
                        document_id=count.id,
                        occurred_on=count.count_date,
                        created_by=actor_user_id,
                    )
                )
        journal_entry_id: UUID | None = None
        if net_delta != 0:
            inventory_account = accounts["inventory"]
            adjustment_account = accounts["inventory_adjustment"]
            if net_delta > 0:
                journal_lines = (
                    JournalLineInput(
                        account_id=inventory_account.id,
                        debit_minor=net_delta,
                        description=f"Stock count {count.number}",
                    ),
                    JournalLineInput(
                        account_id=adjustment_account.id,
                        credit_minor=net_delta,
                        description=f"Stock count {count.number}",
                    ),
                )
            else:
                amount = -net_delta
                journal_lines = (
                    JournalLineInput(
                        account_id=adjustment_account.id,
                        debit_minor=amount,
                        description=f"Stock count {count.number}",
                    ),
                    JournalLineInput(
                        account_id=inventory_account.id,
                        credit_minor=amount,
                        description=f"Stock count {count.number}",
                    ),
                )
            entry = await post_entry_in_session(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                company_id=company_id,
                period_id=period.id,
                entry_date=count.count_date,
                memo=f"Stock count {count.number}",
                lines=journal_lines,
                source_type="stock_count",
                source_id=count.id,
            )
            journal_entry_id = entry.entry.id
        count.status = "posted"
        count.posted_at = _now()
        count.journal_entry_id = journal_entry_id
        count.updated_by = actor_user_id
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="inventory.count.posted",
            entity_type="stock_count",
            entity_id=count.id,
            detail={
                "number": count.number,
                "net_value_minor": net_delta,
                "journal_entry_id": str(journal_entry_id) if journal_entry_id else None,
            },
        )
        return CountResult(
            count=count,
            lines=tuple(lines),
            journal_entry_id=journal_entry_id,
        )

    return await unit_of_work.run(operation, tenant_id=tenant_id)


async def list_stock_counts(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> list[StockCount]:
    """List stock counts, newest count date first."""
    return list(
        (
            await session.execute(
                select(StockCount)
                .where(
                    StockCount.tenant_id == tenant_id,
                    StockCount.company_id == company_id,
                )
                .order_by(StockCount.count_date.desc(), StockCount.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def get_stock_count(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    count_id: UUID,
) -> CountResult:
    """Load one count with its lines."""
    count = await _load_count(
        session, tenant_id=tenant_id, company_id=company_id, count_id=count_id
    )
    lines = await _load_count_lines(
        session, tenant_id=tenant_id, company_id=company_id, count_id=count.id
    )
    return CountResult(count=count, lines=tuple(lines), journal_entry_id=count.journal_entry_id)


async def delete_stock_count(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    count_id: UUID,
) -> None:
    """Delete a draft count; posted counts are permanent."""

    async def operation(session: AsyncSession) -> None:
        count = await _load_count(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            count_id=count_id,
            for_update=True,
        )
        if count.status != "draft":
            raise _conflict("count_already_posted", "A posted stock count cannot be deleted.")
        await session.execute(
            delete(StockCountLine).where(
                StockCountLine.tenant_id == tenant_id,
                StockCountLine.company_id == company_id,
                StockCountLine.stock_count_id == count.id,
            )
        )
        await session.delete(count)
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="inventory.count.deleted",
            entity_type="stock_count",
            entity_id=count.id,
            detail={"number": count.number},
        )

    await unit_of_work.run(operation, tenant_id=tenant_id)
