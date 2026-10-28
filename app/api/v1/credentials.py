"""One-time credential links and self-service password change (T-29).

Invitation links are handed to the administrator once when a user is created
without a password; password-reset links replace a forgotten one. Both are
signed tokens carrying the user, tenant, purpose and credential epoch, so
accepting one changes the password and retires every older link without storing
token rows in the database.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PrincipalDep, SettingsDep, UnitOfWorkDep
from app.modules.identity import provisioning
from app.modules.identity.tokens import TokenError, decode_credential_token
from app.shared.context import get_request_id
from app.shared.errors import AppError

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


class AcceptInvitationRequest(BaseModel):
    """The one-time link and the password the user chooses."""

    token: str = Field(min_length=10, max_length=4096)
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    """The current password proving the session, and the replacement."""

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


@router.post(
    "/accept-invitation",
    status_code=204,
    summary="Accept an invitation or password-reset link",
)
async def accept_invitation(
    request: Request,
    body: AcceptInvitationRequest,
    settings: SettingsDep,
    unit_of_work: UnitOfWorkDep,
) -> Response:
    """Set the password from a valid one-time link; every older link stops working."""
    try:
        claims = decode_credential_token(body.token, settings)
    except TokenError as exc:
        raise AppError(
            code="credential_invalid",
            title="Unauthorized",
            status_code=401,
            detail="The link is invalid or has expired; ask an administrator for a new one.",
        ) from exc

    async def operation(session: AsyncSession) -> None:
        await provisioning.accept_credential_link(
            session,
            settings,
            claims_user_id=claims.user_id,
            claims_tenant_id=claims.tenant_id,
            purpose=claims.purpose,
            epoch=claims.epoch,
            new_password=body.password,
            request_id=get_request_id(),
            ip_address=_client_ip(request),
        )

    await unit_of_work.run(operation, tenant_id=claims.tenant_id)
    return Response(status_code=204)


@router.post(
    "/change-password",
    status_code=204,
    summary="Change the signed-in user's password",
)
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
) -> Response:
    """Change the password and revoke every session, including this one."""

    async def operation(session: AsyncSession) -> None:
        await provisioning.change_own_password(
            session,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            current_password=body.current_password,
            new_password=body.new_password,
            request_id=get_request_id(),
            ip_address=_client_ip(request),
        )

    await unit_of_work.run(operation, tenant_id=principal.tenant_id)
    return Response(status_code=204)
