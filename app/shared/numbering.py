"""Document numbering (phase 0, day 7).

Invoices and bills draw their numbers from the same locked per-company,
per-year sequence table the ledger uses for journal entries, so concurrent
creation cannot produce a duplicate or skip a number inside a transaction.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.errors import AppError


async def allocate_document_number(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    scope: str,
    period_key: str,
    prefix: str,
) -> str:
    """Atomically allocate the next `<prefix>-<period_key>-NNNNNN` number."""
    next_value = await session.scalar(
        text(
            """
            INSERT INTO number_sequence (tenant_id, company_id, scope, period_key, next_value)
            VALUES (:tenant_id, :company_id, :scope, :period_key, 1)
            ON CONFLICT ON CONSTRAINT uq_number_sequence_tenant_company_scope_period
            DO UPDATE SET next_value = number_sequence.next_value + 1, updated_at = now()
            RETURNING next_value
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "company_id": str(company_id),
            "scope": scope,
            "period_key": period_key,
        },
    )
    if next_value is None:
        raise AppError(
            code="document_number_conflict",
            title="Conflict",
            status_code=409,
            detail="Could not allocate a document number; retry the request.",
        )
    return f"{prefix}-{period_key}-{int(next_value):06d}"
