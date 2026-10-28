"""Operator-assisted provisioning: the product, not a seed script (T-29, AC-10).

A fresh install has no tenant, so the forced row-level-security policies have
nothing to bind to and no authenticated principal exists yet. The setup endpoint
therefore opens the privileged DSN for exactly one operation: create the first
tenant, its first company, the chart of accounts, the fiscal calendar and the
administrator account. The token that gates it is held by the operator.

Everything after that first tenant is ordinary tenant-scoped administration:
additional companies, users, memberships and role assignments go through the
admin API and are audited like any other business change.

Invitation and password-reset links are signed single-use tokens; the user's
credential epoch makes an old link stop verifying as soon as a password changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.modules.identity.audit import record_audit
from app.modules.identity.models import (
    AppUser,
    AuthSession,
    Company,
    Membership,
    MembershipRole,
    Role,
    Tenant,
)
from app.modules.identity.security import (
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.modules.identity.seeding import seed_permissions, seed_tenant_roles
from app.modules.identity.tokens import (
    INVITE_PURPOSE,
    RESET_PURPOSE,
    create_credential_token,
)
from app.modules.ledger.models import AccountingPeriod, FiscalYear
from app.modules.ledger.seeding import seed_company_chart
from app.shared.errors import AppError

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,19}$")
ADMIN_ROLE_CODE = "tenant_admin"


def _unprocessable(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Unprocessable entity", status_code=422, detail=detail)


def _conflict(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Conflict", status_code=409, detail=detail)


def _not_found(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Not found", status_code=404, detail=detail)


@dataclass(frozen=True)
class ProvisionedTenant:
    """The identifiers a completed setup returns to the installer."""

    tenant_id: UUID
    tenant_slug: str
    company_id: UUID
    company_code: str
    user_id: UUID
    admin_email: str


@dataclass(frozen=True)
class TenantUser:
    """One user of the tenant with the companies and roles it holds."""

    user_id: UUID
    email: str
    full_name: str
    is_active: bool
    companies: tuple[CompanyAssignment, ...]


@dataclass(frozen=True)
class CompanyAssignment:
    """One membership: the company and the role codes carried there."""

    company_id: UUID
    role_codes: tuple[str, ...]


@dataclass(frozen=True)
class CredentialLink:
    """A one-time link returned exactly once to the administrator."""

    token: str
    expires_at: datetime
    purpose: str


def _engine(settings: Settings) -> AsyncEngine:
    """One short-lived privileged engine; setup runs at most once per install."""
    return create_async_engine(settings.provisioning_url, pool_pre_ping=True)


async def _count_tenants(settings: Settings) -> int:
    """Count tenants on the privileged connection (forced RLS hides them otherwise)."""
    engine = create_async_engine(settings.provisioning_url)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(select(func.count()).select_from(Tenant))
            return int(value or 0)
    finally:
        await engine.dispose()


async def needs_setup(settings: Settings) -> bool:
    """True when the installation has no tenant yet."""
    return await _count_tenants(settings) == 0


async def create_fiscal_year(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    year: int,
) -> FiscalYear:
    """Create one fiscal year and its twelve monthly periods."""
    fiscal_year = FiscalYear(
        tenant_id=tenant_id,
        company_id=company_id,
        code=f"FY{year}",
        name_ar=f"السنة المالية {year}",
        start_date=date(year, 1, 1),
        end_date=date(year, 12, 31),
    )
    session.add(fiscal_year)
    await session.flush()
    for month in range(1, 13):
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
        session.add(
            AccountingPeriod(
                tenant_id=tenant_id,
                company_id=company_id,
                fiscal_year_id=fiscal_year.id,
                number=month,
                start_date=start,
                end_date=end,
            )
        )
    await session.flush()
    return fiscal_year


def validate_slug(value: str) -> str:
    """Return a normalised tenant slug or raise a 422."""
    slug = value.strip().lower()
    if SLUG_PATTERN.fullmatch(slug) is None:
        raise _unprocessable(
            "tenant_slug_invalid",
            "The tenant slug must be 2-63 lowercase letters, digits or hyphens.",
        )
    return slug


def validate_company_code(value: str) -> str:
    """Return a normalised company code or raise a 422."""
    code = value.strip().upper()
    if CODE_PATTERN.fullmatch(code) is None:
        raise _unprocessable(
            "company_code_invalid",
            "The company code must be 1-20 uppercase letters, digits or hyphens.",
        )
    return code


def validate_currency(value: str) -> str:
    """Return a normalised ISO-4217 currency code or raise a 422."""
    currency = value.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise _unprocessable("currency_invalid", "The base currency must be a 3-letter ISO code.")
    return currency


def validate_email(value: str) -> str:
    """Return a normalised email address or raise a 422."""
    email = value.strip().lower()
    local, _, domain = email.partition("@")
    if not local or "." not in domain or domain.startswith(".") or email.endswith("."):
        raise _unprocessable("email_invalid", "The email address is not valid.")
    return email


async def provision_tenant(
    *,
    settings: Settings,
    tenant_name: str,
    tenant_slug: str,
    company_code: str,
    company_name_ar: str,
    company_name_en: str | None,
    base_currency: str,
    admin_email: str,
    admin_full_name: str,
    admin_password: str,
    require_mfa: bool = True,
    fiscal_year: int | None = None,
) -> ProvisionedTenant:
    """Create the first tenant, company, calendar and administrator in one transaction."""
    slug = validate_slug(tenant_slug)
    code = validate_company_code(company_code)
    currency = validate_currency(base_currency)
    email = validate_email(admin_email)
    if not tenant_name.strip():
        raise _unprocessable("tenant_name_invalid", "The tenant name is required.")
    if not company_name_ar.strip():
        raise _unprocessable("company_name_invalid", "The Arabic company name is required.")
    if not admin_full_name.strip():
        raise _unprocessable("admin_name_invalid", "The administrator name is required.")
    validate_password_strength(admin_password)
    year = fiscal_year if fiscal_year is not None else datetime.now(UTC).date().year

    engine = _engine(settings)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await seed_permissions(session)
            taken = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
            if taken is not None:
                raise _conflict("tenant_slug_taken", f"The tenant slug {slug!r} is taken.")
            tenant = Tenant(
                name=tenant_name.strip(),
                slug=slug,
                mfa_sensitive_required=require_mfa,
                mfa_posting_required=require_mfa,
            )
            session.add(tenant)
            await session.flush()
            company = Company(
                tenant_id=tenant.id,
                code=code,
                name_ar=company_name_ar.strip(),
                name_en=company_name_en.strip() if company_name_en else None,
                base_currency=currency,
            )
            user = AppUser(
                tenant_id=tenant.id,
                email=email,
                full_name=admin_full_name.strip(),
                password_hash=hash_password(admin_password),
            )
            session.add_all([company, user])
            await session.flush()
            roles = await seed_tenant_roles(session, tenant.id)
            membership = Membership(
                tenant_id=tenant.id,
                app_user_id=user.id,
                company_id=company.id,
            )
            session.add(membership)
            await session.flush()
            session.add(
                MembershipRole(
                    tenant_id=tenant.id,
                    membership_id=membership.id,
                    role_id=roles[ADMIN_ROLE_CODE].id,
                )
            )
            await seed_company_chart(session, tenant_id=tenant.id, company_id=company.id)
            await create_fiscal_year(
                session,
                tenant_id=tenant.id,
                company_id=company.id,
                year=year,
            )
            await record_audit(
                session,
                tenant_id=tenant.id,
                actor_user_id=user.id,
                action="setup.tenant_provisioned",
                entity_type="tenant",
                entity_id=tenant.id,
                detail={
                    "slug": slug,
                    "company_code": code,
                    "base_currency": currency,
                    "mfa_required": require_mfa,
                    "fiscal_year": year,
                },
            )
        return ProvisionedTenant(
            tenant_id=tenant.id,
            tenant_slug=slug,
            company_id=company.id,
            company_code=code,
            user_id=user.id,
            admin_email=email,
        )
    finally:
        await engine.dispose()


async def create_company(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    code: str,
    name_ar: str,
    name_en: str | None,
    base_currency: str,
    fiscal_year: int | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> Company:
    """Create one company with its chart of accounts and fiscal calendar."""
    normalized = validate_company_code(code)
    currency = validate_currency(base_currency)
    if not name_ar.strip():
        raise _unprocessable("company_name_invalid", "The Arabic company name is required.")
    existing = await session.scalar(
        select(Company.id).where(Company.tenant_id == tenant_id, Company.code == normalized)
    )
    if existing is not None:
        raise _conflict("company_code_taken", f"The company code {normalized!r} is taken.")
    company = Company(
        tenant_id=tenant_id,
        code=normalized,
        name_ar=name_ar.strip(),
        name_en=name_en.strip() if name_en else None,
        base_currency=currency,
        created_by=actor_user_id,
        updated_by=actor_user_id,
    )
    session.add(company)
    await session.flush()
    await seed_company_chart(session, tenant_id=tenant_id, company_id=company.id)
    await create_fiscal_year(
        session,
        tenant_id=tenant_id,
        company_id=company.id,
        year=fiscal_year if fiscal_year is not None else datetime.now(UTC).date().year,
    )
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="admin.company.created",
        entity_type="company",
        entity_id=company.id,
        detail={"code": normalized, "base_currency": currency},
        request_id=request_id,
        ip_address=ip_address,
    )
    return company


async def create_tenant_user(
    session: AsyncSession,
    settings: Settings,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    email: str,
    full_name: str,
    role_codes: tuple[str, ...],
    password: str | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> tuple[TenantUser, CredentialLink | None]:
    """Create a user, its membership and roles; return a one-time link when no password is set."""
    normalized_email = validate_email(email)
    if not full_name.strip():
        raise _unprocessable("user_name_invalid", "The full name is required.")
    if password is not None:
        validate_password_strength(password)
    if not role_codes:
        raise _unprocessable("roles_required", "At least one role code is required.")
    company = await session.scalar(
        select(Company).where(Company.tenant_id == tenant_id, Company.id == company_id)
    )
    if company is None:
        raise _not_found("company_not_found", "The company does not exist in this tenant.")
    roles = {
        role.code: role
        for role in (
            await session.execute(select(Role).where(Role.tenant_id == tenant_id))
        ).scalars()
    }
    unknown = sorted(set(role_codes) - set(roles))
    if unknown:
        raise _unprocessable("role_unknown", f"Unknown role codes: {', '.join(unknown)}.")
    duplicate = await session.scalar(
        select(AppUser.id).where(
            AppUser.tenant_id == tenant_id,
            func.lower(AppUser.email) == normalized_email,
        )
    )
    if duplicate is not None:
        raise _conflict("user_email_taken", "A user with this email already exists in the tenant.")
    user = AppUser(
        tenant_id=tenant_id,
        email=normalized_email,
        full_name=full_name.strip(),
        password_hash=hash_password(password) if password is not None else None,
        created_by=actor_user_id,
        updated_by=actor_user_id,
    )
    session.add(user)
    await session.flush()
    membership = Membership(
        tenant_id=tenant_id,
        app_user_id=user.id,
        company_id=company.id,
        created_by=actor_user_id,
        updated_by=actor_user_id,
    )
    session.add(membership)
    await session.flush()
    for role_code in dict.fromkeys(role_codes):
        session.add(
            MembershipRole(
                tenant_id=tenant_id,
                membership_id=membership.id,
                role_id=roles[role_code].id,
            )
        )
    await session.flush()
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="admin.user.created",
        entity_type="app_user",
        entity_id=user.id,
        detail={"email": normalized_email, "role_codes": sorted(set(role_codes))},
        request_id=request_id,
        ip_address=ip_address,
    )
    link: CredentialLink | None = None
    if password is None:
        link = issue_credential_link(
            settings,
            user_id=user.id,
            tenant_id=tenant_id,
            purpose=INVITE_PURPOSE,
            epoch=user.credential_epoch,
            actor_user_id=actor_user_id,
            request_id=request_id,
            ip_address=ip_address,
        )
    return (
        TenantUser(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_active=user.is_active,
            companies=(
                CompanyAssignment(
                    company_id=company.id,
                    role_codes=tuple(sorted(set(role_codes))),
                ),
            ),
        ),
        link,
    )


def issue_credential_link(
    settings: Settings,
    *,
    user_id: UUID,
    tenant_id: UUID,
    purpose: str,
    epoch: int,
    actor_user_id: UUID | None = None,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> CredentialLink:
    """Sign a one-time link; the caller audits the issuance in its transaction."""
    del actor_user_id, request_id, ip_address  # signing is pure; audit is the caller's duty
    token, expires_at = create_credential_token(
        settings=settings,
        user_id=user_id,
        tenant_id=tenant_id,
        purpose=purpose,
        epoch=epoch,
    )
    return CredentialLink(token=token, expires_at=expires_at, purpose=purpose)


async def issue_reset_link(
    session: AsyncSession,
    settings: Settings,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    user_id: UUID,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> CredentialLink:
    """Issue a password-reset link for one tenant user and audit it."""
    user = await session.scalar(
        select(AppUser).where(AppUser.tenant_id == tenant_id, AppUser.id == user_id)
    )
    if user is None:
        raise _not_found("user_not_found", "The user does not exist in this tenant.")
    if not user.is_active:
        raise _conflict("user_inactive", "An inactive user cannot be sent a reset link.")
    link = issue_credential_link(
        settings,
        user_id=user.id,
        tenant_id=tenant_id,
        purpose=RESET_PURPOSE,
        epoch=user.credential_epoch,
    )
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="admin.password_reset_issued",
        entity_type="app_user",
        entity_id=user.id,
        detail={"email": user.email},
        request_id=request_id,
        ip_address=ip_address,
    )
    return link


async def accept_credential_link(
    session: AsyncSession,
    settings: Settings,
    *,
    claims_user_id: UUID,
    claims_tenant_id: UUID,
    purpose: str,
    epoch: int,
    new_password: str,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> AppUser:
    """Set a password from a validated one-time link and retire every older link."""
    validate_password_strength(new_password)
    user = await session.scalar(
        select(AppUser)
        .where(AppUser.tenant_id == claims_tenant_id, AppUser.id == claims_user_id)
        .with_for_update()
    )
    if user is None:
        raise _not_found("credential_invalid", "The link is not valid for this installation.")
    if not user.is_active:
        raise AppError(
            code="user_inactive",
            title="Forbidden",
            status_code=403,
            detail="This account is inactive.",
        )
    if purpose == INVITE_PURPOSE and user.password_hash is not None:
        raise _conflict("credential_used", "This invitation has already been accepted.")
    if user.credential_epoch != epoch:
        raise _conflict("credential_used", "This link was already used or superseded.")
    user.password_hash = hash_password(new_password)
    user.credential_epoch += 1
    user.updated_by = user.id
    await revoke_user_sessions(session, tenant_id=user.tenant_id, user_id=user.id)
    await record_audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="auth.credential_accepted",
        entity_type="app_user",
        entity_id=user.id,
        detail={"purpose": purpose},
        request_id=request_id,
        ip_address=ip_address,
    )
    return user


async def change_own_password(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    current_password: str,
    new_password: str,
    request_id: str | None = None,
    ip_address: str | None = None,
) -> None:
    """Change the signed-in user's password, retiring every session and link."""
    validate_password_strength(new_password)
    user = await session.scalar(
        select(AppUser)
        .where(AppUser.tenant_id == tenant_id, AppUser.id == user_id)
        .with_for_update()
    )
    if user is None:
        raise _not_found("user_not_found", "The user does not exist in this tenant.")
    if not verify_password(current_password, user.password_hash):
        raise AppError(
            code="invalid_credentials",
            title="Invalid credentials",
            status_code=401,
            detail="The current password is incorrect.",
        )
    user.password_hash = hash_password(new_password)
    user.credential_epoch += 1
    user.updated_by = user.id
    await revoke_user_sessions(session, tenant_id=tenant_id, user_id=user.id)
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=user.id,
        action="auth.password_changed",
        entity_type="app_user",
        entity_id=user.id,
        detail={},
        request_id=request_id,
        ip_address=ip_address,
    )


async def revoke_user_sessions(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
) -> None:
    """Revoke every live session of one user (password change retires them all)."""
    await session.execute(
        update(AuthSession)
        .where(
            AuthSession.tenant_id == tenant_id,
            AuthSession.app_user_id == user_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


async def list_tenant_users(session: AsyncSession, *, tenant_id: UUID) -> list[TenantUser]:
    """Return every tenant user with the companies and roles it holds."""
    rows = (
        await session.execute(
            select(AppUser, Membership, Role.code)
            .join(Membership, Membership.app_user_id == AppUser.id)
            .outerjoin(MembershipRole, MembershipRole.membership_id == Membership.id)
            .outerjoin(Role, Role.id == MembershipRole.role_id)
            .where(AppUser.tenant_id == tenant_id, Membership.tenant_id == tenant_id)
            .order_by(AppUser.email, Membership.company_id)
        )
    ).all()
    users: dict[UUID, tuple[AppUser, dict[UUID, set[str]]]] = {}
    for user, membership, role_code in rows:
        if user.id not in users:
            users[user.id] = (user, {})
        codes = users[user.id][1].setdefault(membership.company_id, set())
        if role_code:
            codes.add(role_code)
    return [
        TenantUser(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_active=user.is_active,
            companies=tuple(
                CompanyAssignment(company_id=company_id, role_codes=tuple(sorted(codes)))
                for company_id, codes in companies.items()
            ),
        )
        for user, companies in users.values()
    ]


async def set_user_active(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    user_id: UUID,
    is_active: bool,
    request_id: str | None = None,
) -> AppUser:
    """Activate or deactivate one tenant user, revoking sessions on deactivation."""
    user = await session.scalar(
        select(AppUser).where(AppUser.tenant_id == tenant_id, AppUser.id == user_id)
    )
    if user is None:
        raise _not_found("user_not_found", "The user does not exist in this tenant.")
    if user.id == actor_user_id and not is_active:
        raise _conflict("self_deactivation", "An administrator cannot deactivate itself.")
    user.is_active = is_active
    user.updated_by = actor_user_id
    if not is_active:
        await revoke_user_sessions(session, tenant_id=tenant_id, user_id=user.id)
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="admin.user.activated" if is_active else "admin.user.deactivated",
        entity_type="app_user",
        entity_id=user.id,
        detail={"email": user.email},
        request_id=request_id,
    )
    return user
