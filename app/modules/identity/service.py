"""Authentication, refresh-session and membership service (phase 0, day 3).

One command = one transaction: each public function commits its own unit of work
so a failed login can persist its lockout counter and audit event before the API
returns 401. Refresh tokens rotate on every use; reusing a revoked token revokes
the whole login family (report 21 §6).

RLS note: the API still connects as the migration owner during phase 0, so these
queries are not tenant-filtered by RLS yet; the membership filters below are the
authorisation boundary and day 4 adds the tenant-scoped data path.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.modules.identity import tokens, totp
from app.modules.identity.audit import record_audit
from app.modules.identity.models import (
    AppUser,
    AuthSession,
    Company,
    Membership,
    MembershipRole,
    MfaRecoveryCode,
    Role,
    RolePermission,
    Tenant,
)
from app.modules.identity.security import hash_password, verify_password
from app.shared.db import set_tenant
from app.shared.errors import AppError
from app.shared.ids import new_id
from app.shared.unit_of_work import UnitOfWork

# Verifying a hash for an unknown email keeps the failure path as slow as a real
# password check, so login timing does not reveal whether an email exists.
_DUMMY_PASSWORD_HASH = hash_password("timing-equalisation-dummy-password")
_CSRF_TOKEN_BYTES = 32
_RECOVERY_CODE_COUNT = 10


def _new_recovery_code() -> str:
    """One human-typable, high-entropy recovery code (shown once, stored hashed)."""
    return secrets.token_hex(4).upper()


def _normalise_recovery_code(value: str) -> str:
    return value.strip().replace("-", "").replace(" ", "").upper()


async def _delete_recovery_codes(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
) -> None:
    for row in (
        await session.execute(
            select(MfaRecoveryCode).where(
                MfaRecoveryCode.tenant_id == tenant_id,
                MfaRecoveryCode.user_id == user_id,
            )
        )
    ).scalars():
        await session.delete(row)


async def _issue_recovery_codes(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
) -> list[str]:
    """Replace every stored recovery code and return the new plaintext list."""
    await _delete_recovery_codes(session, tenant_id=tenant_id, user_id=user_id)
    codes = [_new_recovery_code() for _ in range(_RECOVERY_CODE_COUNT)]
    for code in codes:
        session.add(
            MfaRecoveryCode(
                tenant_id=tenant_id,
                user_id=user_id,
                code_hash=hash_password(_normalise_recovery_code(code)),
            )
        )
    return codes


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CompanyAccess:
    """One company a user may act in, with the roles and permissions it grants."""

    id: UUID
    code: str
    name_ar: str
    name_en: str | None
    base_currency: str
    role_codes: tuple[str, ...]
    permissions: frozenset[str]


@dataclass(frozen=True)
class Principal:
    """The authenticated user and everything the request may see."""

    user_id: UUID
    tenant_id: UUID
    email: str
    full_name: str
    companies: tuple[CompanyAccess, ...]
    mfa_enabled: bool = False
    mfa_sensitive_required: bool = True
    mfa_posting_required: bool = True

    @property
    def permissions(self) -> frozenset[str]:
        """Union of permissions across every company the user belongs to."""
        granted: set[str] = set()
        for company in self.companies:
            granted.update(company.permissions)
        return frozenset(granted)

    def company(self, company_id: UUID) -> CompanyAccess:
        """Return the requested company or raise 403 when it is not a membership."""
        for company in self.companies:
            if company.id == company_id:
                return company
        raise AppError(
            code="company_not_allowed",
            title="Forbidden",
            status_code=403,
            detail="The user is not a member of the requested company.",
        )


@dataclass(frozen=True)
class CompanyMember:
    """One active member of a company, with the role codes it holds there."""

    membership_id: UUID
    user_id: UUID
    email: str
    full_name: str
    role_codes: tuple[str, ...]


@dataclass(frozen=True)
class AuthResult:
    """The tokens and identity returned by login and refresh."""

    session_id: UUID
    user: AppUser
    tenant: Tenant
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    csrf_token: str


async def _find_login_user(
    session: AsyncSession,
    *,
    email: str,
    tenant_slug: str | None,
) -> AppUser | None:
    statement = (
        select(AppUser)
        .join(Tenant, Tenant.id == AppUser.tenant_id)
        .where(
            func.lower(AppUser.email) == email.strip().lower(),
            AppUser.is_active.is_(True),
            Tenant.status == "active",
        )
    )
    if tenant_slug is not None:
        statement = statement.where(Tenant.slug == tenant_slug)
    users = list((await session.execute(statement)).scalars().all())
    if len(users) > 1:
        raise AppError(
            code="tenant_required",
            title="Tenant required",
            status_code=400,
            detail="This email exists in more than one tenant; send the tenant slug.",
        )
    return users[0] if users else None


def _register_failed_attempt(user: AppUser, settings: Settings, now: datetime) -> dict[str, object]:
    """Count one failed sign-in step and apply the lockout backoff.

    The same counter guards the password and the second factor: a wrong TOTP
    code is a failed sign-in, not a harmless retry, so it must exhaust the
    account's attempt budget exactly like a wrong password does.
    """
    user.failed_login_count += 1
    detail: dict[str, object] = {"failed_attempts": user.failed_login_count}
    if user.failed_login_count >= settings.max_failed_logins:
        exponent = min(user.failed_login_count - settings.max_failed_logins, 10)
        backoff = min(
            timedelta(minutes=settings.lockout_minutes) * (2**exponent),
            timedelta(hours=1),
        )
        locked_until = now + backoff
        user.locked_until = locked_until
        detail["locked_until"] = locked_until.isoformat()
    return detail


async def _record_mfa_failure(
    session: AsyncSession,
    settings: Settings,
    *,
    user: AppUser,
    now: datetime,
    request_id: str | None,
    ip_address: str | None,
    detail: dict[str, object],
) -> NoReturn:
    """Persist a second-factor failure, count it toward lockout, and raise 401."""
    merged = {**_register_failed_attempt(user, settings, now), **detail}
    await record_audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="auth.mfa_failed",
        request_id=request_id,
        ip_address=ip_address,
        detail=merged,
    )
    await session.commit()
    raise AppError(
        code="totp_invalid",
        title="Invalid two-factor code",
        status_code=401,
        detail="The authenticator or recovery code is incorrect or was already used.",
    )


async def _consume_recovery_code(
    session: AsyncSession,
    *,
    user: AppUser,
    recovery_code: str,
) -> bool:
    """Consume one unused recovery code; the hash check is constant-time per row."""
    normalised = _normalise_recovery_code(recovery_code)
    if normalised == "":
        return False
    rows = (
        await session.execute(
            select(MfaRecoveryCode).where(
                MfaRecoveryCode.tenant_id == user.tenant_id,
                MfaRecoveryCode.user_id == user.id,
                MfaRecoveryCode.used_at.is_(None),
            )
        )
    ).scalars()
    for row in rows:
        if verify_password(normalised, row.code_hash):
            row.used_at = _now()
            return True
    return False


async def authenticate(
    session: AsyncSession,
    settings: Settings,
    *,
    email: str,
    password: str,
    tenant_slug: str | None = None,
    totp_code: str | None = None,
    recovery_code: str | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> tuple[AppUser, Tenant]:
    """Check credentials, second factor and lockout state, persisting every failure."""
    user = await _find_login_user(session, email=email, tenant_slug=tenant_slug)
    now = _now()

    if user is None:
        # The dummy verification keeps the response time indistinguishable from a
        # wrong password, but the code is distinct: the operator-assisted product
        # tells an installer whose email is unknown apart from a locked account.
        verify_password(password, _DUMMY_PASSWORD_HASH)
        raise AppError(
            code="unknown_email",
            title="Invalid credentials",
            status_code=401,
            detail="No active account uses this email in the selected tenant.",
        )

    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:  # pragma: no cover - the foreign key makes this unreachable
        raise AppError(
            code="invalid_credentials",
            title="Invalid credentials",
            status_code=401,
            detail="Email or password is incorrect.",
        )

    if user.locked_until is not None and user.locked_until > now:
        await record_audit(
            session,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            action="auth.login_locked",
            request_id=request_id,
            ip_address=ip_address,
            detail={"locked_until": user.locked_until.isoformat()},
        )
        await session.commit()
        raise AppError(
            code="account_locked",
            title="Account locked",
            status_code=429,
            detail="Too many failed sign-ins; try again later.",
        )

    if not verify_password(password, user.password_hash):
        detail = _register_failed_attempt(user, settings, now)
        await record_audit(
            session,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            action="auth.login_failed",
            request_id=request_id,
            ip_address=ip_address,
            detail=detail,
        )
        await session.commit()
        raise AppError(
            code="invalid_credentials",
            title="Invalid credentials",
            status_code=401,
            detail="Email or password is incorrect.",
        )

    if user.totp_enabled_at is not None:
        secret = user.totp_secret
        if secret is None:  # pragma: no cover - the enrolment sets both together
            raise AppError(
                code="totp_misconfigured",
                title="Two-factor unavailable",
                status_code=500,
                detail="This account has no stored TOTP secret.",
            )
        if totp_code is not None and totp_code.strip():
            counter = totp.match_counter(secret, totp_code)
            if counter is None or (
                user.totp_last_counter is not None and counter <= user.totp_last_counter
            ):
                await _record_mfa_failure(
                    session,
                    settings,
                    user=user,
                    now=now,
                    request_id=request_id,
                    ip_address=ip_address,
                    detail={"code": "totp_invalid"},
                )
            user.totp_last_counter = counter
        elif recovery_code is not None and recovery_code.strip():
            if not await _consume_recovery_code(session, user=user, recovery_code=recovery_code):
                await _record_mfa_failure(
                    session,
                    settings,
                    user=user,
                    now=now,
                    request_id=request_id,
                    ip_address=ip_address,
                    detail={"code": "recovery_invalid"},
                )
            await record_audit(
                session,
                tenant_id=user.tenant_id,
                actor_user_id=user.id,
                action="auth.mfa_recovery_used",
                request_id=request_id,
                ip_address=ip_address,
            )
        else:
            raise AppError(
                code="totp_required",
                title="Two-factor code required",
                status_code=401,
                detail="Enter the six-digit authenticator code or a recovery code.",
            )

    user.failed_login_count = 0
    user.locked_until = None
    return user, tenant


def _build_auth_session(
    settings: Settings,
    *,
    user: AppUser,
    tenant: Tenant,
    family_id: UUID,
    now: datetime,
    request_id: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> tuple[AuthSession, str, str, str, datetime]:
    """Build a refresh session and its access token without committing."""
    raw_refresh, refresh_hash = tokens.new_refresh_token()
    auth_session = AuthSession(
        id=new_id(),
        tenant_id=tenant.id,
        app_user_id=user.id,
        family_id=family_id,
        token_hash=refresh_hash,
        expires_at=tokens.refresh_token_expiry(settings, now),
        user_agent=user_agent,
        ip_address=ip_address,
    )
    access_token, access_expires_at = tokens.create_access_token(
        settings=settings,
        user_id=user.id,
        tenant_id=tenant.id,
        session_id=auth_session.id,
        now=now,
    )
    csrf_token = secrets.token_urlsafe(_CSRF_TOKEN_BYTES)
    _ = request_id  # kept for symmetry with the audit call at the call site
    return auth_session, raw_refresh, csrf_token, access_token, access_expires_at


async def login(
    session: AsyncSession,
    settings: Settings,
    *,
    email: str,
    password: str,
    tenant_slug: str | None = None,
    totp_code: str | None = None,
    recovery_code: str | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuthResult:
    """Authenticate and open a refresh session in the same transaction."""
    user, tenant = await authenticate(
        session,
        settings,
        email=email,
        password=password,
        tenant_slug=tenant_slug,
        totp_code=totp_code,
        recovery_code=recovery_code,
        request_id=request_id,
        ip_address=ip_address,
    )
    now = _now()
    auth_session, raw_refresh, csrf_token, access_token, access_expires_at = _build_auth_session(
        settings,
        user=user,
        tenant=tenant,
        family_id=new_id(),
        now=now,
        request_id=request_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.add(auth_session)
    await record_audit(
        session,
        tenant_id=tenant.id,
        actor_user_id=user.id,
        action="auth.login_succeeded",
        request_id=request_id,
        ip_address=ip_address,
        detail={"session_id": str(auth_session.id)},
    )
    await session.commit()
    return AuthResult(
        session_id=auth_session.id,
        user=user,
        tenant=tenant,
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token=raw_refresh,
        refresh_expires_at=auth_session.expires_at,
        csrf_token=csrf_token,
    )


async def refresh(
    session: AsyncSession,
    settings: Settings,
    *,
    refresh_token: str,
    request_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuthResult:
    """Rotate a refresh token, rejecting and revoking replayed tokens."""
    now = _now()
    token_hash = tokens.hash_refresh_token(refresh_token)
    # Lock the row: two concurrent refreshes of the same token must serialize,
    # otherwise both read "not revoked", both rotate, and one usable token is
    # silently lost (or a replay is mistaken for a rotation).
    auth_session = (
        await session.execute(
            select(AuthSession).where(AuthSession.token_hash == token_hash).with_for_update()
        )
    ).scalar_one_or_none()
    if auth_session is None:
        raise AppError(
            code="invalid_refresh_token",
            title="Unauthorized",
            status_code=401,
            detail="The refresh token is invalid.",
        )

    if auth_session.revoked_at is not None:
        # Replay of an already rotated/revoked token: treat the whole family as
        # compromised, not just this row.
        await session.execute(
            update(AuthSession)
            .where(
                AuthSession.family_id == auth_session.family_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await record_audit(
            session,
            tenant_id=auth_session.tenant_id,
            actor_user_id=auth_session.app_user_id,
            action="auth.refresh_reuse",
            request_id=request_id,
            ip_address=ip_address,
            detail={"family_id": str(auth_session.family_id)},
        )
        await session.commit()
        raise AppError(
            code="invalid_refresh_token",
            title="Unauthorized",
            status_code=401,
            detail="The refresh token is invalid.",
        )

    if auth_session.expires_at <= now:
        auth_session.revoked_at = now
        await record_audit(
            session,
            tenant_id=auth_session.tenant_id,
            actor_user_id=auth_session.app_user_id,
            action="auth.refresh_expired",
            request_id=request_id,
            ip_address=ip_address,
            detail={"family_id": str(auth_session.family_id)},
        )
        await session.commit()
        raise AppError(
            code="invalid_refresh_token",
            title="Unauthorized",
            status_code=401,
            detail="The refresh token is invalid.",
        )

    user = await session.get(AppUser, auth_session.app_user_id)
    tenant = await session.get(Tenant, auth_session.tenant_id)
    if (
        user is None
        or not user.is_active
        or tenant is None
        or tenant.status != "active"
        or user.tenant_id != tenant.id
    ):
        auth_session.revoked_at = now
        await session.commit()
        raise AppError(
            code="invalid_refresh_token",
            title="Unauthorized",
            status_code=401,
            detail="The refresh token is invalid.",
        )

    new_session, raw_refresh, csrf_token, access_token, access_expires_at = _build_auth_session(
        settings,
        user=user,
        tenant=tenant,
        family_id=auth_session.family_id,
        now=now,
        request_id=request_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    session.add(new_session)
    # Insert the successor first: replaced_by_session_id is a self-FK, so the
    # old row cannot point at a session that does not exist yet.
    await session.flush()
    auth_session.revoked_at = now
    auth_session.last_used_at = now
    auth_session.replaced_by_session_id = new_session.id
    await record_audit(
        session,
        tenant_id=tenant.id,
        actor_user_id=user.id,
        action="auth.refresh_rotated",
        request_id=request_id,
        ip_address=ip_address,
        detail={"family_id": str(new_session.family_id), "session_id": str(new_session.id)},
    )
    await session.commit()
    return AuthResult(
        session_id=new_session.id,
        user=user,
        tenant=tenant,
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token=raw_refresh,
        refresh_expires_at=new_session.expires_at,
        csrf_token=csrf_token,
    )


async def logout(
    session: AsyncSession,
    *,
    refresh_token: str,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> None:
    """Revoke the presented refresh session family; idempotent and non-enumerating."""
    token_hash = tokens.hash_refresh_token(refresh_token)
    auth_session = (
        await session.execute(select(AuthSession).where(AuthSession.token_hash == token_hash))
    ).scalar_one_or_none()
    if auth_session is not None:
        now = _now()
        # Logging out ends the whole rotation family, not just the presented
        # row: the access tokens minted by every rotation carry this family's
        # session ids and must stop working immediately.
        await session.execute(
            update(AuthSession)
            .where(
                AuthSession.family_id == auth_session.family_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await record_audit(
            session,
            tenant_id=auth_session.tenant_id,
            actor_user_id=auth_session.app_user_id,
            action="auth.logout",
            request_id=request_id,
            ip_address=ip_address,
            detail={"family_id": str(auth_session.family_id)},
        )
    await session.commit()


async def load_principal(
    session: AsyncSession,
    claims: tokens.AccessTokenClaims,
) -> Principal:
    """Load the authenticated user, their tenant and their company memberships."""
    user = await session.get(AppUser, claims.user_id)
    if user is None or not user.is_active or user.tenant_id != claims.tenant_id:
        raise AppError(
            code="not_authenticated",
            title="Unauthorized",
            status_code=401,
            detail="The access token is no longer valid.",
        )

    tenant = await session.get(Tenant, claims.tenant_id)
    if tenant is None or tenant.status != "active":
        raise AppError(
            code="not_authenticated",
            title="Unauthorized",
            status_code=401,
            detail="The access token is no longer valid.",
        )

    # The access token is only as good as the session behind it: logout,
    # rotation-replay revocation and administrator revocation all land on the
    # AuthSession row, so a signed token that outlives its session is still a
    # 401 rather than a 15-minute grace period.
    auth_session = await session.get(AuthSession, claims.session_id)
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= _now()
        or auth_session.app_user_id != user.id
        or auth_session.tenant_id != claims.tenant_id
    ):
        raise AppError(
            code="not_authenticated",
            title="Unauthorized",
            status_code=401,
            detail="The access token is no longer valid.",
        )

    # Bind the transaction for RLS as soon as the tenant is known.
    await set_tenant(session, claims.tenant_id)

    membership_rows = (
        await session.execute(
            select(Membership, Company)
            .join(Company, Company.id == Membership.company_id)
            .where(
                Membership.app_user_id == user.id,
                Membership.is_active.is_(True),
                Company.tenant_id == claims.tenant_id,
            )
            .order_by(Company.code)
        )
    ).all()

    membership_ids = [membership.id for membership, _company in membership_rows]
    roles_by_membership: dict[UUID, list[str]] = {}
    permissions_by_membership: dict[UUID, set[str]] = {}
    if membership_ids:
        for membership_id, role_code in (
            await session.execute(
                select(MembershipRole.membership_id, Role.code)
                .join(Role, Role.id == MembershipRole.role_id)
                .where(MembershipRole.membership_id.in_(membership_ids))
            )
        ).all():
            roles_by_membership.setdefault(membership_id, []).append(role_code)
        for membership_id, permission_code in (
            await session.execute(
                select(MembershipRole.membership_id, RolePermission.permission_code)
                .join(
                    RolePermission,
                    RolePermission.role_id == MembershipRole.role_id,
                )
                .where(MembershipRole.membership_id.in_(membership_ids))
            )
        ).all():
            permissions_by_membership.setdefault(membership_id, set()).add(permission_code)

    companies = tuple(
        CompanyAccess(
            id=company.id,
            code=company.code,
            name_ar=company.name_ar,
            name_en=company.name_en,
            base_currency=company.base_currency,
            role_codes=tuple(sorted(roles_by_membership.get(membership.id, []))),
            permissions=frozenset(permissions_by_membership.get(membership.id, set())),
        )
        for membership, company in membership_rows
    )
    return Principal(
        user_id=user.id,
        tenant_id=tenant.id,
        email=user.email,
        full_name=user.full_name,
        companies=companies,
        mfa_enabled=user.totp_enabled_at is not None,
        mfa_sensitive_required=tenant.mfa_sensitive_required,
        mfa_posting_required=tenant.mfa_posting_required,
    )


@dataclass(frozen=True)
class MfaPolicy:
    """The tenant-wide switch for each MFA obligation."""

    mfa_sensitive_required: bool
    mfa_posting_required: bool


async def read_mfa_policy(session: AsyncSession, *, tenant_id: UUID) -> MfaPolicy:
    """Read the tenant switches that drive MFA enforcement."""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise AppError(
            code="tenant_not_found",
            title="Not found",
            status_code=404,
            detail="The tenant no longer exists.",
        )
    return MfaPolicy(
        mfa_sensitive_required=tenant.mfa_sensitive_required,
        mfa_posting_required=tenant.mfa_posting_required,
    )


async def update_mfa_policy(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    mfa_sensitive_required: bool,
    mfa_posting_required: bool,
) -> MfaPolicy:
    """Persist the tenant MFA switches in one audited transaction."""

    async def operation(session: AsyncSession) -> MfaPolicy:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise AppError(
                code="tenant_not_found",
                title="Not found",
                status_code=404,
                detail="The tenant no longer exists.",
            )
        tenant.mfa_sensitive_required = mfa_sensitive_required
        tenant.mfa_posting_required = mfa_posting_required
        tenant.updated_by = actor_user_id
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="identity.tenant.mfa_policy_updated",
            entity_type="tenant",
            entity_id=tenant_id,
            detail={
                "mfa_sensitive_required": mfa_sensitive_required,
                "mfa_posting_required": mfa_posting_required,
            },
        )
        return MfaPolicy(
            mfa_sensitive_required=tenant.mfa_sensitive_required,
            mfa_posting_required=tenant.mfa_posting_required,
        )

    return await unit_of_work.run(operation, tenant_id=tenant_id)


async def list_company_members(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> list[CompanyMember]:
    """List the active members of one company inside the tenant."""
    rows = (
        await session.execute(
            select(Membership, AppUser)
            .join(AppUser, AppUser.id == Membership.app_user_id)
            .where(
                Membership.tenant_id == tenant_id,
                Membership.company_id == company_id,
                Membership.is_active.is_(True),
            )
            .order_by(AppUser.email)
        )
    ).all()
    membership_ids = [membership.id for membership, _user in rows]
    roles_by_membership: dict[UUID, list[str]] = {}
    if membership_ids:
        for membership_id, role_code in (
            await session.execute(
                select(MembershipRole.membership_id, Role.code)
                .join(Role, Role.id == MembershipRole.role_id)
                .where(MembershipRole.membership_id.in_(membership_ids))
            )
        ).all():
            roles_by_membership.setdefault(membership_id, []).append(role_code)
    return [
        CompanyMember(
            membership_id=membership.id,
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            role_codes=tuple(sorted(roles_by_membership.get(membership.id, []))),
        )
        for membership, user in rows
    ]


@dataclass(frozen=True)
class TotpEnrolment:
    """The one-time secret and provisioning URI shown during enrolment."""

    secret: str
    provisioning_uri: str


async def _require_user(session: AsyncSession, *, user_id: UUID) -> AppUser:
    user = await session.get(AppUser, user_id)
    if user is None:  # pragma: no cover - the access token always names a live user
        raise AppError(
            code="user_not_found",
            title="Not found",
            status_code=404,
            detail="The authenticated user no longer exists.",
        )
    return user


async def start_totp_enrolment(session: AsyncSession, *, user_id: UUID) -> TotpEnrolment:
    """Generate and stage a secret; it only becomes active after confirmation."""
    user = await _require_user(session, user_id=user_id)
    if user.totp_enabled_at is not None:
        raise AppError(
            code="totp_already_enabled",
            title="Conflict",
            status_code=409,
            detail="Two-factor authentication is already enabled for this account.",
        )
    secret = totp.generate_secret()
    user.totp_secret = secret
    user.totp_last_counter = None
    await record_audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="identity.totp.started",
    )
    await session.commit()
    return TotpEnrolment(
        secret=secret,
        provisioning_uri=totp.provisioning_uri(secret, account_name=user.email),
    )


async def confirm_totp_enrolment(session: AsyncSession, *, user_id: UUID, code: str) -> list[str]:
    """Activate the staged secret and return the first set of recovery codes."""
    user = await _require_user(session, user_id=user_id)
    if user.totp_enabled_at is not None:
        raise AppError(
            code="totp_already_enabled",
            title="Conflict",
            status_code=409,
            detail="Two-factor authentication is already enabled for this account.",
        )
    if user.totp_secret is None:
        raise AppError(
            code="totp_not_started",
            title="Enrolment not started",
            status_code=400,
            detail="Call the setup endpoint before confirming the code.",
        )
    counter = totp.match_counter(user.totp_secret, code)
    if counter is None:
        raise AppError(
            code="totp_invalid",
            title="Invalid two-factor code",
            status_code=422,
            detail="The authenticator code is incorrect.",
        )
    user.totp_enabled_at = _now()
    # Confirming the enrolment must not consume the code: the same window is
    # usually the user's first sign-in after enabling the second factor.
    user.totp_last_counter = None
    await record_audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="identity.totp.enrolled",
    )
    codes = await _issue_recovery_codes(session, tenant_id=user.tenant_id, user_id=user.id)
    await session.commit()
    return codes


async def regenerate_recovery_codes(
    session: AsyncSession,
    *,
    user_id: UUID,
    password: str,
    code: str,
) -> list[str]:
    """Replace the recovery codes after proving the password and a live TOTP code."""
    user = await _require_user(session, user_id=user_id)
    if user.totp_enabled_at is None or user.totp_secret is None:
        raise AppError(
            code="totp_not_enabled",
            title="Conflict",
            status_code=409,
            detail="Two-factor authentication is not enabled for this account.",
        )
    if user.password_hash is None or not verify_password(password, user.password_hash):
        raise AppError(
            code="invalid_credentials",
            title="Invalid credentials",
            status_code=401,
            detail="Password is incorrect.",
        )
    counter = totp.match_counter(user.totp_secret, code)
    if counter is None or (
        user.totp_last_counter is not None and counter <= user.totp_last_counter
    ):
        raise AppError(
            code="totp_invalid",
            title="Invalid two-factor code",
            status_code=401,
            detail="The authenticator code is incorrect or was already used.",
        )
    user.totp_last_counter = counter
    codes = await _issue_recovery_codes(session, tenant_id=user.tenant_id, user_id=user.id)
    await record_audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="identity.totp.recovery_codes_regenerated",
    )
    await session.commit()
    return codes


async def disable_totp(
    session: AsyncSession,
    *,
    user_id: UUID,
    password: str,
    code: str,
) -> None:
    """Remove the second factor after proving both the password and a live code."""
    user = await _require_user(session, user_id=user_id)
    if user.totp_enabled_at is None or user.totp_secret is None:
        raise AppError(
            code="totp_not_enabled",
            title="Conflict",
            status_code=409,
            detail="Two-factor authentication is not enabled for this account.",
        )
    if user.password_hash is None or not verify_password(password, user.password_hash):
        raise AppError(
            code="invalid_credentials",
            title="Invalid credentials",
            status_code=401,
            detail="Password is incorrect.",
        )
    if totp.match_counter(user.totp_secret, code) is None:
        raise AppError(
            code="totp_invalid",
            title="Invalid two-factor code",
            status_code=401,
            detail="The authenticator code is incorrect.",
        )
    user.totp_secret = None
    user.totp_enabled_at = None
    user.totp_last_counter = None
    await _delete_recovery_codes(session, tenant_id=user.tenant_id, user_id=user.id)
    await record_audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="identity.totp.disabled",
    )
    await session.commit()


async def list_user_sessions(session: AsyncSession, *, user_id: UUID) -> list[AuthSession]:
    """List this user's refresh sessions, newest first."""
    return list(
        (
            await session.execute(
                select(AuthSession)
                .where(AuthSession.app_user_id == user_id)
                .order_by(AuthSession.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def revoke_user_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> None:
    """Revoke one of the user's own live sessions."""
    auth_session = (
        await session.execute(
            select(AuthSession).where(
                AuthSession.id == session_id,
                AuthSession.app_user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if auth_session is None:
        raise AppError(
            code="session_not_found",
            title="Not found",
            status_code=404,
            detail="No such session for this user.",
        )
    if auth_session.revoked_at is None:
        auth_session.revoked_at = _now()
        await record_audit(
            session,
            tenant_id=auth_session.tenant_id,
            actor_user_id=user_id,
            action="identity.session.revoked",
            request_id=request_id,
            ip_address=ip_address,
            detail={"session_id": str(auth_session.id)},
        )
        await session.commit()
