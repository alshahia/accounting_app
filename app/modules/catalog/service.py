"""Master data creation and lookup (phase 0, day 7).

Codes are unique per company; a duplicate surfaces as a 409 problem+json error
instead of a database trace. These helpers run inside a unit of work.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import Customer, Item, Supplier, TaxCode, TaxCodeRate
from app.modules.identity.audit import record_audit
from app.shared.errors import AppError
from app.shared.unit_of_work import UnitOfWork


def _conflict(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Conflict", status_code=409, detail=detail)


@dataclass(frozen=True)
class MasterDataResult:
    """A created master record plus the audit action it produced."""

    entity: object
    action: str


async def create_customer(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    code: str,
    name_ar: str,
    name_en: str | None = None,
    tax_number: str | None = None,
) -> Customer:
    async def operation(session: AsyncSession) -> Customer:
        customer = Customer(
            tenant_id=tenant_id,
            company_id=company_id,
            code=code,
            name_ar=name_ar,
            name_en=name_en,
            tax_number=tax_number,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(customer)
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="catalog.customer.created",
            entity_type="customer",
            entity_id=customer.id,
            detail={"code": code},
        )
        return customer

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError:
        raise _conflict("duplicate_code", "A customer with this code already exists.") from None


async def create_supplier(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    code: str,
    name_ar: str,
    name_en: str | None = None,
    tax_number: str | None = None,
) -> Supplier:
    async def operation(session: AsyncSession) -> Supplier:
        supplier = Supplier(
            tenant_id=tenant_id,
            company_id=company_id,
            code=code,
            name_ar=name_ar,
            name_en=name_en,
            tax_number=tax_number,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(supplier)
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="catalog.supplier.created",
            entity_type="supplier",
            entity_id=supplier.id,
            detail={"code": code},
        )
        return supplier

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError:
        raise _conflict("duplicate_code", "A supplier with this code already exists.") from None


async def create_tax_code(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    code: str,
    name_ar: str,
    rate_basis_points: int,
    kind: str = "sales",
    jurisdiction: str | None = None,
    valid_from: date | None = None,
) -> TaxCode:
    if kind not in {"sales", "purchase", "withholding"}:
        raise AppError(
            code="tax_kind_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="The tax kind must be sales, purchase or withholding.",
        )
    if not 0 <= rate_basis_points <= 10000:
        raise AppError(
            code="tax_rate_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="The tax rate must be between 0 and 10000 basis points.",
        )

    first_rate_date = valid_from or date(2000, 1, 1)

    async def operation(session: AsyncSession) -> TaxCode:
        tax_code = TaxCode(
            tenant_id=tenant_id,
            company_id=company_id,
            code=code,
            name_ar=name_ar,
            rate_basis_points=rate_basis_points,
            kind=kind,
            jurisdiction=jurisdiction,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(tax_code)
        await session.flush()
        session.add(
            TaxCodeRate(
                tenant_id=tenant_id,
                company_id=company_id,
                tax_code_id=tax_code.id,
                rate_basis_points=rate_basis_points,
                valid_from=first_rate_date,
                created_by=actor_user_id,
                updated_by=actor_user_id,
            )
        )
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="catalog.tax_code.created",
            entity_type="tax_code",
            entity_id=tax_code.id,
            detail={
                "code": code,
                "rate_basis_points": rate_basis_points,
                "kind": kind,
                "jurisdiction": jurisdiction,
                "valid_from": first_rate_date.isoformat(),
            },
        )
        return tax_code

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError:
        raise _conflict("duplicate_code", "A tax code with this code already exists.") from None


async def set_tax_rate(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    tax_code_id: UUID,
    rate_basis_points: int,
    valid_from: date,
) -> TaxCode:
    """Add an effective-dated rate, keeping the ranges contiguous.

    The row that was open on the new date is closed the day before; if a later
    rate already exists, the new row is closed the day before that one. Equal
    start dates are rejected instead of replaced, so history is never rewritten.
    """
    if not 0 <= rate_basis_points <= 10000:
        raise AppError(
            code="tax_rate_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="The tax rate must be between 0 and 10000 basis points.",
        )

    async def operation(session: AsyncSession) -> TaxCode:
        tax_code = (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.tenant_id == tenant_id,
                    TaxCode.company_id == company_id,
                    TaxCode.id == tax_code_id,
                )
            )
        ).scalar_one_or_none()
        if tax_code is None:
            raise AppError(
                code="tax_code_not_found",
                title="Not found",
                status_code=404,
                detail="The tax code does not exist in the selected company.",
            )
        duplicate = (
            await session.execute(
                select(TaxCodeRate.id).where(
                    TaxCodeRate.tenant_id == tenant_id,
                    TaxCodeRate.company_id == company_id,
                    TaxCodeRate.tax_code_id == tax_code_id,
                    TaxCodeRate.valid_from == valid_from,
                )
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise _conflict(
                "tax_rate_duplicate",
                "A rate already starts on that effective date; choose another date.",
            )
        previous = (
            await session.execute(
                select(TaxCodeRate)
                .where(
                    TaxCodeRate.tenant_id == tenant_id,
                    TaxCodeRate.company_id == company_id,
                    TaxCodeRate.tax_code_id == tax_code_id,
                    TaxCodeRate.valid_from < valid_from,
                )
                .order_by(TaxCodeRate.valid_from.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if previous is not None and (previous.valid_to is None or previous.valid_to >= valid_from):
            previous.valid_to = valid_from - timedelta(days=1)
            previous.updated_by = actor_user_id
        later = (
            await session.execute(
                select(TaxCodeRate)
                .where(
                    TaxCodeRate.tenant_id == tenant_id,
                    TaxCodeRate.company_id == company_id,
                    TaxCodeRate.tax_code_id == tax_code_id,
                    TaxCodeRate.valid_from > valid_from,
                )
                .order_by(TaxCodeRate.valid_from)
                .limit(1)
            )
        ).scalar_one_or_none()
        rate_row = TaxCodeRate(
            tenant_id=tenant_id,
            company_id=company_id,
            tax_code_id=tax_code_id,
            rate_basis_points=rate_basis_points,
            valid_from=valid_from,
            valid_to=(later.valid_from - timedelta(days=1)) if later is not None else None,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(rate_row)
        await session.flush()
        latest = (
            await session.execute(
                select(TaxCodeRate)
                .where(
                    TaxCodeRate.tenant_id == tenant_id,
                    TaxCodeRate.company_id == company_id,
                    TaxCodeRate.tax_code_id == tax_code_id,
                )
                .order_by(TaxCodeRate.valid_from.desc())
                .limit(1)
            )
        ).scalar_one()
        tax_code.rate_basis_points = latest.rate_basis_points
        tax_code.updated_by = actor_user_id
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="settings.tax_rate.updated",
            entity_type="tax_code",
            entity_id=tax_code_id,
            detail={
                "rate_basis_points": rate_basis_points,
                "valid_from": valid_from.isoformat(),
            },
        )
        return tax_code

    return await unit_of_work.run(operation, tenant_id=tenant_id)


async def list_tax_rates(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, tax_code_id: UUID
) -> Sequence[TaxCodeRate]:
    """Return a code's rate history, newest effective date first."""
    return (
        (
            await session.execute(
                select(TaxCodeRate)
                .where(
                    TaxCodeRate.tenant_id == tenant_id,
                    TaxCodeRate.company_id == company_id,
                    TaxCodeRate.tax_code_id == tax_code_id,
                )
                .order_by(TaxCodeRate.valid_from.desc())
            )
        )
        .scalars()
        .all()
    )


async def create_item(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    code: str,
    name_ar: str,
    unit_of_measure: str,
    name_en: str | None = None,
) -> Item:
    async def operation(session: AsyncSession) -> Item:
        item = Item(
            tenant_id=tenant_id,
            company_id=company_id,
            code=code,
            name_ar=name_ar,
            name_en=name_en,
            unit_of_measure=unit_of_measure,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(item)
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="catalog.item.created",
            entity_type="item",
            entity_id=item.id,
            detail={"code": code},
        )
        return item

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError:
        raise _conflict("duplicate_code", "An item with this code already exists.") from None


async def list_customers(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> Sequence[Customer]:
    return (
        (
            await session.execute(
                select(Customer)
                .where(Customer.tenant_id == tenant_id, Customer.company_id == company_id)
                .order_by(Customer.code)
            )
        )
        .scalars()
        .all()
    )


async def list_suppliers(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> Sequence[Supplier]:
    return (
        (
            await session.execute(
                select(Supplier)
                .where(Supplier.tenant_id == tenant_id, Supplier.company_id == company_id)
                .order_by(Supplier.code)
            )
        )
        .scalars()
        .all()
    )


async def list_tax_codes(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> Sequence[TaxCode]:
    return (
        (
            await session.execute(
                select(TaxCode)
                .where(TaxCode.tenant_id == tenant_id, TaxCode.company_id == company_id)
                .order_by(TaxCode.code)
            )
        )
        .scalars()
        .all()
    )


async def list_items(session: AsyncSession, *, tenant_id: UUID, company_id: UUID) -> Sequence[Item]:
    return (
        (
            await session.execute(
                select(Item)
                .where(Item.tenant_id == tenant_id, Item.company_id == company_id)
                .order_by(Item.code)
            )
        )
        .scalars()
        .all()
    )
