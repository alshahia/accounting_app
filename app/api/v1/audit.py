"""Audit-trail query endpoints (phase 0, day 4; report 21 §6).

Reading the tenant-wide trail requires `audit.view`; every authenticated user may
read their own login history. Both read the tenant-scoped, append-only `audit_log`
table that login, refresh, logout and future document events write to.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict

from app.api.deps import PrincipalDep, TenantSessionDep, require_permission
from app.modules.identity.audit import list_audit_events, list_login_history
from app.modules.identity.service import Principal

router = APIRouter(prefix="/audit", tags=["audit"])

# A spreadsheet treats a cell starting with one of these as a formula. The audit
# trail is operator-controlled data, but an exported file that executes string
# fields is still a delivery flaw (report 19 §9, T-29 sweep).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
FORMULA_GUARD = "'"


def _csv_text(value: str) -> str:
    """Prefix formula-looking text so a spreadsheet shows it verbatim."""
    if value.startswith(_FORMULA_PREFIXES):
        return FORMULA_GUARD + value
    return value


class AuditEvent(BaseModel):
    """One append-only audit record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor_user_id: UUID | None
    action: str
    entity_type: str | None
    entity_id: UUID | None
    detail: dict[str, Any] | None
    request_id: str | None
    ip_address: str | None
    created_at: datetime


class AuditList(BaseModel):
    """A page of audit records."""

    items: list[AuditEvent]
    limit: int
    offset: int


def _page(events: list[AuditEvent], limit: int, offset: int) -> AuditList:
    return AuditList(items=events, limit=limit, offset=offset)


@router.get("/events", response_model=AuditList, summary="Tenant audit trail")
async def read_events(
    session: TenantSessionDep,
    principal: Annotated[Principal, Depends(require_permission("audit.view"))],
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    actor_user_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditList:
    """Return the tenant's audit events, newest first, with optional filters."""
    events = await list_audit_events(
        session,
        tenant_id=principal.tenant_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor_user_id,
        limit=limit,
        offset=offset,
    )
    return _page([AuditEvent.model_validate(event) for event in events], limit, offset)


@router.get("/login-history", response_model=AuditList, summary="My login history")
async def read_login_history(
    session: TenantSessionDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditList:
    """Return the current user's own authentication events, newest first."""
    events = await list_login_history(
        session,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        limit=limit,
        offset=offset,
    )
    return _page([AuditEvent.model_validate(event) for event in events], limit, offset)


@router.get("/events.csv", response_class=Response, summary="Export tenant audit trail as CSV")
async def export_events(
    session: TenantSessionDep,
    principal: Annotated[Principal, Depends(require_permission("audit.view"))],
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    actor_user_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 10000,
) -> Response:
    """Export the filtered tenant audit trail as a UTF-8 CSV attachment."""
    events = await list_audit_events(
        session,
        tenant_id=principal.tenant_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_user_id=actor_user_id,
        limit=limit,
        offset=0,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "id",
            "created_at",
            "actor_user_id",
            "action",
            "entity_type",
            "entity_id",
            "request_id",
            "ip_address",
            "detail",
        ]
    )
    for event in events:
        writer.writerow(
            [
                str(event.id),
                event.created_at.isoformat(),
                str(event.actor_user_id) if event.actor_user_id else "",
                _csv_text(event.action),
                _csv_text(event.entity_type or ""),
                str(event.entity_id) if event.entity_id else "",
                _csv_text(event.request_id or ""),
                _csv_text(event.ip_address or ""),
                _csv_text(
                    json.dumps(event.detail, ensure_ascii=False, sort_keys=True)
                    if event.detail
                    else ""
                ),
            ]
        )
    return Response(
        content=buffer.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-trail.csv"'},
    )
