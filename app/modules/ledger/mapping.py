"""Effective-dated posting account mapping (report 20 §8).

Documents never name an account directly: each posting role resolves to the
company's current mapping for the document date. A mapping change closes the
previous row the day before and opens a new one, so reprints and audits can
still explain which account an older document used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.audit import record_audit
from app.modules.ledger.models import Account, PostingMapping
from app.shared.errors import AppError
from app.shared.ids import new_id

# The canonical chart code each role maps to when a company is first seeded.
DEFAULT_MAPPING: tuple[tuple[str, str], ...] = (
    ("accounts_receivable", "1100"),
    ("inventory", "1200"),
    ("cheques_under_collection", "1230"),
    ("withholding_receivable", "1250"),
    ("supplier_advances", "1400"),
    ("accounts_payable", "2000"),
    ("vat_payable", "2100"),
    ("withholding_payable", "2150"),
    ("cheques_issued", "2200"),
    ("customer_advances", "2300"),
    ("sales_revenue", "4000"),
    ("fx_gain", "4800"),
    ("cost_of_sales", "5100"),
    ("inventory_adjustment", "5200"),
    ("fx_loss", "5800"),
)


@dataclass(frozen=True)
class PostingRole:
    """One configurable posting destination, with the labels the settings UI shows."""

    key: str
    label_ar: str
    label_en: str
    account_type: str


POSTING_ROLES: tuple[PostingRole, ...] = (
    PostingRole("accounts_receivable", "العملاء", "Accounts receivable", "asset"),
    PostingRole("accounts_payable", "الموردون", "Accounts payable", "liability"),
    PostingRole("sales_revenue", "المبيعات", "Sales revenue", "revenue"),
    PostingRole("vat_payable", "ضريبة القيمة المضافة", "VAT payable", "liability"),
    PostingRole("inventory", "المخزون", "Inventory", "asset"),
    PostingRole("cost_of_sales", "تكلفة المبيعات", "Cost of sales", "expense"),
    PostingRole("inventory_adjustment", "فروق المخزون", "Inventory adjustment", "expense"),
    PostingRole("customer_advances", "دفعات مقدمة من العملاء", "Customer advances", "liability"),
    PostingRole("supplier_advances", "سلف الموردين", "Supplier advances", "asset"),
    PostingRole(
        "withholding_receivable", "ضريبة مستقطعة من العملاء", "Withholding receivable", "asset"
    ),
    PostingRole(
        "withholding_payable", "ضريبة مستقطعة من الموردين", "Withholding payable", "liability"
    ),
    PostingRole("fx_gain", "أرباح فروق العملة", "Exchange gain", "revenue"),
    PostingRole("fx_loss", "خسائر فروق العملة", "Exchange loss", "expense"),
    PostingRole(
        "cheques_under_collection", "شيكات برسم التحصيل", "Cheques under collection", "asset"
    ),
    PostingRole("cheques_issued", "شيكات صادرة", "Cheques issued", "liability"),
)

ROLES_BY_KEY: dict[str, PostingRole] = {role.key: role for role in POSTING_ROLES}
_ROLE_ALIASES: dict[str, str] = {role.key: role.key for role in POSTING_ROLES} | {
    code: key for key, code in DEFAULT_MAPPING
}


def canonical_role(key: str) -> PostingRole:
    """Return the role for a key or raise a clear 422 for unknown keys."""
    canonical = _ROLE_ALIASES.get(key, key)
    role = ROLES_BY_KEY.get(canonical)
    if role is None:
        raise AppError(
            code="posting_role_invalid",
            title="Invalid posting role",
            status_code=422,
            detail=f"Unknown posting role: {key}.",
        )
    return role


async def _load_company_accounts(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> dict[UUID, Account]:
    accounts = (
        (
            await session.execute(
                select(Account).where(
                    Account.tenant_id == tenant_id,
                    Account.company_id == company_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return {account.id: account for account in accounts}


async def resolve_posting_accounts(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    on_date: date,
    keys: tuple[str, ...],
) -> dict[str, Account]:
    """Resolve each posting role to the account effective on the document date."""
    canonical = {key: canonical_role(key).key for key in keys}
    rows = (
        (
            await session.execute(
                select(PostingMapping)
                .where(
                    PostingMapping.tenant_id == tenant_id,
                    PostingMapping.company_id == company_id,
                    PostingMapping.key.in_(set(canonical.values())),
                    PostingMapping.valid_from <= on_date,
                    (PostingMapping.valid_to.is_(None)) | (PostingMapping.valid_to >= on_date),
                )
                .order_by(PostingMapping.key, PostingMapping.valid_from)
            )
        )
        .scalars()
        .all()
    )
    effective: dict[str, PostingMapping] = {}
    for row in rows:
        effective[row.key] = row
    missing = [role_key for role_key in canonical.values() if role_key not in effective]
    if missing:
        raise AppError(
            code="posting_mapping_missing",
            title="Posting settings incomplete",
            status_code=409,
            detail=(
                "No posting account is configured for: "
                + ", ".join(sorted(set(missing)))
                + f" on {on_date.isoformat()}."
            ),
        )
    accounts_by_id = await _load_company_accounts(
        session, tenant_id=tenant_id, company_id=company_id
    )
    resolved: dict[str, Account] = {}
    for source_key, role_key in canonical.items():
        mapping = effective[role_key]
        account = accounts_by_id.get(mapping.account_id)
        if account is None:
            raise AppError(
                code="posting_mapping_missing",
                title="Posting settings incomplete",
                status_code=409,
                detail=f"The mapped account for {role_key} is not in this company's chart.",
            )
        if not account.is_postable or not account.is_active:
            raise AppError(
                code="account_not_postable",
                title="Invalid posting account",
                status_code=422,
                detail=f"Account {account.code} mapped to {role_key} is not active and postable.",
            )
        expected = ROLES_BY_KEY[role_key].account_type
        if account.account_type != expected:
            raise AppError(
                code="posting_mapping_type_mismatch",
                title="Invalid posting account",
                status_code=422,
                detail=(
                    f"Account {account.code} is {account.account_type}; "
                    f"{role_key} needs a {expected} account."
                ),
            )
        resolved[source_key] = account
    return resolved


@dataclass(frozen=True)
class MappingRow:
    """One posting role and its effective account (or None when unset)."""

    key: str
    label_ar: str
    label_en: str
    account_type: str
    account_id: UUID | None
    account_code: str | None
    account_name_ar: str | None
    valid_from: date | None


async def list_posting_mappings(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    on_date: date,
) -> list[MappingRow]:
    """Return every role with the mapping effective on the given date."""
    rows = (
        await session.execute(
            select(PostingMapping, Account)
            .join(
                Account,
                (Account.tenant_id == PostingMapping.tenant_id)
                & (Account.company_id == PostingMapping.company_id)
                & (Account.id == PostingMapping.account_id),
            )
            .where(
                PostingMapping.tenant_id == tenant_id,
                PostingMapping.company_id == company_id,
                PostingMapping.valid_from <= on_date,
                (PostingMapping.valid_to.is_(None)) | (PostingMapping.valid_to >= on_date),
            )
            .order_by(PostingMapping.key, PostingMapping.valid_from)
        )
    ).all()
    effective: dict[str, tuple[PostingMapping, Account]] = {}
    for mapping, account in rows:
        effective[mapping.key] = (mapping, account)
    result: list[MappingRow] = []
    for role in POSTING_ROLES:
        pair = effective.get(role.key)
        result.append(
            MappingRow(
                key=role.key,
                label_ar=role.label_ar,
                label_en=role.label_en,
                account_type=role.account_type,
                account_id=pair[1].id if pair else None,
                account_code=pair[1].code if pair else None,
                account_name_ar=pair[1].name_ar if pair else None,
                valid_from=pair[0].valid_from if pair else None,
            )
        )
    return result


async def update_posting_mapping(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    actor_user_id: UUID,
    key: str,
    account_id: UUID,
    valid_from: date,
) -> PostingMapping:
    """Point one posting role at an account from `valid_from` onwards."""
    role = canonical_role(key)
    account = await session.get(Account, account_id)
    if account is None or account.tenant_id != tenant_id or account.company_id != company_id:
        raise AppError(
            code="account_not_found",
            title="Not found",
            status_code=404,
            detail="The account does not exist in the selected company.",
        )
    if not account.is_postable or not account.is_active:
        raise AppError(
            code="account_not_postable",
            title="Invalid posting account",
            status_code=422,
            detail=f"Account {account.code} is not an active postable account.",
        )
    if account.account_type != role.account_type:
        raise AppError(
            code="posting_mapping_type_mismatch",
            title="Invalid posting account",
            status_code=422,
            detail=(
                f"Account {account.code} is {account.account_type}; "
                f"the role needs a {role.account_type} account."
            ),
        )
    existing_rows = list(
        (
            await session.execute(
                select(PostingMapping)
                .where(
                    PostingMapping.tenant_id == tenant_id,
                    PostingMapping.company_id == company_id,
                    PostingMapping.key == role.key,
                )
                .order_by(PostingMapping.valid_from)
            )
        )
        .scalars()
        .all()
    )
    same_start = next((row for row in existing_rows if row.valid_from == valid_from), None)
    if same_start is not None:
        previous = same_start.account_id
        same_start.account_id = account_id
        same_start.updated_by = actor_user_id
        mapping = same_start
    else:
        previous = None
        later = next((row for row in existing_rows if row.valid_from > valid_from), None)
        for row in existing_rows:
            if row.valid_from < valid_from and (row.valid_to is None or row.valid_to >= valid_from):
                row.valid_to = valid_from - timedelta(days=1)
                row.updated_by = actor_user_id
                previous = row.account_id
        mapping = PostingMapping(
            id=new_id(),
            tenant_id=tenant_id,
            company_id=company_id,
            key=role.key,
            account_id=account_id,
            valid_from=valid_from,
            valid_to=(later.valid_from - timedelta(days=1)) if later is not None else None,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(mapping)
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="settings.posting_mapping.updated",
        entity_type="posting_mapping",
        entity_id=mapping.id,
        detail={
            "key": role.key,
            "account_code": account.code,
            "valid_from": valid_from.isoformat(),
            "previous_account_id": str(previous) if previous is not None else None,
        },
    )
    await session.commit()
    return mapping


async def seed_posting_mappings(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    accounts_by_code: dict[str, Account],
) -> None:
    """Insert the default mapping rows for a freshly seeded company chart."""
    existing = set(
        (
            await session.execute(
                select(PostingMapping.key).where(
                    PostingMapping.tenant_id == tenant_id,
                    PostingMapping.company_id == company_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for key, code in DEFAULT_MAPPING:
        account = accounts_by_code.get(code)
        if account is None or key in existing:
            continue
        session.add(
            PostingMapping(
                tenant_id=tenant_id,
                company_id=company_id,
                key=key,
                account_id=account.id,
                valid_from=date(2000, 1, 1),
            )
        )
    await session.flush()
