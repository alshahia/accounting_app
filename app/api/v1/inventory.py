"""Inventory endpoints: warehouses, per-warehouse balances and transfers.

Warehouses are the stock locations v1 requires; balances show quantity and value
per location while the item row keeps the company total, and a transfer moves
quantity at the item's weighted-average cost without touching the GL.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import PrincipalDep, TenantSessionDep, UnitOfWorkDep, require_company_permission
from app.modules.identity.service import CompanyAccess
from app.modules.inventory import service
from app.modules.inventory.service import TransferInput

router = APIRouter(prefix="/inventory", tags=["inventory"])


class WarehouseOut(BaseModel):
    """One stock location."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name_ar: str
    name_en: str | None
    is_default: bool
    is_active: bool


class WarehouseCreate(BaseModel):
    """A new warehouse; the first one becomes the default automatically."""

    code: Annotated[str, Field(min_length=1, max_length=20)]
    name_ar: Annotated[str, Field(min_length=1, max_length=200)]
    name_en: Annotated[str | None, Field(max_length=200)] = None
    is_default: bool = False


class WarehouseUpdate(BaseModel):
    """A full replacement of the editable warehouse fields."""

    name_ar: Annotated[str, Field(min_length=1, max_length=200)]
    name_en: Annotated[str | None, Field(max_length=200)] = None
    is_active: bool = True
    is_default: bool = False


class BalanceOut(BaseModel):
    """Quantity and value of one item in one warehouse."""

    warehouse_id: UUID
    item_id: UUID
    item_code: str
    item_name_ar: str
    quantity: Decimal
    value_minor: int


class TransferCreate(BaseModel):
    """Move one item between two warehouses."""

    transfer_date: date
    from_warehouse_id: UUID
    to_warehouse_id: UUID
    item_id: UUID
    quantity: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
    memo: Annotated[str | None, Field(max_length=300)] = None


class TransferOut(BaseModel):
    """A posted stock transfer."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    number: str
    transfer_date: date
    from_warehouse_id: UUID
    to_warehouse_id: UUID
    item_id: UUID
    quantity: Decimal
    value_minor: int
    memo: str | None
    posted_at: object


@router.get("/warehouses", response_model=list[WarehouseOut], summary="List warehouses")
async def read_warehouses(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[WarehouseOut]:
    """List the selected company's warehouses, default first."""
    rows = await service.list_warehouses(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    return [WarehouseOut.model_validate(row) for row in rows]


@router.post(
    "/warehouses",
    response_model=WarehouseOut,
    status_code=201,
    summary="Create a warehouse",
)
async def create_warehouse(
    payload: WarehouseCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("inventory.manage"))],
) -> WarehouseOut:
    """Create a warehouse; choosing it as default clears the previous default."""
    warehouse = await service.create_warehouse(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        code=payload.code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        is_default=payload.is_default,
    )
    return WarehouseOut.model_validate(warehouse)


@router.put(
    "/warehouses/{warehouse_id}",
    response_model=WarehouseOut,
    summary="Update a warehouse",
)
async def update_warehouse(
    warehouse_id: UUID,
    payload: WarehouseUpdate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("inventory.manage"))],
) -> WarehouseOut:
    """Rename, deactivate or make a warehouse the default."""
    warehouse = await service.update_warehouse(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        warehouse_id=warehouse_id,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        is_active=payload.is_active,
        is_default=payload.is_default,
    )
    return WarehouseOut.model_validate(warehouse)


@router.get("/balances", response_model=list[BalanceOut], summary="Stock balances")
async def read_balances(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    warehouse_id: UUID | None = None,
) -> list[BalanceOut]:
    """Per-warehouse quantity and value, optionally filtered to one warehouse."""
    rows = await service.list_balances(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        warehouse_id=warehouse_id,
    )
    return [
        BalanceOut(
            warehouse_id=row[0],
            item_id=row[1],
            item_code=row[2],
            item_name_ar=row[3],
            quantity=row[4],
            value_minor=row[5],
        )
        for row in rows
    ]


@router.post(
    "/transfers",
    response_model=TransferOut,
    status_code=201,
    summary="Transfer stock between warehouses",
)
async def create_transfer(
    payload: TransferCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("inventory.manage"))],
) -> TransferOut:
    """Move quantity at weighted-average cost; the company total is unchanged."""
    result = await service.create_stock_transfer(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=TransferInput(
            company_id=company.id,
            from_warehouse_id=payload.from_warehouse_id,
            to_warehouse_id=payload.to_warehouse_id,
            item_id=payload.item_id,
            quantity=payload.quantity,
            transfer_date=payload.transfer_date,
            memo=payload.memo,
        ),
    )
    return TransferOut.model_validate(result.transfer)


class CountLineIn(BaseModel):
    """One item and the quantity physically counted."""

    item_id: UUID
    counted_quantity: Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]


class StockCountCreate(BaseModel):
    """A stock count for one warehouse; saving it creates an editable draft."""

    count_date: date
    warehouse_id: UUID
    memo: Annotated[str | None, Field(max_length=300)] = None
    lines: Annotated[list[CountLineIn], Field(min_length=1, max_length=500)]


class StockCountLineOut(BaseModel):
    """One counted line with the system quantity and the variance computed at posting."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    item_id: UUID
    counted_quantity: Decimal
    system_quantity: Decimal
    variance_quantity: Decimal
    variance_value_minor: int


class StockCountSummary(BaseModel):
    """A stock count without its lines."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    number: str
    count_date: date
    warehouse_id: UUID
    status: str
    memo: str | None
    journal_entry_id: UUID | None
    posted_at: object | None


class StockCountOut(StockCountSummary):
    """A stock count with its lines."""

    lines: list[StockCountLineOut]


def _count_out(result: service.CountResult) -> StockCountOut:
    return StockCountOut(
        id=result.count.id,
        number=result.count.number,
        count_date=result.count.count_date,
        warehouse_id=result.count.warehouse_id,
        status=result.count.status,
        memo=result.count.memo,
        journal_entry_id=result.count.journal_entry_id,
        posted_at=result.count.posted_at,
        lines=[StockCountLineOut.model_validate(line) for line in result.lines],
    )


@router.get("/counts", response_model=list[StockCountSummary], summary="List stock counts")
async def read_stock_counts(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[StockCountSummary]:
    """List stock counts, newest count date first."""
    rows = await service.list_stock_counts(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    return [StockCountSummary.model_validate(row) for row in rows]


@router.post(
    "/counts",
    response_model=StockCountOut,
    status_code=201,
    summary="Create a draft stock count",
)
async def create_stock_count(
    payload: StockCountCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("inventory.manage"))],
) -> StockCountOut:
    """Snapshot the warehouse quantities and save the counted lines as a draft."""
    result = await service.create_stock_count(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=service.CountInput(
            company_id=company.id,
            warehouse_id=payload.warehouse_id,
            count_date=payload.count_date,
            memo=payload.memo,
            lines=tuple(
                service.CountLineInput(item_id=line.item_id, counted_quantity=line.counted_quantity)
                for line in payload.lines
            ),
        ),
    )
    return _count_out(result)


@router.get("/counts/{count_id}", response_model=StockCountOut, summary="Read a stock count")
async def read_stock_count(
    count_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> StockCountOut:
    """Return one count with its lines."""
    result = await service.get_stock_count(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        count_id=count_id,
    )
    return _count_out(result)


@router.put(
    "/counts/{count_id}",
    response_model=StockCountOut,
    summary="Update a draft stock count",
)
async def update_stock_count(
    count_id: UUID,
    payload: StockCountCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("inventory.manage"))],
) -> StockCountOut:
    """Replace a draft count's lines; the warehouse of an existing count is fixed."""
    result = await service.update_stock_count(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        count_id=count_id,
        request=service.CountInput(
            company_id=company.id,
            warehouse_id=payload.warehouse_id,
            count_date=payload.count_date,
            memo=payload.memo,
            lines=tuple(
                service.CountLineInput(item_id=line.item_id, counted_quantity=line.counted_quantity)
                for line in payload.lines
            ),
        ),
    )
    return _count_out(result)


@router.post(
    "/counts/{count_id}/post",
    response_model=StockCountOut,
    summary="Post a stock count",
)
async def post_stock_count(
    count_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("inventory.manage"))],
) -> StockCountOut:
    """Apply counted quantities and post the variance against inventory adjustment."""
    result = await service.post_stock_count(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        count_id=count_id,
    )
    return _count_out(result)


@router.delete("/counts/{count_id}", status_code=204, summary="Delete a draft stock count")
async def delete_stock_count(
    count_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("inventory.manage"))],
) -> None:
    """Delete a draft count."""
    await service.delete_stock_count(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        count_id=count_id,
    )
