"""Fiscal year-end close: zero the income statement into retained earnings.

Closing a year posts one final journal entry dated the fiscal year's last day.
Every revenue and expense account with a balance in the year is reversed, and
the net profit or loss is booked to retained earnings (3100). The entry is a
normal posted entry, so it obeys the same debit = credit, immutability and
closed-period rules as every other posting. All periods of the year are then
closed and the fiscal year is marked closed.

Design limits, recorded so they cannot be mistaken for features:
* the last period of the year must still be open when the close runs, because a
  posted entry cannot enter a closed period; reopen it first if it was closed;
* a closed year cannot be reopened through the API; a later decision will say
  whether that is a reversal entry or an explicit reopen workflow;
* only posted entries are closed; any draft entry in the year blocks the close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.audit import record_audit
from app.modules.ledger.models import (
    Account,
    AccountingPeriod,
    FiscalYear,
    JournalEntry,
    JournalLine,
)
from app.modules.ledger.service import (
    EntryResult,
    JournalLineInput,
    load_accounts_by_code,
    post_entry_in_session,
)
from app.shared.context import get_request_id
from app.shared.errors import AppError
from app.shared.unit_of_work import UnitOfWork

CLOSING_ACCOUNT_CODE = "3100"
CLOSING_SOURCE_TYPE = "fiscal_year_close"
INCOME_STATEMENT_TYPES = ("revenue", "expense")


@dataclass(frozen=True)
class YearCloseResult:
    """The closed fiscal year, its closing entry (when there was activity) and periods."""

    fiscal_year: FiscalYear
    closing_entry: EntryResult | None
    periods_closed: int


def _now() -> datetime:
    return datetime.now(UTC)


def _conflict(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Conflict", status_code=409, detail=detail)


async def _load_fiscal_year(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    fiscal_year_id: UUID,
    for_update: bool = False,
) -> FiscalYear:
    statement = select(FiscalYear).where(
        FiscalYear.tenant_id == tenant_id,
        FiscalYear.company_id == company_id,
        FiscalYear.id == fiscal_year_id,
    )
    if for_update:
        # Two concurrent closes must serialise: the second waits here, then sees
        # status 'closed' and answers 409 instead of posting a duplicate entry.
        statement = statement.with_for_update()
    fiscal_year = (await session.execute(statement)).scalar_one_or_none()
    if fiscal_year is None:
        raise AppError(
            code="fiscal_year_not_found",
            title="Not found",
            status_code=404,
            detail="The fiscal year does not exist in the selected company.",
        )
    return fiscal_year


async def _income_statement_balances(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    start_date: date,
    end_date: date,
) -> list[tuple[UUID, str, int]]:
    """Return (account_id, code, balance) for revenue and expense accounts."""
    rows = (
        await session.execute(
            select(
                Account.id,
                Account.code,
                func.coalesce(func.sum(JournalLine.debit_minor), 0),
                func.coalesce(func.sum(JournalLine.credit_minor), 0),
            )
            .select_from(Account)
            .join(JournalLine, JournalLine.account_id == Account.id)
            .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(
                Account.tenant_id == tenant_id,
                Account.company_id == company_id,
                Account.account_type.in_(INCOME_STATEMENT_TYPES),
                JournalEntry.status == "posted",
                JournalEntry.entry_date >= start_date,
                JournalEntry.entry_date <= end_date,
            )
            .group_by(Account.id, Account.code)
            .order_by(Account.code)
        )
    ).all()
    return [
        (account_id, code, int(debit) - int(credit)) for account_id, code, debit, credit in rows
    ]


async def close_fiscal_year(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    fiscal_year_id: UUID,
    reason: str,
) -> YearCloseResult:
    """Close one fiscal year: post the closing entry, close periods, mark the year closed."""

    async def operation(session: AsyncSession) -> YearCloseResult:
        fiscal_year = await _load_fiscal_year(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            fiscal_year_id=fiscal_year_id,
            for_update=True,
        )
        if fiscal_year.status == "closed":
            raise _conflict("fiscal_year_already_closed", "The fiscal year is already closed.")
        periods = (
            (
                await session.execute(
                    select(AccountingPeriod)
                    .where(
                        AccountingPeriod.tenant_id == tenant_id,
                        AccountingPeriod.company_id == company_id,
                        AccountingPeriod.fiscal_year_id == fiscal_year.id,
                    )
                    .order_by(AccountingPeriod.number)
                )
            )
            .scalars()
            .all()
        )
        if not periods:
            raise _conflict("fiscal_year_has_no_periods", "The fiscal year has no periods.")
        draft_count = await session.scalar(
            select(func.count())
            .select_from(JournalEntry)
            .where(
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.company_id == company_id,
                JournalEntry.entry_date >= fiscal_year.start_date,
                JournalEntry.entry_date <= fiscal_year.end_date,
                JournalEntry.status == "draft",
            )
        )
        if draft_count:
            raise _conflict(
                "fiscal_year_has_drafts",
                f"The fiscal year still has {int(draft_count)} draft journal entries.",
            )
        target = next(
            (
                period
                for period in periods
                if period.start_date <= fiscal_year.end_date <= period.end_date
            ),
            None,
        )
        if target is None:
            raise _conflict(
                "fiscal_year_target_period_missing",
                "No accounting period contains the fiscal year end date.",
            )
        if target.status != "open":
            raise _conflict(
                "fiscal_year_target_period_closed",
                "The last period of the fiscal year is closed; reopen it before closing the year.",
            )
        balances = await _income_statement_balances(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            start_date=fiscal_year.start_date,
            end_date=fiscal_year.end_date,
        )
        lines: list[JournalLineInput] = []
        for account_id, code, balance in balances:
            if balance == 0:
                continue
            lines.append(
                JournalLineInput(
                    account_id=account_id,
                    debit_minor=max(-balance, 0),
                    credit_minor=max(balance, 0),
                    description=f"Year-end close {fiscal_year.code} account {code}",
                )
            )
        closing_entry: EntryResult | None = None
        if lines:
            accounts = await load_accounts_by_code(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                codes=(CLOSING_ACCOUNT_CODE,),
            )
            total_debit = sum(line.debit_minor for line in lines)
            total_credit = sum(line.credit_minor for line in lines)
            difference = total_debit - total_credit
            lines.append(
                JournalLineInput(
                    account_id=accounts[CLOSING_ACCOUNT_CODE].id,
                    debit_minor=max(-difference, 0),
                    credit_minor=max(difference, 0),
                    description=f"Retained earnings {fiscal_year.code}",
                )
            )
            closing_entry = await post_entry_in_session(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                company_id=company_id,
                period_id=target.id,
                entry_date=fiscal_year.end_date,
                memo=f"Year-end close {fiscal_year.code}",
                lines=tuple(lines),
                source_type=CLOSING_SOURCE_TYPE,
                source_id=fiscal_year.id,
            )
        periods_closed = 0
        for period in periods:
            if period.status == "closed":
                continue
            period.status = "closed"
            period.closed_at = _now()
            period.closed_by = actor_user_id
            period.updated_by = actor_user_id
            periods_closed += 1
        fiscal_year.status = "closed"
        fiscal_year.updated_by = actor_user_id
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="ledger.fiscal_year.closed",
            entity_type="fiscal_year",
            entity_id=fiscal_year.id,
            detail={
                "reason": reason,
                "code": fiscal_year.code,
                "periods_closed": periods_closed,
                "closing_entry_id": str(closing_entry.entry.id) if closing_entry else None,
            },
            request_id=get_request_id() if get_request_id() != "-" else None,
        )
        return YearCloseResult(
            fiscal_year=fiscal_year,
            closing_entry=closing_entry,
            periods_closed=periods_closed,
        )

    return await unit_of_work.run(operation, tenant_id=tenant_id)
