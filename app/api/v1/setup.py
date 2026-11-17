"""Fresh-install provisioning endpoint (T-29, AC-10).

A new installation has no tenant and no account, so this endpoint is necessarily
unauthenticated; it is gated by the operator-held bootstrap token
(MAHMOUD_BOOTSTRAP_TOKEN) compared in constant time. Once the first tenant exists
the endpoint stops accepting requests here: further companies and users are
created through the authenticated admin API.
"""

from __future__ import annotations

import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from app.api.deps import SettingsDep
from app.config import Settings
from app.modules.identity import provisioning
from app.shared.errors import AppError

router = APIRouter(prefix="/setup", tags=["setup"])

BOOTSTRAP_HEADER = "X-Bootstrap-Token"


class SetupStatusOut(BaseModel):
    """Whether the installation still needs its first tenant."""

    needs_setup: bool
    bootstrap_configured: bool
    version: str


class SetupRequest(BaseModel):
    """Everything the installer supplies once, in one screen."""

    tenant_name: str = Field(min_length=2, max_length=200)
    tenant_slug: str = Field(min_length=2, max_length=63)
    company_code: str = Field(min_length=1, max_length=20)
    company_name_ar: str = Field(min_length=2, max_length=200)
    company_name_en: str | None = Field(default=None, max_length=200)
    base_currency: str = Field(default="USD", min_length=3, max_length=3)
    admin_email: str = Field(min_length=5, max_length=320)
    admin_full_name: str = Field(min_length=2, max_length=200)
    admin_password: str = Field(min_length=1, max_length=1024)
    require_mfa: bool = True
    fiscal_year: int | None = Field(default=None, ge=2000, le=2200)


class SetupResponse(BaseModel):
    """The identifiers the installer uses to sign in for the first time."""

    tenant_id: UUID
    tenant_slug: str
    company_id: UUID
    company_code: str
    user_id: UUID
    admin_email: str
    login_url: str


def _reject_without_bootstrap_token(settings: Settings, provided: str | None) -> None:
    configured = settings.bootstrap_token.get_secret_value() if settings.bootstrap_token else ""
    if configured == "":
        raise AppError(
            code="setup_not_available",
            title="Forbidden",
            status_code=403,
            detail="Fresh-install setup is disabled: MAHMOUD_BOOTSTRAP_TOKEN is not configured.",
        )
    if provided is None or not secrets.compare_digest(provided, configured):
        raise AppError(
            code="setup_token_invalid",
            title="Forbidden",
            status_code=403,
            detail="The bootstrap token is missing or incorrect.",
        )


@router.get("/status", response_model=SetupStatusOut, summary="Is this a fresh install?")
async def setup_status(settings: SettingsDep) -> SetupStatusOut:
    """Report whether the installation still needs provisioning."""
    return SetupStatusOut(
        needs_setup=await provisioning.needs_setup(settings),
        bootstrap_configured=bool(
            settings.bootstrap_token and settings.bootstrap_token.get_secret_value()
        ),
        version=settings.version,
    )


@router.post("", response_model=SetupResponse, status_code=201, summary="Provision a new install")
async def setup_install(
    body: SetupRequest,
    settings: SettingsDep,
    x_bootstrap_token: Annotated[str | None, Header(alias=BOOTSTRAP_HEADER)] = None,
) -> SetupResponse:
    """Create the first tenant, company and administrator of a fresh installation."""
    _reject_without_bootstrap_token(settings, x_bootstrap_token)
    if not await provisioning.needs_setup(settings):
        raise AppError(
            code="setup_completed",
            title="Conflict",
            status_code=409,
            detail="This installation already has a tenant; use the admin API for more.",
        )
    provisioned = await provisioning.provision_tenant(
        settings=settings,
        tenant_name=body.tenant_name,
        tenant_slug=body.tenant_slug,
        company_code=body.company_code,
        company_name_ar=body.company_name_ar,
        company_name_en=body.company_name_en,
        base_currency=body.base_currency,
        admin_email=body.admin_email,
        admin_full_name=body.admin_full_name,
        admin_password=body.admin_password,
        require_mfa=body.require_mfa,
        fiscal_year=body.fiscal_year,
    )
    return SetupResponse(
        tenant_id=provisioned.tenant_id,
        tenant_slug=provisioned.tenant_slug,
        company_id=provisioned.company_id,
        company_code=provisioned.company_code,
        user_id=provisioned.user_id,
        admin_email=provisioned.admin_email,
        login_url="/login",
    )
