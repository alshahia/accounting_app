"""Authentication endpoints: login, refresh, logout and me (report 21 §5-§6).

Refresh and CSRF tokens travel as cookies; the access token is returned in the
body. Refresh and logout are protected by a double-submit CSRF token: the
readable cookie must match the X-CSRF-Token header, so a cross-site form post
cannot rotate or revoke a session with the ambient refresh cookie alone.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import (
    PrincipalDep,
    SessionDep,
    SettingsDep,
    TenantSessionDep,
    UnitOfWorkDep,
)
from app.config import Settings
from app.modules.identity import service
from app.modules.identity.service import AuthResult, Principal
from app.shared.context import get_request_id
from app.shared.errors import AppError
from app.shared.ratelimit import FixedWindowLimiter

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
COOKIE_PATH = "/api/v1/auth"


class LoginRequest(BaseModel):
    """Credentials; the tenant slug disambiguates an email used in two tenants."""

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    tenant: str | None = Field(default=None, max_length=100)
    totp_code: str | None = Field(default=None, max_length=10)
    recovery_code: str | None = Field(default=None, max_length=32)


class UserSummary(BaseModel):
    """The authenticated user."""

    id: UUID
    tenant_id: UUID
    email: str
    full_name: str
    mfa_enabled: bool


class CompanySummary(BaseModel):
    """A company the user may act in, with the roles and permissions it grants."""

    id: UUID
    code: str
    name_ar: str
    name_en: str | None
    base_currency: str
    role_codes: list[str]
    permissions: list[str]


class LoginResponse(BaseModel):
    """A fresh access token and the user it belongs to."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type label, not a secret
    expires_at: datetime
    user: UserSummary


class TenantMfaPolicyOut(BaseModel):
    """The tenant-wide MFA obligations; both default on."""

    mfa_sensitive_required: bool
    mfa_posting_required: bool


class TenantMfaPolicyUpdate(BaseModel):
    """A full replacement of the tenant MFA obligations."""

    mfa_sensitive_required: bool
    mfa_posting_required: bool


class MeResponse(BaseModel):
    """The current user and every company they may switch to."""

    user: UserSummary
    companies: list[CompanySummary]
    tenant_mfa_policy: TenantMfaPolicyOut


class MfaSetupResponse(BaseModel):
    """The staged TOTP secret and the URI an authenticator app scans."""

    secret: str
    provisioning_uri: str


class MfaCodeRequest(BaseModel):
    """A six-digit authenticator code."""

    code: str = Field(min_length=6, max_length=10)


class MfaDisableRequest(BaseModel):
    """The password and a live code, required for a sensitive MFA change."""

    password: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=6, max_length=10)


class MfaRecoveryCodesResponse(BaseModel):
    """Freshly issued single-use recovery codes, shown to the user exactly once."""

    codes: list[str]


class SessionOut(BaseModel):
    """One refresh session belonging to the current user."""

    id: UUID
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    revoked_at: datetime | None
    user_agent: str | None
    ip_address: str | None


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


# Sign-in steps that count against the per-address-and-account failure budget.
_COUNTED_LOGIN_FAILURES = frozenset(
    {"invalid_credentials", "unknown_email", "totp_invalid", "totp_required"}
)


def _login_limiter(request: Request) -> FixedWindowLimiter:
    limiter: FixedWindowLimiter = request.app.state.login_limiter
    return limiter


def _login_key(request: Request) -> str:
    """One bucket per client address: spreading guesses across accounts is still guessing."""
    return _client_ip(request) or "unknown"


def _reject_if_rate_limited(request: Request) -> None:
    if _login_limiter(request).blocked(_login_key(request)):
        raise AppError(
            code="rate_limited",
            title="Too many requests",
            status_code=429,
            detail="Too many failed sign-in attempts; try again shortly.",
        )


def _user_summary(
    user_id: UUID,
    tenant_id: UUID,
    email: str,
    full_name: str,
    *,
    mfa_enabled: bool,
) -> UserSummary:
    return UserSummary(
        id=user_id,
        tenant_id=tenant_id,
        email=email,
        full_name=full_name,
        mfa_enabled=mfa_enabled,
    )


def _set_auth_cookies(
    response: Response,
    settings: Settings,
    *,
    refresh_token: str,
    csrf_token: str,
) -> None:
    max_age = settings.refresh_token_days * 24 * 60 * 60
    secure = settings.cookies_are_secure
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path=COOKIE_PATH,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="strict",
        path=COOKIE_PATH,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
    response.delete_cookie(CSRF_COOKIE, path=COOKIE_PATH)


def _require_csrf(request: Request) -> None:
    header = request.headers.get(CSRF_HEADER)
    cookie = request.cookies.get(CSRF_COOKIE)
    if not header or not cookie or not secrets.compare_digest(header, cookie):
        raise AppError(
            code="csrf_failed",
            title="Forbidden",
            status_code=403,
            detail="The CSRF token is missing or invalid.",
        )


def _companies(principal: Principal) -> list[CompanySummary]:
    return [
        CompanySummary(
            id=company.id,
            code=company.code,
            name_ar=company.name_ar,
            name_en=company.name_en,
            base_currency=company.base_currency,
            role_codes=list(company.role_codes),
            permissions=sorted(company.permissions),
        )
        for company in principal.companies
    ]


def _login_response(result: AuthResult) -> LoginResponse:
    return LoginResponse(
        access_token=result.access_token,
        expires_at=result.access_expires_at,
        user=_user_summary(
            result.user.id,
            result.tenant.id,
            result.user.email,
            result.user.full_name,
            mfa_enabled=result.user.totp_enabled_at is not None,
        ),
    )


@router.post("/login", response_model=LoginResponse, summary="Sign in")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> LoginResponse:
    """Check credentials, open a refresh session and set the auth cookies."""
    _reject_if_rate_limited(request)
    try:
        result = await service.login(
            session,
            settings,
            email=payload.email,
            password=payload.password,
            tenant_slug=payload.tenant,
            totp_code=payload.totp_code,
            recovery_code=payload.recovery_code,
            request_id=get_request_id(),
            ip_address=_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
    except AppError as exc:
        if exc.code in _COUNTED_LOGIN_FAILURES:
            _login_limiter(request).hit(_login_key(request))
        raise
    _set_auth_cookies(
        response,
        settings,
        refresh_token=result.refresh_token,
        csrf_token=result.csrf_token,
    )
    return _login_response(result)


@router.post("/refresh", response_model=LoginResponse, summary="Rotate the session")
async def refresh(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> LoginResponse:
    """Rotate the refresh token; a replayed token revokes the whole family."""
    _require_csrf(request)
    raw_refresh = request.cookies.get(REFRESH_COOKIE)
    if not raw_refresh:
        raise AppError(
            code="invalid_refresh_token",
            title="Unauthorized",
            status_code=401,
            detail="The refresh token is missing.",
        )
    result = await service.refresh(
        session,
        settings,
        refresh_token=raw_refresh,
        request_id=get_request_id(),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    _set_auth_cookies(
        response,
        settings,
        refresh_token=result.refresh_token,
        csrf_token=result.csrf_token,
    )
    return _login_response(result)


@router.post("/logout", status_code=204, summary="Sign out")
async def logout(
    request: Request,
    response: Response,
    session: SessionDep,
) -> Response:
    """Revoke the presented refresh session and clear the cookies."""
    raw_refresh = request.cookies.get(REFRESH_COOKIE)
    if raw_refresh:
        _require_csrf(request)
        await service.logout(
            session,
            refresh_token=raw_refresh,
            request_id=get_request_id(),
            ip_address=_client_ip(request),
        )
    _clear_auth_cookies(response)
    response.status_code = 204
    return response


@router.post("/mfa/setup", response_model=MfaSetupResponse, summary="Start TOTP enrolment")
async def start_mfa(principal: PrincipalDep, session: TenantSessionDep) -> MfaSetupResponse:
    """Generate a secret for the authenticated user; confirm it to activate."""
    enrolment = await service.start_totp_enrolment(session, user_id=principal.user_id)
    return MfaSetupResponse(
        secret=enrolment.secret,
        provisioning_uri=enrolment.provisioning_uri,
    )


@router.post("/mfa/confirm", response_model=MfaRecoveryCodesResponse, summary="Activate TOTP")
async def confirm_mfa(
    payload: MfaCodeRequest,
    principal: PrincipalDep,
    session: TenantSessionDep,
) -> MfaRecoveryCodesResponse:
    """Activate the staged secret and return the one-time recovery codes."""
    codes = await service.confirm_totp_enrolment(
        session, user_id=principal.user_id, code=payload.code
    )
    return MfaRecoveryCodesResponse(codes=codes)


@router.post(
    "/mfa/recovery-codes",
    response_model=MfaRecoveryCodesResponse,
    summary="Regenerate recovery codes",
)
async def regenerate_recovery_codes(
    payload: MfaDisableRequest,
    principal: PrincipalDep,
    session: TenantSessionDep,
) -> MfaRecoveryCodesResponse:
    """Replace the recovery codes after proving the password and a live code."""
    codes = await service.regenerate_recovery_codes(
        session,
        user_id=principal.user_id,
        password=payload.password,
        code=payload.code,
    )
    return MfaRecoveryCodesResponse(codes=codes)


@router.post("/mfa/disable", status_code=204, summary="Remove TOTP")
async def disable_mfa(
    payload: MfaDisableRequest,
    principal: PrincipalDep,
    session: TenantSessionDep,
) -> Response:
    """Remove the second factor after proving the password and a live code."""
    await service.disable_totp(
        session,
        user_id=principal.user_id,
        password=payload.password,
        code=payload.code,
    )
    return Response(status_code=204)


@router.get("/sessions", response_model=list[SessionOut], summary="List my sessions")
async def list_sessions(principal: PrincipalDep, session: TenantSessionDep) -> list[SessionOut]:
    """Return the current user's refresh sessions, newest first."""
    rows = await service.list_user_sessions(session, user_id=principal.user_id)
    return [
        SessionOut(
            id=row.id,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            user_agent=row.user_agent,
            ip_address=row.ip_address,
        )
        for row in rows
    ]


@router.delete("/sessions/{session_id}", status_code=204, summary="Revoke one session")
async def revoke_session(
    session_id: UUID,
    request: Request,
    principal: PrincipalDep,
    session: TenantSessionDep,
) -> Response:
    """Revoke one of the current user's sessions, e.g. a lost device."""
    await service.revoke_user_session(
        session,
        user_id=principal.user_id,
        session_id=session_id,
        request_id=get_request_id(),
        ip_address=_client_ip(request),
    )
    return Response(status_code=204)


@router.get("/me", response_model=MeResponse, summary="Current user and companies")
async def me(principal: PrincipalDep) -> MeResponse:
    """Return the authenticated user and only the companies they belong to."""
    return MeResponse(
        user=_user_summary(
            principal.user_id,
            principal.tenant_id,
            principal.email,
            principal.full_name,
            mfa_enabled=principal.mfa_enabled,
        ),
        companies=_companies(principal),
        tenant_mfa_policy=TenantMfaPolicyOut(
            mfa_sensitive_required=principal.mfa_sensitive_required,
            mfa_posting_required=principal.mfa_posting_required,
        ),
    )


@router.get(
    "/tenant-settings/mfa-policy",
    response_model=TenantMfaPolicyOut,
    summary="Read the tenant MFA policy",
)
async def read_tenant_mfa_policy(
    principal: PrincipalDep,
    session: TenantSessionDep,
) -> TenantMfaPolicyOut:
    """Return the switches that decide when a second factor is required."""
    policy = await service.read_mfa_policy(session, tenant_id=principal.tenant_id)
    return TenantMfaPolicyOut(
        mfa_sensitive_required=policy.mfa_sensitive_required,
        mfa_posting_required=policy.mfa_posting_required,
    )


@router.put(
    "/tenant-settings/mfa-policy",
    response_model=TenantMfaPolicyOut,
    summary="Update the tenant MFA policy",
)
async def update_tenant_mfa_policy(
    payload: TenantMfaPolicyUpdate,
    principal: PrincipalDep,
    unit_of_work: UnitOfWorkDep,
) -> TenantMfaPolicyOut:
    """Replace the switches; changing security policy is audited."""
    if "admin.role.manage" not in principal.permissions:
        raise AppError(
            code="permission_denied",
            title="Forbidden",
            status_code=403,
            detail="The permission 'admin.role.manage' is required to change MFA policy.",
        )
    policy = await service.update_mfa_policy(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        mfa_sensitive_required=payload.mfa_sensitive_required,
        mfa_posting_required=payload.mfa_posting_required,
    )
    return TenantMfaPolicyOut(
        mfa_sensitive_required=policy.mfa_sensitive_required,
        mfa_posting_required=policy.mfa_posting_required,
    )
