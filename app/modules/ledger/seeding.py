"""Minimal chart of accounts for phase-0 tests and demo (day 5).

The chart is data, not policy, so it is seeded through the service/API rather
than baked into a migration. Codes are stable test fixtures, not hard-coded
business rules.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ledger.mapping import seed_posting_mappings
from app.modules.ledger.models import Account

# (code, name_ar, name_en, account_type, is_postable)
DEFAULT_CHART: tuple[tuple[str, str, str, str, bool], ...] = (
    ("1000", "النقدية", "Cash", "asset", True),
    ("1010", "البنك", "Bank", "asset", True),
    ("1100", "العملاء", "Accounts receivable", "asset", True),
    ("1200", "المخزون", "Inventory", "asset", True),
    ("1230", "شيكات برسم التحصيل", "Cheques under collection", "asset", True),
    ("1250", "ضريبة مستقطعة من العملاء", "Withholding receivable", "asset", True),
    ("1400", "سلف الموردين", "Supplier advances", "asset", True),
    ("2000", "الموردون", "Accounts payable", "liability", True),
    ("2100", "ضريبة القيمة المضافة", "Value added tax", "liability", True),
    ("2150", "ضريبة مستقطعة من الموردين", "Withholding payable", "liability", True),
    ("2200", "شيكات صادرة", "Cheques issued", "liability", True),
    ("2300", "دفعات مقدمة من العملاء", "Customer advances", "liability", True),
    ("3000", "رأس المال", "Capital", "equity", True),
    ("3100", "الأرباح المحتجزة", "Retained earnings", "equity", True),
    ("4000", "المبيعات", "Sales", "revenue", True),
    ("4800", "أرباح فروق العملة", "Exchange gain", "revenue", True),
    ("5000", "المشتريات", "Purchases", "expense", True),
    ("5100", "تكلفة المبيعات", "Cost of sales", "expense", True),
    ("5200", "فروق المخزون", "Inventory adjustment", "expense", True),
    ("5800", "خسائر فروق العملة", "Exchange loss", "expense", True),
    ("5900", "مصروفات عمومية", "General expenses", "expense", True),
)


async def seed_company_chart(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> dict[str, Account]:
    """Insert the default chart for one company if absent; return code -> account."""
    existing = {
        account.code: account
        for account in (
            await session.execute(
                select(Account).where(
                    Account.tenant_id == tenant_id,
                    Account.company_id == company_id,
                )
            )
        )
        .scalars()
        .all()
    }
    for code, name_ar, name_en, account_type, is_postable in DEFAULT_CHART:
        if code in existing:
            continue
        account = Account(
            tenant_id=tenant_id,
            company_id=company_id,
            code=code,
            name_ar=name_ar,
            name_en=name_en,
            account_type=account_type,
            is_postable=is_postable,
        )
        session.add(account)
        existing[code] = account
    await session.flush()
    await seed_posting_mappings(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        accounts_by_code=existing,
    )
    return existing
