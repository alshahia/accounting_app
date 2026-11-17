"""Audit-trail writes (report 21 §6).

Day 3 records authentication events; day 4 adds the query endpoint and the
generic document/permission/period events. Append-only: nothing here updates or
deletes an audit row.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import AuditLog


async def record_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    action: str,
    actor_user_id: UUID | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    detail: dict[str, object] | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> None:
    """Append one audit event to the current transaction."""
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail,
            request_id=request_id,
            ip_address=ip_address,
        )
    )
    await session.flush()


async def list_audit_events(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    actor_user_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[AuditLog]:
    """Return tenant audit events, newest first, with optional filters."""
    statement = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if action is not None:
        statement = statement.where(AuditLog.action == action)
    if entity_type is not None:
        statement = statement.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        statement = statement.where(AuditLog.entity_id == entity_id)
    if actor_user_id is not None:
        statement = statement.where(AuditLog.actor_user_id == actor_user_id)
    statement = (
        statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(statement)).scalars().all())


async def list_login_history(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[AuditLog]:
    """Return one user's own authentication events, newest first."""
    statement = (
        select(AuditLog)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.actor_user_id == actor_user_id,
            AuditLog.action.like("auth.%"),
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(statement)).scalars().all())
