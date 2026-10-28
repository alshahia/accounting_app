"""Master data endpoints: customers, suppliers, items and tax codes (day 7)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import PrincipalDep, TenantSessionDep, UnitOfWorkDep, require_company_permission
from app.modules.catalog import service
from app.modules.catalog.models import Customer, Item, Supplier, TaxCode, TaxCodeRate
from app.modules.identity.service import CompanyAccess

router = APIRouter(prefix="/catalog", tags=["catalog"])


class CustomerCreate(BaseModel):
    """A customer to add to the selected company."""

    code: Annotated[str, Field(min_length=1, max_length=30)]
    name_ar: Annotated[str, Field(min_length=1, max_length=200)]
    name_en: Annotated[str | None, Field(max_length=200)] = None
    tax_number: Annotated[str | None, Field(max_length=50)] = None


class CustomerOut(BaseModel):
    """A customer record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name_ar: str
    name_en: str | None
    tax_number: str | None
    is_active: bool
    created_at: datetime


class SupplierCreate(CustomerCreate):
    """A supplier to add to the selected company."""


class SupplierOut(CustomerOut):
    """A supplier record."""


class TaxCodeCreate(BaseModel):
    """A VAT code; the rate is basis points (1500 = 15.00%)."""

    code: Annotated[str, Field(min_length=1, max_length=30)]
    name_ar: Annotated[str, Field(min_length=1, max_length=200)]
    rate_basis_points: Annotated[int, Field(ge=0, le=10000)]
    kind: Annotated[str, Field(pattern="^(sales|purchase|withholding)$")] = "sales"
    jurisdiction: Annotated[str | None, Field(max_length=50)] = None
    valid_from: date | None = None


class TaxRateCreate(BaseModel):
    """A new effective-dated rate for an existing tax code."""

    rate_basis_points: Annotated[int, Field(ge=0, le=10000)]
    valid_from: date


class TaxRateOut(BaseModel):
    """One effective-dated rate row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    rate_basis_points: int
    valid_from: date
    valid_to: date | None


class TaxCodeOut(BaseModel):
    """A tax code record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name_ar: str
    rate_basis_points: int
    kind: str
    jurisdiction: str | None
    is_active: bool


class ItemCreate(BaseModel):
    """A stock item to add to the selected company."""

    code: Annotated[str, Field(min_length=1, max_length=50)]
    name_ar: Annotated[str, Field(min_length=1, max_length=200)]
    name_en: Annotated[str | None, Field(max_length=200)] = None
    unit_of_measure: Annotated[str, Field(min_length=1, max_length=20)] = "piece"


class ItemOut(BaseModel):
    """An item with its current stock position."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name_ar: str
    name_en: str | None
    unit_of_measure: str
    quantity_on_hand: Decimal
    stock_value_minor: int
    is_active: bool


@router.post(
    "/customers",
    response_model=CustomerOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer",
)
async def create_customer(
    payload: CustomerCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("settings.manage"))],
) -> Customer:
    """Create a customer in the selected company."""
    return await service.create_customer(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        code=payload.code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        tax_number=payload.tax_number,
    )


@router.get("/customers", response_model=list[CustomerOut], summary="List customers")
async def read_customers(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[Customer]:
    """List the selected company's customers by code."""
    return list(
        await service.list_customers(session, tenant_id=principal.tenant_id, company_id=company.id)
    )


@router.post(
    "/suppliers",
    response_model=SupplierOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a supplier",
)
async def create_supplier(
    payload: SupplierCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("settings.manage"))],
) -> Supplier:
    """Create a supplier in the selected company."""
    return await service.create_supplier(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        code=payload.code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        tax_number=payload.tax_number,
    )


@router.get("/suppliers", response_model=list[SupplierOut], summary="List suppliers")
async def read_suppliers(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[Supplier]:
    """List the selected company's suppliers by code."""
    return list(
        await service.list_suppliers(session, tenant_id=principal.tenant_id, company_id=company.id)
    )


@router.post(
    "/tax-codes",
    response_model=TaxCodeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tax code",
)
async def create_tax_code(
    payload: TaxCodeCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("tax.manage"))],
) -> TaxCode:
    """Create a VAT code for the selected company."""
    return await service.create_tax_code(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        code=payload.code,
        name_ar=payload.name_ar,
        rate_basis_points=payload.rate_basis_points,
        kind=payload.kind,
        jurisdiction=payload.jurisdiction,
        valid_from=payload.valid_from,
    )


@router.get("/tax-codes", response_model=list[TaxCodeOut], summary="List tax codes")
async def read_tax_codes(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[TaxCode]:
    """List the selected company's tax codes."""
    return list(
        await service.list_tax_codes(session, tenant_id=principal.tenant_id, company_id=company.id)
    )


@router.get(
    "/tax-codes/{tax_code_id}/rates",
    response_model=list[TaxRateOut],
    summary="List a tax code's rate history",
)
async def read_tax_rates(
    tax_code_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[TaxCodeRate]:
    """Return the effective-dated rates for one code, newest first."""
    return list(
        await service.list_tax_rates(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            tax_code_id=tax_code_id,
        )
    )


@router.put(
    "/tax-codes/{tax_code_id}/rate",
    response_model=TaxCodeOut,
    summary="Add an effective-dated tax rate",
)
async def set_tax_rate(
    tax_code_id: UUID,
    payload: TaxRateCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("tax.manage"))],
) -> TaxCode:
    """Add a rate that applies from a date without rewriting earlier documents."""
    return await service.set_tax_rate(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        tax_code_id=tax_code_id,
        rate_basis_points=payload.rate_basis_points,
        valid_from=payload.valid_from,
    )


@router.post(
    "/items",
    response_model=ItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an item",
)
async def create_item(
    payload: ItemCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("inventory.manage"))],
) -> Item:
    """Create a stock item for the selected company."""
    return await service.create_item(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        code=payload.code,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        unit_of_measure=payload.unit_of_measure,
    )


@router.get("/items", response_model=list[ItemOut], summary="List items")
async def read_items(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[Item]:
    """List the selected company's items by code."""
    return list(
        await service.list_items(session, tenant_id=principal.tenant_id, company_id=company.id)
    )
