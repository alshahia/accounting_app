"""Tenant administration: users, memberships, roles and companies (T-29, AC-10).

These endpoints are the onboarding path for a real installation: the demo seed
is a development fixture only. Every mutation is permission-checked, run inside
a tenant-bound transaction (so forced row-level security applies) and audited.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    PrincipalDep,
    TenantSessionDep,
    UnitOfWorkDep,
    require_company_permission,
    require_permission,
)
from app.modules.identity import provisioning
from app.modules.identity.models import Role, RolePermission
from app.modules.identity.service import CompanyAccess, Principal, list_company_members
from app.shared.context import get_request_id

router = APIRouter(prefix="/admin", tags=["admin"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _request_id() -> str | None:
    return get_request_id()


class CompanyMemberOut(BaseModel):
    """One member of the selected company."""

    membership_id: UUID
    user_id: UUID
    email: str
    full_name: str
    role_codes: list[str]


class RoleOut(BaseModel):
    """One role of the tenant with the number of permissions it grants."""

    code: str
    name: str
    is_system: bool
    permission_count: int


class CompanyAssignmentOut(BaseModel):
    """One membership of a user."""

    company_id: UUID
    role_codes: list[str]


class TenantUserOut(BaseModel):
    """One tenant user and its memberships."""

    user_id: UUID
    email: str
    full_name: str
    is_active: bool
    companies: list[CompanyAssignmentOut]


class CreateUserRequest(BaseModel):
    """A new user; without a password the response carries a one-time invite link."""

    email: str = Field(min_length=5, max_length=320)
    full_name: str = Field(min_length=2, max_length=200)
    company_id: UUID
    role_codes: list[str] = Field(min_length=1)
    password: str | None = Field(default=None, min_length=1, max_length=1024)


class CreateUserResponse(BaseModel):
    """The created user plus the one-time invitation link, when one was issued."""

    user: TenantUserOut
    invitation_token: str | None = None
    invitation_expires_at: datetime | None = None


class ResetLinkResponse(BaseModel):
    """A one-time password-reset link."""

    user_id: UUID
    reset_token: str
    expires_at: datetime


class SetActiveRequest(BaseModel):
    """Activate or deactivate one user."""

    is_active: bool


class UserActiveOut(BaseModel):
    """The resulting activation state of one user."""

    user_id: UUID
    is_active: bool


class CreateCompanyRequest(BaseModel):
    """A second company of the tenant, with its own chart of accounts."""

    code: str = Field(min_length=1, max_length=20)
    name_ar: str = Field(min_length=2, max_length=200)
    name_en: str | None = Field(default=None, max_length=200)
    base_currency: str = Field(default="USD", min_length=3, max_length=3)
    fiscal_year: int | None = Field(default=None, ge=2000, le=2200)


class CreateCompanyResponse(BaseModel):
    """The created company."""

    company_id: UUID
    code: str
    base_currency: str


def _user_out(user: provisioning.TenantUser) -> TenantUserOut:
    return TenantUserOut(
        user_id=user.user_id,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        companies=[
            CompanyAssignmentOut(company_id=item.company_id, role_codes=list(item.role_codes))
            for item in user.companies
        ],
    )


@router.get("/members", response_model=list[CompanyMemberOut], summary="Company members")
async def read_members(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[
        CompanyAccess,
        Depends(require_company_permission("admin.user.manage")),
    ],
) -> list[CompanyMemberOut]:
    """List the active members of the selected company."""
    members = await list_company_members(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
    )
    return [
        CompanyMemberOut(
            membership_id=member.membership_id,
            user_id=member.user_id,
            email=member.email,
            full_name=member.full_name,
            role_codes=list(member.role_codes),
        )
        for member in members
    ]


@router.get("/roles", response_model=list[RoleOut], summary="Tenant roles")
async def read_roles(
    session: TenantSessionDep,
    principal: Annotated[Principal, Depends(require_permission("admin.role.manage"))],
) -> list[RoleOut]:
    """List the roles defined in this tenant."""
    rows = (
        await session.execute(
            select(Role, func.count(RolePermission.permission_code))
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .where(Role.tenant_id == principal.tenant_id)
            .group_by(Role.id)
            .order_by(Role.code)
        )
    ).all()
    return [
        RoleOut(
            code=role.code,
            name=role.name,
            is_system=role.is_system,
            permission_count=int(count),
        )
        for role, count in rows
    ]


@router.get("/users", response_model=list[TenantUserOut], summary="Tenant users")
async def read_users(
    session: TenantSessionDep,
    principal: Annotated[Principal, Depends(require_permission("admin.user.manage"))],
) -> list[TenantUserOut]:
    """List every user of the tenant with the companies and roles it holds."""
    users = await provisioning.list_tenant_users(session, tenant_id=principal.tenant_id)
    return [_user_out(user) for user in users]


@router.post(
    "/users",
    response_model=CreateUserResponse,
    status_code=201,
    summary="Create a user and assign roles",
)
async def create_user(
    request: Request,
    body: CreateUserRequest,
    unit_of_work: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(require_permission("admin.user.manage"))],
) -> CreateUserResponse:
    """Create a user, membership and roles; issue a one-time link when no password is set."""

    async def operation(
        session: AsyncSession,
    ) -> tuple[provisioning.TenantUser, provisioning.CredentialLink | None]:
        return await provisioning.create_tenant_user(
            session,
            request.app.state.settings,
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            company_id=body.company_id,
            email=body.email,
            full_name=body.full_name,
            role_codes=tuple(body.role_codes),
            password=body.password,
            request_id=_request_id(),
            ip_address=_client_ip(request),
        )

    user, link = await unit_of_work.run(operation, tenant_id=principal.tenant_id)
    return CreateUserResponse(
        user=_user_out(user),
        invitation_token=link.token if link is not None else None,
        invitation_expires_at=link.expires_at if link is not None else None,
    )


@router.post(
    "/users/{user_id}/password-reset",
    response_model=ResetLinkResponse,
    status_code=201,
    summary="Issue a one-time password-reset link",
)
async def issue_password_reset(
    request: Request,
    user_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(require_permission("admin.user.manage"))],
) -> ResetLinkResponse:
    """Issue a reset link for one user; the link is returned once and never stored."""

    async def operation(session: AsyncSession) -> tuple[str, datetime]:
        link = await provisioning.issue_reset_link(
            session,
            request.app.state.settings,
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            user_id=user_id,
            request_id=_request_id(),
            ip_address=_client_ip(request),
        )
        return link.token, link.expires_at

    token, expires_at = await unit_of_work.run(operation, tenant_id=principal.tenant_id)
    return ResetLinkResponse(user_id=user_id, reset_token=token, expires_at=expires_at)


@router.patch(
    "/users/{user_id}",
    response_model=UserActiveOut,
    summary="Activate or deactivate a user",
)
async def set_user_active(
    request: Request,
    user_id: UUID,
    body: SetActiveRequest,
    unit_of_work: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(require_permission("admin.user.manage"))],
) -> UserActiveOut:
    """Activate or deactivate a user, revoking every session when deactivated."""

    async def operation(session: AsyncSession) -> UUID:
        user = await provisioning.set_user_active(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            user_id=user_id,
            is_active=body.is_active,
            request_id=_request_id(),
        )
        return user.id

    await unit_of_work.run(operation, tenant_id=principal.tenant_id)
    return UserActiveOut(user_id=user_id, is_active=body.is_active)


@router.post(
    "/companies",
    response_model=CreateCompanyResponse,
    status_code=201,
    summary="Create a company with its chart and fiscal calendar",
)
async def create_company(
    request: Request,
    body: CreateCompanyRequest,
    unit_of_work: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(require_permission("admin.company.manage"))],
) -> CreateCompanyResponse:
    """Create a second (or later) company of the tenant."""

    async def operation(session: AsyncSession) -> tuple[UUID, str, str]:
        company = await provisioning.create_company(
            session,
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            code=body.code,
            name_ar=body.name_ar,
            name_en=body.name_en,
            base_currency=body.base_currency,
            fiscal_year=body.fiscal_year,
            request_id=_request_id(),
            ip_address=_client_ip(request),
        )
        return company.id, company.code, company.base_currency

    company_id, code, currency = await unit_of_work.run(operation, tenant_id=principal.tenant_id)
    return CreateCompanyResponse(company_id=company_id, code=code, base_currency=currency)
