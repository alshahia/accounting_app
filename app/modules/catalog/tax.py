"""Effective-dated tax-rate resolution.

A tax rate is configuration with a date range, because a rate change must not
rewrite the tax an already-dated invoice or bill charged. Documents store the
rate they used on their lines, and posting resolves the rate on the document
date from this history (analysis/20 §5: "tax codes, effective-dated rates").
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import TaxCodeRate
from app.shared.errors import AppError


async def rates_on(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    tax_code_ids: set[UUID],
    on_date: date,
) -> dict[UUID, int]:
    """Resolve each code's basis-point rate on the given date.

    A code with no row covering the date is a configuration error, not a silent
    zero: posting a document with an unavailable rate would understate tax.
    """
    if not tax_code_ids:
        return {}
    rows = (
        await session.execute(
            select(
                TaxCodeRate.tax_code_id,
                TaxCodeRate.rate_basis_points,
                TaxCodeRate.valid_from,
            )
            .where(
                TaxCodeRate.tenant_id == tenant_id,
                TaxCodeRate.company_id == company_id,
                TaxCodeRate.tax_code_id.in_(tax_code_ids),
                TaxCodeRate.valid_from <= on_date,
                or_(TaxCodeRate.valid_to.is_(None), TaxCodeRate.valid_to >= on_date),
            )
            .order_by(TaxCodeRate.tax_code_id, TaxCodeRate.valid_from.desc())
        )
    ).all()
    rates: dict[UUID, int] = {}
    for tax_code_id, rate_basis_points, _valid_from in rows:
        rates.setdefault(tax_code_id, rate_basis_points)
    missing = tax_code_ids - set(rates)
    if missing:
        raise AppError(
            code="tax_rate_missing",
            title="Conflict",
            status_code=409,
            detail=(
                "One or more tax codes have no rate for the document date. "
                "Add an effective-dated rate in tax settings."
            ),
        )
    return rates


async def rate_on(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    tax_code_id: UUID,
    on_date: date,
) -> int:
    """Resolve one code's rate on the given date; see rates_on."""
    rates = await rates_on(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        tax_code_ids={tax_code_id},
        on_date=on_date,
    )
    return rates[tax_code_id]
