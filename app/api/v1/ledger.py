"""Ledger endpoints: chart, periods, journal entries and reversals (day 6).

Every endpoint is company-scoped through X-Company-Id and permission-checked
inside that company. Writes go through the posting service and its unit of
work; a client may send an Idempotency-Key header so a retried request returns
the entry the first call created instead of posting twice.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import (
    PrincipalDep,
    TenantSessionDep,
    UnitOfWorkDep,
    require_company_permission,
)
from app.modules.identity.service import CompanyAccess
from app.modules.ledger import mapping, service, year_end
from app.modules.ledger.models import (
    Account,
    AccountingPeriod,
    FiscalYear,
    JournalEntry,
    JournalLine,
)
from app.modules.ledger.service import JournalEntryInput, JournalLineInput, ReversalInput
from app.shared.errors import AppError

router = APIRouter(prefix="/ledger", tags=["ledger"])

IDEMPOTENCY_HEADER = "Idempotency-Key"


class JournalLineIn(BaseModel):
    """One line of a manual journal entry."""

    account_id: UUID
    debit_minor: Annotated[int, Field(ge=0)] = 0
    credit_minor: Annotated[int, Field(ge=0)] = 0
    description: Annotated[str | None, Field(max_length=300)] = None


class JournalEntryCreate(BaseModel):
    """A manual journal entry to create as a draft or post immediately."""

    period_id: UUID
    entry_date: date
    memo: Annotated[str | None, Field(max_length=500)] = None
    post: bool = False
    lines: Annotated[list[JournalLineIn], Field(min_length=2, max_length=100)]


class JournalEntryUpdate(BaseModel):
    """A replacement header and lines for an existing draft entry."""

    period_id: UUID
    entry_date: date
    memo: Annotated[str | None, Field(max_length=500)] = None
    lines: Annotated[list[JournalLineIn], Field(min_length=2, max_length=100)]


class ReversalCreate(BaseModel):
    """A request to reverse a posted journal entry."""

    entry_date: date | None = None
    memo: Annotated[str | None, Field(max_length=500)] = None


class PeriodAction(BaseModel):
    """The reason recorded for closing or reopening a period."""

    reason: Annotated[str, Field(min_length=1, max_length=500)]


class JournalLineOut(BaseModel):
    """One posted line."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_no: int
    account_id: UUID
    debit_minor: int
    credit_minor: int
    description: str | None


class JournalEntrySummary(BaseModel):
    """A journal entry header, without lines."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    number: str
    status: str
    entry_date: date
    period_id: UUID
    memo: str | None
    source_type: str | None
    source_id: UUID | None
    reverses_entry_id: UUID | None
    posted_at: datetime | None
    created_at: datetime


class JournalEntryOut(JournalEntrySummary):
    """A journal entry with its lines and totals."""

    total_debit_minor: int
    total_credit_minor: int
    lines: list[JournalLineOut]
    idempotent_replay: bool = False


class AccountOut(BaseModel):
    """One account in the chart."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name_ar: str
    name_en: str | None
    account_type: str
    is_postable: bool
    is_active: bool


class AccountCreate(BaseModel):
    """A new chart account for the selected company."""

    code: Annotated[str, Field(min_length=1, max_length=20)]
    name_ar: Annotated[str, Field(min_length=1, max_length=200)]
    name_en: Annotated[str | None, Field(max_length=200)] = None
    account_type: Annotated[str, Field(pattern="^(asset|liability|equity|revenue|expense)$")] = (
        "asset"
    )
    is_postable: bool = True
    is_active: bool = True


class AccountUpdate(BaseModel):
    """Editable attributes of an existing account."""

    name_ar: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    name_en: Annotated[str | None, Field(max_length=200)] = None
    is_postable: bool | None = None
    is_active: bool | None = None


class PeriodOut(BaseModel):
    """One accounting period."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    number: int
    start_date: date
    end_date: date
    status: str
    closed_at: datetime | None


def _line_out(line: JournalLine) -> JournalLineOut:
    return JournalLineOut.model_validate(line)


def _entry_body(result: service.EntryResult) -> JournalEntryOut:
    total_debit = sum(line.debit_minor for line in result.lines)
    total_credit = sum(line.credit_minor for line in result.lines)
    return JournalEntryOut(
        **JournalEntrySummary.model_validate(result.entry).model_dump(),
        total_debit_minor=total_debit,
        total_credit_minor=total_credit,
        lines=[_line_out(line) for line in result.lines],
        idempotent_replay=result.replayed,
    )


def _account_out(account: Account) -> AccountOut:
    return AccountOut.model_validate(account)


def _period_out(period: AccountingPeriod) -> PeriodOut:
    return PeriodOut.model_validate(period)


def _summary_out(entry: JournalEntry) -> JournalEntrySummary:
    return JournalEntrySummary.model_validate(entry)


@router.get("/accounts", response_model=list[AccountOut], summary="Company chart of accounts")
async def read_accounts(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[AccountOut]:
    """Return the selected company's chart, ordered by code."""
    accounts = await service.list_accounts(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    return [_account_out(account) for account in accounts]


class PostingMappingOut(BaseModel):
    """One posting role and the account effective for the requested date."""

    key: str
    label_ar: str
    label_en: str
    account_type: str
    account_id: UUID | None
    account_code: str | None
    account_name_ar: str | None
    valid_from: date | None


class PostingMappingUpdate(BaseModel):
    """Point a posting role at an account from a date onwards."""

    account_id: UUID
    valid_from: date | None = None


def _mapping_out(row: mapping.MappingRow) -> PostingMappingOut:
    return PostingMappingOut(
        key=row.key,
        label_ar=row.label_ar,
        label_en=row.label_en,
        account_type=row.account_type,
        account_id=row.account_id,
        account_code=row.account_code,
        account_name_ar=row.account_name_ar,
        valid_from=row.valid_from,
    )


@router.get(
    "/posting-mappings",
    response_model=list[PostingMappingOut],
    summary="Posting account mapping effective on a date",
)
async def get_posting_mappings(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    on_date: Annotated[date | None, Query()] = None,
) -> list[PostingMappingOut]:
    """Return every posting role and the account effective on the given date."""
    effective = on_date or datetime.now(UTC).date()
    rows = await mapping.list_posting_mappings(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        on_date=effective,
    )
    return [_mapping_out(row) for row in rows]


@router.put(
    "/posting-mappings/{key}",
    response_model=list[PostingMappingOut],
    summary="Change one posting account from a date",
)
async def put_posting_mapping(
    key: str,
    payload: PostingMappingUpdate,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("settings.manage"))],
) -> list[PostingMappingOut]:
    """Close the previous mapping and open a new effective-dated one."""
    effective = payload.valid_from or datetime.now(UTC).date()
    await mapping.update_posting_mapping(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        actor_user_id=principal.user_id,
        key=key,
        account_id=payload.account_id,
        valid_from=effective,
    )
    rows = await mapping.list_posting_mappings(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        on_date=effective,
    )
    return [_mapping_out(row) for row in rows]


@router.post("/chart/seed", response_model=list[AccountOut], summary="Seed the default chart")
async def seed_chart(
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("settings.manage"))],
) -> list[AccountOut]:
    """Insert the phase-0 default chart for a company that does not have one."""
    accounts = await service.seed_chart(
        unit_of_work, tenant_id=principal.tenant_id, company_id=company.id
    )
    return [_account_out(account) for account in accounts]


@router.post(
    "/accounts",
    response_model=AccountOut,
    status_code=201,
    summary="Create a chart account",
)
async def create_account(
    payload: AccountCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("settings.manage"))],
) -> AccountOut:
    """Add one account to the selected company's chart; a duplicate code is refused."""
    account = await service.create_account(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=service.AccountInput(
            company_id=company.id,
            code=payload.code,
            name_ar=payload.name_ar,
            name_en=payload.name_en,
            account_type=payload.account_type,
            is_postable=payload.is_postable,
            is_active=payload.is_active,
        ),
    )
    return _account_out(account)


@router.patch(
    "/accounts/{account_id}",
    response_model=AccountOut,
    summary="Rename or activate/deactivate a chart account",
)
async def update_account(
    account_id: UUID,
    payload: AccountUpdate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("settings.manage"))],
) -> AccountOut:
    """Update an account's names and flags; codes are immutable once created."""
    account = await service.update_account(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        account_id=account_id,
        name_ar=payload.name_ar,
        name_en=payload.name_en,
        is_postable=payload.is_postable,
        is_active=payload.is_active,
    )
    return _account_out(account)


@router.get("/periods", response_model=list[PeriodOut], summary="Accounting periods")
async def read_periods(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[PeriodOut]:
    """Return the selected company's periods in date order."""
    periods = await service.list_periods(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    return [_period_out(period) for period in periods]


@router.post(
    "/periods/{period_id}/close",
    response_model=PeriodOut,
    summary="Close an accounting period",
)
async def close_period(
    period_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("ledger.period.close"))],
    payload: PeriodAction,
) -> PeriodOut:
    """Close a period; refuses while draft entries remain and records the reason."""
    period = await service.close_period(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        period_id=period_id,
        reason=payload.reason,
    )
    return _period_out(period)


@router.post(
    "/periods/{period_id}/reopen",
    response_model=PeriodOut,
    summary="Reopen an accounting period",
)
async def reopen_period(
    period_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("ledger.period.close"))],
    payload: PeriodAction,
) -> PeriodOut:
    """Reopen a closed period and record the reason in the audit trail."""
    period = await service.reopen_period(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        period_id=period_id,
        reason=payload.reason,
    )
    return _period_out(period)


@router.post(
    "/journal-entries",
    response_model=JournalEntryOut,
    status_code=201,
    summary="Create a draft or posted journal entry",
)
async def create_journal_entry(
    payload: JournalEntryCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("ledger.journal.create"))],
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> JournalEntryOut:
    """Create a manual journal entry; posting needs ledger.journal.post here."""
    if payload.post and "ledger.journal.post" not in company.permissions:
        raise AppError(
            code="permission_denied",
            title="Forbidden",
            status_code=403,
            detail="The permission 'ledger.journal.post' is required in the selected company.",
        )
    request = JournalEntryInput(
        company_id=company.id,
        period_id=payload.period_id,
        entry_date=payload.entry_date,
        memo=payload.memo,
        post=payload.post,
        lines=tuple(
            JournalLineInput(
                account_id=line.account_id,
                debit_minor=line.debit_minor,
                credit_minor=line.credit_minor,
                description=line.description,
            )
            for line in payload.lines
        ),
        idempotency_key=idempotency_key,
    )
    result = await service.create_journal_entry(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=request,
    )
    return _entry_body(result)


@router.get(
    "/journal-entries",
    response_model=list[JournalEntrySummary],
    summary="List journal entries",
)
async def read_journal_entries(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period_id: UUID | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[JournalEntrySummary]:
    """List the selected company's entries, newest first."""
    entries = await service.list_entries(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        period_id=period_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [_summary_out(entry) for entry in entries]


@router.get(
    "/journal-entries/{entry_id}",
    response_model=JournalEntryOut,
    summary="Read a journal entry",
)
async def read_journal_entry(
    entry_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> JournalEntryOut:
    """Return one entry and its lines."""
    result = await service.get_entry(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        entry_id=entry_id,
    )
    return _entry_body(result)


@router.put(
    "/journal-entries/{entry_id}",
    response_model=JournalEntryOut,
    summary="Edit a draft journal entry",
)
async def update_journal_entry(
    entry_id: UUID,
    payload: JournalEntryUpdate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("ledger.journal.create"))],
) -> JournalEntryOut:
    """Replace a draft's header and lines; posted entries are immutable."""
    result = await service.update_journal_entry(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        entry_id=entry_id,
        request=JournalEntryInput(
            company_id=company.id,
            period_id=payload.period_id,
            entry_date=payload.entry_date,
            memo=payload.memo,
            lines=tuple(
                JournalLineInput(
                    account_id=line.account_id,
                    debit_minor=line.debit_minor,
                    credit_minor=line.credit_minor,
                    description=line.description,
                )
                for line in payload.lines
            ),
        ),
    )
    return _entry_body(result)


@router.post(
    "/journal-entries/{entry_id}/post",
    response_model=JournalEntryOut,
    summary="Post a draft journal entry",
)
async def post_journal_entry(
    entry_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("ledger.journal.post"))],
) -> JournalEntryOut:
    """Post a draft after the database re-checks the period and balance."""
    result = await service.post_journal_entry(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        entry_id=entry_id,
    )
    return _entry_body(result)


@router.delete(
    "/journal-entries/{entry_id}",
    status_code=204,
    summary="Delete a draft journal entry",
)
async def delete_journal_entry(
    entry_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("ledger.journal.create"))],
) -> None:
    """Delete a draft entry and its lines; a posted entry is never deleted."""
    await service.delete_journal_entry(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        entry_id=entry_id,
    )


@router.post(
    "/journal-entries/{entry_id}/reverse",
    response_model=JournalEntryOut,
    status_code=201,
    summary="Reverse a posted journal entry",
)
async def reverse_journal_entry(
    entry_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("ledger.journal.post"))],
    payload: ReversalCreate | None = None,
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> JournalEntryOut:
    """Post a linked reversal; the original posted entry stays immutable."""
    body = payload or ReversalCreate()
    result = await service.reverse_journal_entry(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=ReversalInput(
            company_id=company.id,
            entry_id=entry_id,
            entry_date=body.entry_date,
            memo=body.memo,
            idempotency_key=idempotency_key,
        ),
    )
    return _entry_body(result)


class FiscalYearOut(BaseModel):
    """One fiscal year with its periods."""

    id: UUID
    code: str
    name_ar: str
    start_date: date
    end_date: date
    status: str
    periods: list[PeriodOut]


class FiscalYearCloseOut(BaseModel):
    """The result of closing a fiscal year."""

    fiscal_year: FiscalYearOut
    closing_entry: JournalEntryOut | None
    periods_closed: int


def _fiscal_year_out(fiscal_year: FiscalYear, periods: list[AccountingPeriod]) -> FiscalYearOut:
    return FiscalYearOut(
        id=fiscal_year.id,
        code=fiscal_year.code,
        name_ar=fiscal_year.name_ar,
        start_date=fiscal_year.start_date,
        end_date=fiscal_year.end_date,
        status=fiscal_year.status,
        periods=[_period_out(period) for period in periods],
    )


@router.get("/fiscal-years", response_model=list[FiscalYearOut], summary="Fiscal years")
async def read_fiscal_years(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[FiscalYearOut]:
    """Return the selected company's fiscal years with their periods."""
    fiscal_years = await service.list_fiscal_years(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    periods = await service.list_periods(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    return [
        _fiscal_year_out(
            fiscal_year,
            [period for period in periods if period.fiscal_year_id == fiscal_year.id],
        )
        for fiscal_year in fiscal_years
    ]


@router.post(
    "/fiscal-years/{fiscal_year_id}/close",
    response_model=FiscalYearCloseOut,
    summary="Close a fiscal year",
)
async def close_fiscal_year(
    fiscal_year_id: UUID,
    unit_of_work: UnitOfWorkDep,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("ledger.period.close"))],
    payload: PeriodAction,
) -> FiscalYearCloseOut:
    """Post the year-end closing entry, close its periods and mark the year closed."""
    result = await year_end.close_fiscal_year(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        fiscal_year_id=fiscal_year_id,
        reason=payload.reason,
    )
    periods = await service.list_periods(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    return FiscalYearCloseOut(
        fiscal_year=_fiscal_year_out(
            result.fiscal_year,
            [period for period in periods if period.fiscal_year_id == result.fiscal_year.id],
        ),
        closing_entry=(
            _entry_body(result.closing_entry) if result.closing_entry is not None else None
        ),
        periods_closed=result.periods_closed,
    )
