"""Opening-balance CSV import (phase 1, slice 5).

The accountant uploads one row per account with a debit or credit amount in the
company's base currency. The import validates every row, the chart and the
period first; a dry run returns the validation report without writing anything,
and a committed run posts exactly one balanced opening journal entry dated in
the chosen period. Two runs can never post the same file into the same company
period twice by accident: each committed import gets its own source id, and the
ledger's source uniqueness still applies per import.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ledger import service
from app.modules.ledger.models import Account, AccountingPeriod
from app.modules.ledger.service import JournalEntryInput, JournalLineInput
from app.shared.errors import AppError
from app.shared.ids import new_id
from app.shared.unit_of_work import UnitOfWork

OPENING_SOURCE_TYPE = "opening_balance"
OPENING_MEMO = "Opening balances imported from CSV"


@dataclass(frozen=True)
class OpeningBalanceLine:
    """One validated CSV row, in integer minor units."""

    account_code: str
    account_name_ar: str
    account_name_en: str | None
    debit_minor: int
    credit_minor: int
    description: str | None


@dataclass(frozen=True)
class OpeningBalanceReport:
    """The validation report returned to the accountant."""

    valid: bool
    total_debit_minor: int
    total_credit_minor: int
    line_count: int
    lines: tuple[OpeningBalanceLine, ...]
    entry_id: UUID | None
    entry_number: str | None


def _row_error(row_number: int, detail: str) -> AppError:
    return AppError(
        code="import_invalid_row",
        title="Unprocessable entity",
        status_code=422,
        detail=f"Row {row_number}: {detail}",
    )


def _amount(row: dict[str, str], field: str, row_number: int) -> int:
    raw = row.get(field, "").strip()
    if raw == "":
        return 0
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise _row_error(row_number, f"'{field}' is not a number.") from error
    if value < 0:
        raise _row_error(row_number, f"'{field}' must not be negative.")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _parse_rows(rows: list[dict[str, str]]) -> tuple[OpeningBalanceLine, ...]:
    """Validate the CSV rows; amounts stay unresolved until the chart is loaded."""
    parsed: list[OpeningBalanceLine] = []
    seen: set[str] = set()
    for offset, row in enumerate(rows, start=2):
        code = row.get("account_code", "").strip()
        if code == "":
            raise _row_error(offset, "'account_code' is required.")
        if code in seen:
            raise _row_error(offset, f"duplicate account code '{code}' in the file.")
        seen.add(code)
        debit = _amount(row, "debit", offset)
        credit = _amount(row, "credit", offset)
        if debit == 0 and credit == 0:
            raise _row_error(offset, "enter a debit or a credit amount.")
        if debit != 0 and credit != 0:
            raise _row_error(offset, "one row must not hold both a debit and a credit.")
        description = row.get("description", "").strip() or None
        parsed.append(
            OpeningBalanceLine(
                account_code=code,
                account_name_ar="",
                account_name_en=None,
                debit_minor=debit,
                credit_minor=credit,
                description=description,
            )
        )
    total_debit = sum(line.debit_minor for line in parsed)
    total_credit = sum(line.credit_minor for line in parsed)
    if total_debit != total_credit:
        raise AppError(
            code="opening_balance_unbalanced",
            title="Unprocessable entity",
            status_code=422,
            detail=(
                "The opening balances are not balanced: "
                f"debits {total_debit / 100:.2f} vs credits {total_credit / 100:.2f}."
            ),
        )
    if total_debit == 0:
        raise AppError(
            code="opening_balance_empty",
            title="Unprocessable entity",
            status_code=422,
            detail="The opening balances total zero; nothing to import.",
        )
    return tuple(parsed)


async def _resolve(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    period_id: UUID,
    entry_date: date,
    posting: bool,
    lines: tuple[OpeningBalanceLine, ...],
) -> dict[str, Account]:
    period = (
        await session.execute(
            select(AccountingPeriod).where(
                AccountingPeriod.tenant_id == tenant_id,
                AccountingPeriod.company_id == company_id,
                AccountingPeriod.id == period_id,
            )
        )
    ).scalar_one_or_none()
    if period is None:
        raise AppError(
            code="period_not_found",
            title="Not found",
            status_code=404,
            detail="The accounting period does not exist in the selected company.",
        )
    if entry_date < period.start_date or entry_date > period.end_date:
        raise AppError(
            code="entry_date_outside_period",
            title="Unprocessable entity",
            status_code=422,
            detail=f"Entry date {entry_date.isoformat()} is outside the selected period.",
        )
    if posting and period.status != "open":
        raise AppError(
            code="period_closed",
            title="Conflict",
            status_code=409,
            detail="The selected accounting period is closed.",
        )
    return await service.load_accounts_by_code(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        codes=tuple(line.account_code for line in lines),
    )


def _named(
    lines: tuple[OpeningBalanceLine, ...], accounts: dict[str, Account]
) -> tuple[OpeningBalanceLine, ...]:
    return tuple(
        OpeningBalanceLine(
            account_code=line.account_code,
            account_name_ar=accounts[line.account_code].name_ar,
            account_name_en=accounts[line.account_code].name_en,
            debit_minor=line.debit_minor,
            credit_minor=line.credit_minor,
            description=line.description,
        )
        for line in lines
    )


async def import_opening_balances(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    period_id: UUID,
    entry_date: date,
    rows: list[dict[str, str]],
    post: bool,
    dry_run: bool,
) -> OpeningBalanceReport:
    """Validate the CSV; on a committed run post one balanced opening entry."""
    parsed = _parse_rows(rows)

    async def inspect(session: AsyncSession) -> dict[str, Account]:
        return await _resolve(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            period_id=period_id,
            entry_date=entry_date,
            posting=post,
            lines=parsed,
        )

    accounts = await unit_of_work.run(inspect, tenant_id=tenant_id)
    named = _named(parsed, accounts)
    total_debit = sum(line.debit_minor for line in named)
    total_credit = sum(line.credit_minor for line in named)
    if dry_run:
        return OpeningBalanceReport(
            valid=True,
            total_debit_minor=total_debit,
            total_credit_minor=total_credit,
            line_count=len(named),
            lines=named,
            entry_id=None,
            entry_number=None,
        )
    result = await service.create_journal_entry(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        request=JournalEntryInput(
            company_id=company_id,
            period_id=period_id,
            entry_date=entry_date,
            memo=OPENING_MEMO,
            post=post,
            source_type=OPENING_SOURCE_TYPE,
            source_id=new_id(),
            lines=tuple(
                JournalLineInput(
                    account_id=accounts[line.account_code].id,
                    debit_minor=line.debit_minor,
                    credit_minor=line.credit_minor,
                    description=line.description or "Opening balance",
                )
                for line in named
            ),
        ),
    )
    return OpeningBalanceReport(
        valid=True,
        total_debit_minor=total_debit,
        total_credit_minor=total_credit,
        line_count=len(named),
        lines=named,
        entry_id=result.entry.id,
        entry_number=result.entry.number,
    )
