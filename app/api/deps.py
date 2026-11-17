"""FastAPI dependencies: session, settings and the authenticated principal."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.modules.identity.mfa_policy import mfa_required
from app.modules.identity.service import CompanyAccess, Principal, load_principal
from app.modules.identity.tokens import TokenError, decode_access_token
from app.shared.db import Database, set_database_role, set_tenant
from app.shared.errors import AppError
from app.shared.unit_of_work import UnitOfWork


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Open one session per request; services own their commits."""
    database = cast(Database, request.app.state.database)
    async with database.session_factory() as session:
        yield session


def app_settings(request: Request) -> Settings:
    """Return the settings the application was built with."""
    return cast(Settings, request.app.state.settings)


def get_unit_of_work(request: Request) -> UnitOfWork:
    """Return a unit of work bound to the application's database.

    Write transactions run as the restricted database role so forced row-level
    security applies to the API path, not just to raw SQL in tests.
    """
    database = cast(Database, request.app.state.database)
    return UnitOfWork(database.session_factory, set_role=database.set_role)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(app_settings)]
UnitOfWorkDep = Annotated[UnitOfWork, Depends(get_unit_of_work)]


async def get_principal(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> Principal:
    """Validate the bearer token and load the user's memberships."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AppError(
            code="not_authenticated",
            title="Unauthorized",
            status_code=401,
            detail="A bearer access token is required.",
        )
    try:
        claims = decode_access_token(token, settings)
    except TokenError as exc:
        raise AppError(
            code="not_authenticated",
            title="Unauthorized",
            status_code=401,
            detail="The access token is invalid or expired.",
        ) from exc
    return await load_principal(session, claims)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def get_tenant_session(
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
) -> AsyncIterator[AsyncSession]:
    """Bind the authenticated tenant and drop to the restricted role for reads.

    The authentication dependency runs before this one on the owner connection,
    because resolving a user crosses tenants. Every business read that runs
    after it sees only the principal's tenant even if a service filter is
    forgotten.
    """
    await set_tenant(session, principal.tenant_id)
    database = cast(Database, request.app.state.database)
    if database.set_role is not None:
        await set_database_role(session, database.set_role)
    yield session


TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]


async def get_company(
    principal: PrincipalDep,
    x_company_id: Annotated[str | None, Header(alias="X-Company-Id")] = None,
) -> CompanyAccess:
    """Resolve the acting company from X-Company-Id, denying non-members."""
    if not x_company_id:
        raise AppError(
            code="company_required",
            title="Bad request",
            status_code=400,
            detail="The X-Company-Id header is required to select the acting company.",
        )
    try:
        company_id = UUID(x_company_id)
    except ValueError as exc:
        raise AppError(
            code="invalid_company",
            title="Bad request",
            status_code=400,
            detail="The X-Company-Id header is not a valid company id.",
        ) from exc
    return principal.company(company_id)


CompanyDep = Annotated[CompanyAccess, Depends(get_company)]


def require_permission(permission: str) -> Callable[[Principal], Awaitable[Principal]]:
    """Deny by default unless the principal holds the permission anywhere."""

    async def dependency(principal: PrincipalDep) -> Principal:
        if permission not in principal.permissions:
            raise AppError(
                code="permission_denied",
                title="Forbidden",
                status_code=403,
                detail=f"The permission '{permission}' is required for this action.",
            )
        return principal

    return dependency


def require_company_permission(
    permission: str,
) -> Callable[..., Awaitable[CompanyAccess]]:
    """Deny unless the selected company's roles grant the permission."""

    async def dependency(company: CompanyDep, principal: PrincipalDep) -> CompanyAccess:
        if permission not in company.permissions:
            raise AppError(
                code="permission_denied",
                title="Forbidden",
                status_code=403,
                detail=f"The permission '{permission}' is required in the selected company.",
            )
        if mfa_required(
            permission,
            mfa_enabled=principal.mfa_enabled,
            mfa_sensitive_required=principal.mfa_sensitive_required,
            mfa_posting_required=principal.mfa_posting_required,
        ):
            raise AppError(
                code="mfa_required",
                title="Forbidden",
                status_code=403,
                detail=(
                    "Two-factor authentication must be enabled before this action; "
                    "enrol from the security settings and sign in again."
                ),
            )
        return company

    return dependency
