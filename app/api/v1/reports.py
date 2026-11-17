"""Reporting endpoints: trial balance JSON, Arabic PDF and Excel (day 8).

The three views share one query and one date range, so the PDF and workbook a
user downloads always agree with the JSON on screen.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    PrincipalDep,
    TenantSessionDep,
    require_company_permission,
    require_permission,
)
from app.modules.identity.service import CompanyAccess, Principal
from app.modules.reports import render, service

router = APIRouter(prefix="/reports", tags=["reports"])

PDF_MEDIA_TYPE = "application/pdf"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ReportMeta(BaseModel):
    """Who generated a report, for which company, when, and with which filters."""

    generated_at: datetime
    generated_by: str
    company_id: UUID
    company_name_ar: str
    company_name_en: str | None
    filters: dict[str, str]


class ReportEnvelope(BaseModel):
    """Common report metadata carried by every JSON report response."""

    meta: ReportMeta


def _meta(
    principal: PrincipalDep,
    company: CompanyAccess,
    **filters: str,
) -> ReportMeta:
    """Build the report metadata shared by every report response."""
    return ReportMeta(
        generated_at=datetime.now(UTC),
        generated_by=principal.email,
        company_id=company.id,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        filters=filters,
    )


def _meta_lines(meta: ReportMeta) -> list[str]:
    """Return the human-readable metadata lines printed on a report download."""
    filters = ", ".join(f"{key}={value}" for key, value in meta.filters.items() if value != "")
    generated = meta.generated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return [
        f"وقت الإنشاء: {generated}",
        f"أنشئ بواسطة: {meta.generated_by}",
        f"الشركة: {meta.company_name_ar}",
        f"المرشحات: {filters or '—'}",
    ]


def _previous_period(start_date: date, end_date: date) -> tuple[date, date]:
    """Return the equally long period immediately before the requested one."""
    length = (end_date - start_date).days + 1
    comparison_end = start_date - timedelta(days=1)
    return comparison_end - timedelta(days=length - 1), comparison_end


def _previous_year(as_of: date) -> date:
    """Return the same calendar date one year earlier (29 February falls back)."""
    try:
        return as_of.replace(year=as_of.year - 1)
    except ValueError:
        return as_of.replace(year=as_of.year - 1, day=28)


async def _load_party_statement(
    session: AsyncSession,
    principal: Principal,
    company: CompanyAccess,
    *,
    party_kind: Literal["customer", "supplier"],
    party_id: UUID,
    start_date: date,
    end_date: date,
) -> service.PartyStatement:
    """Load one customer or supplier statement for a report endpoint."""
    if party_kind == "customer":
        return await service.customer_statement(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            customer_id=party_id,
            start_date=start_date,
            end_date=end_date,
        )
    return await service.supplier_statement(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        supplier_id=party_id,
        start_date=start_date,
        end_date=end_date,
    )


async def _comparison_statement(
    session: AsyncSession,
    principal: Principal,
    company: CompanyAccess,
    *,
    party_kind: Literal["customer", "supplier"],
    party_id: UUID,
    start_date: date,
    end_date: date,
) -> service.PartyStatement:
    """Load the equally long period immediately before the requested one."""
    compare_start, compare_end = _previous_period(start_date, end_date)
    return await _load_party_statement(
        session,
        principal,
        company,
        party_kind=party_kind,
        party_id=party_id,
        start_date=compare_start,
        end_date=compare_end,
    )


class TrialBalanceRowOut(BaseModel):
    """One account line in a trial balance, optionally with its prior period."""

    account_id: str
    code: str
    name_ar: str
    name_en: str | None
    account_type: str
    debit_minor: int
    credit_minor: int
    balance_minor: int
    compare_debit_minor: int | None = None
    compare_credit_minor: int | None = None
    compare_balance_minor: int | None = None


class TrialBalanceOut(ReportEnvelope):
    """A trial balance with totals and a balance check."""

    start_date: date
    end_date: date
    compare_start_date: date | None = None
    compare_end_date: date | None = None
    rows: list[TrialBalanceRowOut]
    total_debit_minor: int
    total_credit_minor: int
    compare_total_debit_minor: int | None = None
    compare_total_credit_minor: int | None = None
    balanced: bool


def _body(
    balance: service.TrialBalance,
    meta: ReportMeta,
    comparison: service.TrialBalance | None = None,
) -> TrialBalanceOut:
    prior = {row.account_id: row for row in comparison.rows} if comparison is not None else {}
    return TrialBalanceOut(
        meta=meta,
        start_date=balance.start_date,
        end_date=balance.end_date,
        compare_start_date=comparison.start_date if comparison is not None else None,
        compare_end_date=comparison.end_date if comparison is not None else None,
        rows=[
            TrialBalanceRowOut(
                account_id=str(row.account_id),
                code=row.code,
                name_ar=row.name_ar,
                name_en=row.name_en,
                account_type=row.account_type,
                debit_minor=row.debit_minor,
                credit_minor=row.credit_minor,
                balance_minor=row.balance_minor,
                compare_debit_minor=(
                    prior[row.account_id].debit_minor if row.account_id in prior else 0
                )
                if comparison is not None
                else None,
                compare_credit_minor=(
                    prior[row.account_id].credit_minor if row.account_id in prior else 0
                )
                if comparison is not None
                else None,
                compare_balance_minor=(
                    prior[row.account_id].balance_minor if row.account_id in prior else 0
                )
                if comparison is not None
                else None,
            )
            for row in balance.rows
        ],
        total_debit_minor=balance.total_debit_minor,
        total_credit_minor=balance.total_credit_minor,
        compare_total_debit_minor=(
            comparison.total_debit_minor if comparison is not None else None
        ),
        compare_total_credit_minor=(
            comparison.total_credit_minor if comparison is not None else None
        ),
        balanced=balance.balanced,
    )


def _balance_args(
    start_date: Annotated[date, Query(alias="start")],
    end_date: Annotated[date, Query(alias="end")],
) -> tuple[date, date]:
    return start_date, end_date


@router.get("/trial-balance", response_model=TrialBalanceOut, summary="Trial balance")
async def read_trial_balance(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> TrialBalanceOut:
    """Return the posted trial balance for a date range, optionally with the prior period."""
    start_date, end_date = period
    balance = await service.trial_balance(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        compare_start, compare_end = _previous_period(start_date, end_date)
        comparison = await service.trial_balance(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            start_date=compare_start,
            end_date=compare_end,
        )
    return _body(
        balance,
        _meta(
            principal,
            company,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            **({"compare": "true"} if compare else {}),
        ),
        comparison,
    )


@router.get(
    "/trial-balance.pdf",
    response_class=Response,
    summary="Trial balance as an Arabic PDF",
)
async def read_trial_balance_pdf(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> Response:
    """Return the trial balance as a PDF, optionally with the prior period."""
    start_date, end_date = period
    balance = await service.trial_balance(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        compare_start, compare_end = _previous_period(start_date, end_date)
        comparison = await service.trial_balance(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            start_date=compare_start,
            end_date=compare_end,
        )
    meta = _meta(
        principal,
        company,
        start=balance.start_date.isoformat(),
        end=balance.end_date.isoformat(),
        **({"compare": "true"} if compare else {}),
    )
    content = render.render_trial_balance_pdf(
        balance,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        comparison=comparison,
        meta_lines=_meta_lines(meta),
    )
    return Response(
        content=content,
        media_type=PDF_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="trial-balance.pdf"'},
    )


@router.get(
    "/trial-balance.xlsx",
    response_class=Response,
    summary="Trial balance as Excel",
)
async def read_trial_balance_xlsx(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> Response:
    """Return the trial balance as an Excel workbook, optionally with the prior period."""
    start_date, end_date = period
    balance = await service.trial_balance(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        compare_start, compare_end = _previous_period(start_date, end_date)
        comparison = await service.trial_balance(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            start_date=compare_start,
            end_date=compare_end,
        )
    meta = _meta(
        principal,
        company,
        start=balance.start_date.isoformat(),
        end=balance.end_date.isoformat(),
        **({"compare": "true"} if compare else {}),
    )
    content = render.render_trial_balance_xlsx(
        balance,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        comparison=comparison,
        meta_lines=_meta_lines(meta),
    )
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="trial-balance.xlsx"'},
    )


class AgingRowOut(BaseModel):
    """One open document line in an aging report."""

    party_id: str
    party_code: str
    party_name_ar: str
    document_id: str
    document_number: str
    document_date: date
    due_date: date
    currency: str
    total_minor: int
    allocated_minor: int
    outstanding_minor: int
    base_outstanding_minor: int
    days_overdue: int
    bucket: str


class AgingBucketOut(BaseModel):
    """One aging bucket and its base-currency total."""

    bucket: str
    total_base_minor: int


class AgingReportOut(ReportEnvelope):
    """AR or AP aging with rows and bucket totals."""

    as_of: date
    kind: str
    base_currency: str
    rows: list[AgingRowOut]
    buckets: list[AgingBucketOut]
    total_base_outstanding_minor: int


def _aging_body(
    report: service.AgingReport, base_currency: str, meta: ReportMeta
) -> AgingReportOut:
    return AgingReportOut(
        meta=meta,
        as_of=report.as_of,
        kind=report.kind,
        base_currency=base_currency,
        rows=[
            AgingRowOut(
                party_id=str(row.party_id),
                party_code=row.party_code,
                party_name_ar=row.party_name_ar,
                document_id=str(row.document_id),
                document_number=row.document_number,
                document_date=row.document_date,
                due_date=row.due_date,
                currency=row.currency,
                total_minor=row.total_minor,
                allocated_minor=row.allocated_minor,
                outstanding_minor=row.outstanding_minor,
                base_outstanding_minor=row.base_outstanding_minor,
                days_overdue=row.days_overdue,
                bucket=row.bucket,
            )
            for row in report.rows
        ],
        buckets=[
            AgingBucketOut(bucket=bucket, total_base_minor=total)
            for bucket, total in report.bucket_totals()
        ],
        total_base_outstanding_minor=report.total_base_outstanding_minor,
    )


@router.get("/ar-aging", response_model=AgingReportOut, summary="Accounts receivable aging")
async def read_ar_aging(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    as_of: Annotated[date, Query(alias="as_of")],
) -> AgingReportOut:
    """Return open customer invoices aged by due date."""
    report = await service.receivables_aging(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        as_of=as_of,
    )
    return _aging_body(
        report,
        company.base_currency,
        _meta(principal, company, as_of=as_of.isoformat(), kind="ar_aging"),
    )


@router.get("/ap-aging", response_model=AgingReportOut, summary="Accounts payable aging")
async def read_ap_aging(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    as_of: Annotated[date, Query(alias="as_of")],
) -> AgingReportOut:
    """Return open supplier bills aged by due date."""
    report = await service.payables_aging(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        as_of=as_of,
    )
    return _aging_body(
        report,
        company.base_currency,
        _meta(principal, company, as_of=as_of.isoformat(), kind="ap_aging"),
    )


class StatementRowOut(BaseModel):
    """One movement on a customer or supplier statement."""

    entry_date: date
    document_id: str
    document_type: str
    number: str
    description: str | None
    debit_minor: int
    credit_minor: int
    balance_minor: int


class PartyStatementOut(ReportEnvelope):
    """A customer or supplier statement with opening and closing balances."""

    party_id: str
    party_name_ar: str
    party_name_en: str | None
    start_date: date
    end_date: date
    opening_balance_minor: int
    rows: list[StatementRowOut]
    total_debit_minor: int
    total_credit_minor: int
    closing_balance_minor: int
    compare_start_date: date | None = None
    compare_end_date: date | None = None
    compare_opening_balance_minor: int | None = None
    compare_total_debit_minor: int | None = None
    compare_total_credit_minor: int | None = None
    compare_closing_balance_minor: int | None = None


def _statement_body(
    statement: service.PartyStatement,
    meta: ReportMeta,
    comparison: service.PartyStatement | None = None,
) -> PartyStatementOut:
    return PartyStatementOut(
        meta=meta,
        party_id=str(statement.party_id),
        party_name_ar=statement.party_name_ar,
        party_name_en=statement.party_name_en,
        start_date=statement.start_date,
        end_date=statement.end_date,
        opening_balance_minor=statement.opening_balance_minor,
        rows=[
            StatementRowOut(
                entry_date=row.entry_date,
                document_id=str(row.document_id),
                document_type=row.document_type,
                number=row.number,
                description=row.description,
                debit_minor=row.debit_minor,
                credit_minor=row.credit_minor,
                balance_minor=row.balance_minor,
            )
            for row in statement.rows
        ],
        total_debit_minor=statement.total_debit_minor,
        total_credit_minor=statement.total_credit_minor,
        closing_balance_minor=statement.closing_balance_minor,
        compare_start_date=comparison.start_date if comparison is not None else None,
        compare_end_date=comparison.end_date if comparison is not None else None,
        compare_opening_balance_minor=(
            comparison.opening_balance_minor if comparison is not None else None
        ),
        compare_total_debit_minor=(
            comparison.total_debit_minor if comparison is not None else None
        ),
        compare_total_credit_minor=(
            comparison.total_credit_minor if comparison is not None else None
        ),
        compare_closing_balance_minor=(
            comparison.closing_balance_minor if comparison is not None else None
        ),
    )


@router.get(
    "/customer-statement",
    response_model=PartyStatementOut,
    summary="Customer statement",
)
async def read_customer_statement(
    customer_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> PartyStatementOut:
    """Return a customer's posted invoices, credit notes and receipts."""
    start_date, end_date = period
    statement = await _load_party_statement(
        session,
        principal,
        company,
        party_kind="customer",
        party_id=customer_id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        comparison = await _comparison_statement(
            session,
            principal,
            company,
            party_kind="customer",
            party_id=customer_id,
            start_date=start_date,
            end_date=end_date,
        )
    return _statement_body(
        statement,
        _meta(
            principal,
            company,
            customer_id=str(customer_id),
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            **({"compare": "true"} if compare else {}),
        ),
        comparison,
    )


@router.get(
    "/supplier-statement",
    response_model=PartyStatementOut,
    summary="Supplier statement",
)
async def read_supplier_statement(
    supplier_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> PartyStatementOut:
    """Return a supplier's posted bills, debit notes and payments."""
    start_date, end_date = period
    statement = await _load_party_statement(
        session,
        principal,
        company,
        party_kind="supplier",
        party_id=supplier_id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        comparison = await _comparison_statement(
            session,
            principal,
            company,
            party_kind="supplier",
            party_id=supplier_id,
            start_date=start_date,
            end_date=end_date,
        )
    return _statement_body(
        statement,
        _meta(
            principal,
            company,
            supplier_id=str(supplier_id),
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            **({"compare": "true"} if compare else {}),
        ),
        comparison,
    )


class ProfitAndLossRowOut(BaseModel):
    """One revenue or expense line in the profit and loss, optionally with its prior period."""

    account_id: str
    code: str
    name_ar: str
    name_en: str | None
    account_type: str
    amount_minor: int
    compare_amount_minor: int | None = None


class ProfitAndLossOut(ReportEnvelope):
    """Revenue and expense movements with the net result and optional prior period."""

    start_date: date
    end_date: date
    compare_start_date: date | None = None
    compare_end_date: date | None = None
    revenue: list[ProfitAndLossRowOut]
    expenses: list[ProfitAndLossRowOut]
    total_revenue_minor: int
    total_expense_minor: int
    net_profit_minor: int
    compare_total_revenue_minor: int | None = None
    compare_total_expense_minor: int | None = None
    compare_net_profit_minor: int | None = None


def _pnl_rows(
    rows: tuple[service.ProfitAndLossRow, ...],
    comparison_rows: tuple[service.ProfitAndLossRow, ...] | None,
) -> list[ProfitAndLossRowOut]:
    prior = {row.account_id: row for row in comparison_rows} if comparison_rows is not None else {}
    result = [
        ProfitAndLossRowOut(
            account_id=str(row.account_id),
            code=row.code,
            name_ar=row.name_ar,
            name_en=row.name_en,
            account_type=row.account_type,
            amount_minor=row.amount_minor,
            compare_amount_minor=(
                prior[row.account_id].amount_minor if row.account_id in prior else 0
            )
            if comparison_rows is not None
            else None,
        )
        for row in rows
    ]
    if comparison_rows is not None:
        current = {row.account_id for row in rows}
        for row in comparison_rows:
            if row.account_id in current:
                continue
            result.append(
                ProfitAndLossRowOut(
                    account_id=str(row.account_id),
                    code=row.code,
                    name_ar=row.name_ar,
                    name_en=row.name_en,
                    account_type=row.account_type,
                    amount_minor=0,
                    compare_amount_minor=row.amount_minor,
                )
            )
    result.sort(key=lambda item: item.code)
    return result


@router.get(
    "/profit-and-loss",
    response_model=ProfitAndLossOut,
    summary="Profit and loss",
)
async def read_profit_and_loss(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> ProfitAndLossOut:
    """Return revenue, expenses and net profit for a period, with an optional comparison."""
    start_date, end_date = period
    report = await service.profit_and_loss(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        compare_start, compare_end = _previous_period(start_date, end_date)
        comparison = await service.profit_and_loss(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            start_date=compare_start,
            end_date=compare_end,
        )
    return ProfitAndLossOut(
        meta=_meta(
            principal,
            company,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            **({"compare": "true"} if compare else {}),
        ),
        start_date=report.start_date,
        end_date=report.end_date,
        compare_start_date=comparison.start_date if comparison is not None else None,
        compare_end_date=comparison.end_date if comparison is not None else None,
        revenue=_pnl_rows(report.revenue, comparison.revenue if comparison is not None else None),
        expenses=_pnl_rows(
            report.expenses, comparison.expenses if comparison is not None else None
        ),
        total_revenue_minor=report.total_revenue_minor,
        total_expense_minor=report.total_expense_minor,
        net_profit_minor=report.net_profit_minor,
        compare_total_revenue_minor=(
            comparison.total_revenue_minor if comparison is not None else None
        ),
        compare_total_expense_minor=(
            comparison.total_expense_minor if comparison is not None else None
        ),
        compare_net_profit_minor=(comparison.net_profit_minor if comparison is not None else None),
    )


class BalanceSheetRowOut(BaseModel):
    """One account balance on the balance sheet, optionally with its prior year."""

    account_id: str
    code: str
    name_ar: str
    name_en: str | None
    account_type: str
    amount_minor: int
    compare_amount_minor: int | None = None


class BalanceSheetOut(ReportEnvelope):
    """Assets, liabilities and equity with a balance check and optional prior year."""

    as_of: date
    compare_as_of: date | None = None
    assets: list[BalanceSheetRowOut]
    liabilities: list[BalanceSheetRowOut]
    equity: list[BalanceSheetRowOut]
    current_earnings_minor: int
    compare_current_earnings_minor: int | None = None
    total_assets_minor: int
    total_liabilities_minor: int
    total_equity_minor: int
    compare_total_assets_minor: int | None = None
    compare_total_liabilities_minor: int | None = None
    compare_total_equity_minor: int | None = None
    balanced: bool


def _bs_rows(
    rows: tuple[service.BalanceSheetRow, ...],
    comparison_rows: tuple[service.BalanceSheetRow, ...] | None,
) -> list[BalanceSheetRowOut]:
    prior = (
        {row.account_id: row for row in comparison_rows} if comparison_rows is not None else None
    )
    result = [
        BalanceSheetRowOut(
            account_id=str(row.account_id),
            code=row.code,
            name_ar=row.name_ar,
            name_en=row.name_en,
            account_type=row.account_type,
            amount_minor=row.amount_minor,
            compare_amount_minor=(
                (prior[row.account_id].amount_minor if row.account_id in prior else 0)
                if prior is not None
                else None
            ),
        )
        for row in rows
    ]
    if prior is not None and comparison_rows is not None:
        current = {row.account_id for row in rows}
        for row in comparison_rows:
            if row.account_id in current:
                continue
            result.append(
                BalanceSheetRowOut(
                    account_id=str(row.account_id),
                    code=row.code,
                    name_ar=row.name_ar,
                    name_en=row.name_en,
                    account_type=row.account_type,
                    amount_minor=0,
                    compare_amount_minor=row.amount_minor,
                )
            )
    result.sort(key=lambda item: item.code)
    return result


@router.get("/balance-sheet", response_model=BalanceSheetOut, summary="Balance sheet")
async def read_balance_sheet(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    as_of: Annotated[date, Query(alias="as_of")],
    compare: Annotated[bool, Query()] = False,
) -> BalanceSheetOut:
    """Return the balance sheet as of a date, optionally against the prior year."""
    report = await service.balance_sheet(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        as_of=as_of,
    )
    comparison = None
    if compare:
        comparison = await service.balance_sheet(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            as_of=_previous_year(as_of),
        )
    return BalanceSheetOut(
        meta=_meta(
            principal,
            company,
            as_of=as_of.isoformat(),
            **({"compare": "true"} if compare else {}),
        ),
        as_of=report.as_of,
        compare_as_of=comparison.as_of if comparison is not None else None,
        assets=_bs_rows(report.assets, comparison.assets if comparison is not None else None),
        liabilities=_bs_rows(
            report.liabilities, comparison.liabilities if comparison is not None else None
        ),
        equity=_bs_rows(report.equity, comparison.equity if comparison is not None else None),
        current_earnings_minor=report.current_earnings_minor,
        compare_current_earnings_minor=(
            comparison.current_earnings_minor if comparison is not None else None
        ),
        total_assets_minor=report.total_assets_minor,
        total_liabilities_minor=report.total_liabilities_minor,
        total_equity_minor=report.total_equity_minor,
        compare_total_assets_minor=(
            comparison.total_assets_minor if comparison is not None else None
        ),
        compare_total_liabilities_minor=(
            comparison.total_liabilities_minor if comparison is not None else None
        ),
        compare_total_equity_minor=(
            comparison.total_equity_minor if comparison is not None else None
        ),
        balanced=report.balanced,
    )


class GeneralLedgerRowOut(BaseModel):
    """One posted line in an account ledger."""

    entry_id: str
    entry_date: date
    entry_number: str
    description: str | None
    debit_minor: int
    credit_minor: int
    balance_minor: int


class GeneralLedgerOut(ReportEnvelope):
    """One account's opening balance, movements and closing balance."""

    account_id: str
    account_code: str
    account_name_ar: str
    start_date: date
    end_date: date
    opening_balance_minor: int
    rows: list[GeneralLedgerRowOut]
    closing_balance_minor: int


@router.get("/general-ledger", response_model=GeneralLedgerOut, summary="Account ledger")
async def read_general_ledger(
    account_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
) -> GeneralLedgerOut:
    """Return one account's posted movements with a running balance."""
    start_date, end_date = period
    ledger = await service.general_ledger(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        account_id=account_id,
        start_date=start_date,
        end_date=end_date,
    )
    return GeneralLedgerOut(
        meta=_meta(
            principal,
            company,
            account_id=str(account_id),
            start=start_date.isoformat(),
            end=end_date.isoformat(),
        ),
        account_id=str(ledger.account_id),
        account_code=ledger.account_code,
        account_name_ar=ledger.account_name_ar,
        start_date=ledger.start_date,
        end_date=ledger.end_date,
        opening_balance_minor=ledger.opening_balance_minor,
        rows=[
            GeneralLedgerRowOut(
                entry_id=str(row.entry_id),
                entry_date=row.entry_date,
                entry_number=row.entry_number,
                description=row.description,
                debit_minor=row.debit_minor,
                credit_minor=row.credit_minor,
                balance_minor=row.balance_minor,
            )
            for row in ledger.rows
        ],
        closing_balance_minor=ledger.closing_balance_minor,
    )


class InventoryValuationRowOut(BaseModel):
    """One item's quantity and value."""

    item_id: str
    code: str
    name_ar: str
    name_en: str | None
    unit_of_measure: str
    quantity_on_hand: str
    stock_value_minor: int


class InventoryValuationOut(ReportEnvelope):
    """Current inventory valuation with a total."""

    rows: list[InventoryValuationRowOut]
    total_value_minor: int


@router.get(
    "/inventory-valuation",
    response_model=InventoryValuationOut,
    summary="Inventory valuation",
)
async def read_inventory_valuation(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> InventoryValuationOut:
    """Return every item's quantity and weighted-average value."""
    report = await service.inventory_valuation(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    return InventoryValuationOut(
        meta=_meta(principal, company),
        rows=[
            InventoryValuationRowOut(
                item_id=str(row.item_id),
                code=row.code,
                name_ar=row.name_ar,
                name_en=row.name_en,
                unit_of_measure=row.unit_of_measure,
                quantity_on_hand=str(row.quantity_on_hand),
                stock_value_minor=row.stock_value_minor,
            )
            for row in report.rows
        ],
        total_value_minor=report.total_value_minor,
    )


class StockMovementRowOut(BaseModel):
    """One stock receipt or issue."""

    occurred_on: date
    move_type: str
    warehouse_id: str
    warehouse_code: str
    warehouse_name_ar: str
    warehouse_name_en: str | None
    item_code: str
    item_name_ar: str
    quantity: str
    unit_cost_minor: int
    value_minor: int
    document_type: str
    document_id: str


class StockMovementOut(ReportEnvelope):
    """Stock movements in a date range with totals."""

    start_date: date
    end_date: date
    rows: list[StockMovementRowOut]
    total_receipt_value_minor: int
    total_issue_value_minor: int


@router.get("/stock-movement", response_model=StockMovementOut, summary="Stock movement")
async def read_stock_movement(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
) -> StockMovementOut:
    """Return stock receipts and issues in a date range."""
    start_date, end_date = period
    report = await service.stock_movement(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    return StockMovementOut(
        meta=_meta(
            principal,
            company,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
        ),
        start_date=report.start_date,
        end_date=report.end_date,
        rows=[
            StockMovementRowOut(
                occurred_on=row.occurred_on,
                move_type=row.move_type,
                warehouse_id=str(row.warehouse_id),
                warehouse_code=row.warehouse_code,
                warehouse_name_ar=row.warehouse_name_ar,
                warehouse_name_en=row.warehouse_name_en,
                item_code=row.item_code,
                item_name_ar=row.item_name_ar,
                quantity=str(row.quantity),
                unit_cost_minor=row.unit_cost_minor,
                value_minor=row.value_minor,
                document_type=row.document_type,
                document_id=str(row.document_id),
            )
            for row in report.rows
        ],
        total_receipt_value_minor=report.total_receipt_value_minor,
        total_issue_value_minor=report.total_issue_value_minor,
    )


class DocumentRegisterRowOut(BaseModel):
    """One business document in the register."""

    document_type: str
    document_id: str
    number: str
    document_date: date
    status: str
    currency: str
    amount_minor: int
    base_amount_minor: int


class DocumentRegisterOut(ReportEnvelope):
    """Business documents in a date range."""

    start_date: date
    end_date: date
    rows: list[DocumentRegisterRowOut]
    total_amount_minor: int


def _register_out(report: service.DocumentRegister, meta: ReportMeta) -> DocumentRegisterOut:
    return DocumentRegisterOut(
        meta=meta,
        start_date=report.start_date,
        end_date=report.end_date,
        rows=[
            DocumentRegisterRowOut(
                document_type=row.document_type,
                document_id=str(row.document_id),
                number=row.number,
                document_date=row.document_date,
                status=row.status,
                currency=row.currency,
                amount_minor=row.amount_minor,
                base_amount_minor=row.base_amount_minor,
            )
            for row in report.rows
        ],
        total_amount_minor=sum(row.base_amount_minor for row in report.rows),
    )


@router.get(
    "/document-register",
    response_model=DocumentRegisterOut,
    summary="Document register",
)
async def read_document_register(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    document_type: Annotated[str | None, Query()] = None,
) -> DocumentRegisterOut:
    """Return invoices, bills, notes, receipts, payments, transfers and cheques in range."""
    start_date, end_date = period
    report = await service.document_register(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
        document_type=document_type,
    )
    return _register_out(
        report,
        _meta(
            principal,
            company,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            document_type=document_type or "",
        ),
    )


@router.get(
    "/document-register.pdf",
    response_class=Response,
    summary="Document register as an Arabic PDF",
)
async def read_document_register_pdf(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    document_type: Annotated[str | None, Query()] = None,
) -> Response:
    """Return the document register as a PDF."""
    start_date, end_date = period
    report = await service.document_register(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
        document_type=document_type,
    )
    meta = _meta(
        principal,
        company,
        start=report.start_date.isoformat(),
        end=report.end_date.isoformat(),
        document_type=document_type or "",
    )
    content = render.render_document_register_pdf(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return Response(
        content=content,
        media_type=PDF_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="document-register.pdf"'},
    )


@router.get(
    "/document-register.xlsx",
    response_class=Response,
    summary="Document register as an Excel workbook",
)
async def read_document_register_xlsx(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    document_type: Annotated[str | None, Query()] = None,
) -> Response:
    """Return the document register as a right-to-left Excel workbook."""
    start_date, end_date = period
    report = await service.document_register(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
        document_type=document_type,
    )
    meta = _meta(
        principal,
        company,
        start=report.start_date.isoformat(),
        end=report.end_date.isoformat(),
        document_type=document_type or "",
    )
    content = render.render_document_register_xlsx(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="document-register.xlsx"'},
    )


@router.get(
    "/general-ledger.pdf",
    response_class=Response,
    summary="Account ledger as an Arabic PDF",
)
async def read_general_ledger_pdf(
    account_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
) -> Response:
    """Return one account ledger as a PDF."""
    start_date, end_date = period
    ledger = await service.general_ledger(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        account_id=account_id,
        start_date=start_date,
        end_date=end_date,
    )
    meta = _meta(
        principal,
        company,
        account_id=str(ledger.account_id),
        start=ledger.start_date.isoformat(),
        end=ledger.end_date.isoformat(),
    )
    content = render.render_general_ledger_pdf(
        ledger,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _pdf(content, "general-ledger.pdf")


@router.get(
    "/general-ledger.xlsx",
    response_class=Response,
    summary="Account ledger as an Excel workbook",
)
async def read_general_ledger_xlsx(
    account_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
) -> Response:
    """Return one account ledger as a right-to-left Excel workbook."""
    start_date, end_date = period
    ledger = await service.general_ledger(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        account_id=account_id,
        start_date=start_date,
        end_date=end_date,
    )
    meta = _meta(
        principal,
        company,
        account_id=str(ledger.account_id),
        start=ledger.start_date.isoformat(),
        end=ledger.end_date.isoformat(),
    )
    content = render.render_general_ledger_xlsx(
        ledger,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _xlsx(content, "general-ledger.xlsx")


class TaxReturnOut(ReportEnvelope):
    """Output and input VAT totals with the net position."""

    start_date: date
    end_date: date
    output_vat_minor: int
    output_credit_notes_minor: int
    input_vat_minor: int
    input_debit_notes_minor: int
    net_output_vat_minor: int
    net_input_vat_minor: int
    payable_minor: int
    invoice_count: int
    bill_count: int


@router.get("/tax-return", response_model=TaxReturnOut, summary="VAT return")
async def read_tax_return(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
) -> TaxReturnOut:
    """Return output and input VAT totals from posted documents."""
    start_date, end_date = period
    report = await service.tax_return(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    return TaxReturnOut(
        meta=_meta(
            principal,
            company,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
        ),
        start_date=report.start_date,
        end_date=report.end_date,
        output_vat_minor=report.output_vat_minor,
        output_credit_notes_minor=report.output_credit_notes_minor,
        input_vat_minor=report.input_vat_minor,
        input_debit_notes_minor=report.input_debit_notes_minor,
        net_output_vat_minor=report.net_output_vat_minor,
        net_input_vat_minor=report.net_input_vat_minor,
        payable_minor=report.payable_minor,
        invoice_count=report.invoice_count,
        bill_count=report.bill_count,
    )


class AuditTrailRowOut(BaseModel):
    """One audit event in the report."""

    id: str
    created_at: datetime
    actor_user_id: str | None
    action: str
    entity_type: str | None
    entity_id: str | None
    request_id: str | None
    ip_address: str | None
    detail: dict[str, Any] | None


class AuditTrailOut(ReportEnvelope):
    """The tenant audit trail for a date range."""

    start_date: date
    end_date: date
    rows: list[AuditTrailRowOut]
    total: int


@router.get("/audit-trail", response_model=AuditTrailOut, summary="Audit trail report")
async def read_audit_trail(
    session: TenantSessionDep,
    principal: Annotated[Principal, Depends(require_permission("audit.view"))],
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    action: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
) -> AuditTrailOut:
    """Return the tenant audit trail for a date range."""
    start_date, end_date = period
    trail = await service.audit_trail(
        session,
        tenant_id=principal.tenant_id,
        start_date=start_date,
        end_date=end_date,
        action=action,
        entity_type=entity_type,
    )
    return AuditTrailOut(
        meta=_meta(
            principal,
            company,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            action=action or "",
            entity_type=entity_type or "",
        ),
        start_date=trail.start_date,
        end_date=trail.end_date,
        rows=[
            AuditTrailRowOut(
                id=str(row.id),
                created_at=row.created_at,
                actor_user_id=str(row.actor_user_id) if row.actor_user_id is not None else None,
                action=row.action,
                entity_type=row.entity_type,
                entity_id=str(row.entity_id) if row.entity_id is not None else None,
                request_id=row.request_id,
                ip_address=row.ip_address,
                detail=row.detail,
            )
            for row in trail.rows
        ],
        total=len(trail.rows),
    )


@router.get(
    "/audit-trail.pdf",
    response_class=Response,
    summary="Audit trail as an Arabic PDF",
)
async def read_audit_trail_pdf(
    session: TenantSessionDep,
    principal: Annotated[Principal, Depends(require_permission("audit.view"))],
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    action: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
) -> Response:
    """Return the audit trail as a PDF."""
    start_date, end_date = period
    trail = await service.audit_trail(
        session,
        tenant_id=principal.tenant_id,
        start_date=start_date,
        end_date=end_date,
        action=action,
        entity_type=entity_type,
    )
    meta = _meta(
        principal,
        company,
        start=trail.start_date.isoformat(),
        end=trail.end_date.isoformat(),
        action=action or "",
        entity_type=entity_type or "",
    )
    content = render.render_audit_trail_pdf(
        trail,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _pdf(content, "audit-trail.pdf")


@router.get(
    "/audit-trail.xlsx",
    response_class=Response,
    summary="Audit trail as an Excel workbook",
)
async def read_audit_trail_xlsx(
    session: TenantSessionDep,
    principal: Annotated[Principal, Depends(require_permission("audit.view"))],
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    action: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
) -> Response:
    """Return the audit trail as a right-to-left Excel workbook."""
    start_date, end_date = period
    trail = await service.audit_trail(
        session,
        tenant_id=principal.tenant_id,
        start_date=start_date,
        end_date=end_date,
        action=action,
        entity_type=entity_type,
    )
    meta = _meta(
        principal,
        company,
        start=trail.start_date.isoformat(),
        end=trail.end_date.isoformat(),
        action=action or "",
        entity_type=entity_type or "",
    )
    content = render.render_audit_trail_xlsx(
        trail,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _xlsx(content, "audit-trail.xlsx")


@router.get(
    "/inventory-valuation.pdf",
    response_class=Response,
    summary="Inventory valuation as an Arabic PDF",
)
async def read_inventory_valuation_pdf(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> Response:
    """Return the inventory valuation as a PDF."""
    report = await service.inventory_valuation(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    meta = _meta(principal, company)
    content = render.render_inventory_valuation_pdf(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _pdf(content, "inventory-valuation.pdf")


@router.get(
    "/inventory-valuation.xlsx",
    response_class=Response,
    summary="Inventory valuation as an Excel workbook",
)
async def read_inventory_valuation_xlsx(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> Response:
    """Return the inventory valuation as a right-to-left Excel workbook."""
    report = await service.inventory_valuation(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    meta = _meta(principal, company)
    content = render.render_inventory_valuation_xlsx(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _xlsx(content, "inventory-valuation.xlsx")


@router.get(
    "/stock-movement.pdf",
    response_class=Response,
    summary="Stock movement as an Arabic PDF",
)
async def read_stock_movement_pdf(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
) -> Response:
    """Return the stock movement report as a PDF."""
    start_date, end_date = period
    report = await service.stock_movement(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    meta = _meta(
        principal, company, start=report.start_date.isoformat(), end=report.end_date.isoformat()
    )
    content = render.render_stock_movement_pdf(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _pdf(content, "stock-movement.pdf")


@router.get(
    "/stock-movement.xlsx",
    response_class=Response,
    summary="Stock movement as an Excel workbook",
)
async def read_stock_movement_xlsx(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
) -> Response:
    """Return the stock movement report as a right-to-left Excel workbook."""
    start_date, end_date = period
    report = await service.stock_movement(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    meta = _meta(
        principal, company, start=report.start_date.isoformat(), end=report.end_date.isoformat()
    )
    content = render.render_stock_movement_xlsx(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _xlsx(content, "stock-movement.xlsx")


@router.get(
    "/tax-return.pdf",
    response_class=Response,
    summary="VAT return as an Arabic PDF",
)
async def read_tax_return_pdf(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
) -> Response:
    """Return the VAT return summary as a PDF."""
    start_date, end_date = period
    report = await service.tax_return(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    meta = _meta(
        principal, company, start=report.start_date.isoformat(), end=report.end_date.isoformat()
    )
    content = render.render_tax_return_pdf(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _pdf(content, "tax-return.pdf")


@router.get(
    "/tax-return.xlsx",
    response_class=Response,
    summary="VAT return as an Excel workbook",
)
async def read_tax_return_xlsx(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
) -> Response:
    """Return the VAT return summary as a right-to-left Excel workbook."""
    start_date, end_date = period
    report = await service.tax_return(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    meta = _meta(
        principal, company, start=report.start_date.isoformat(), end=report.end_date.isoformat()
    )
    content = render.render_tax_return_xlsx(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _xlsx(content, "tax-return.xlsx")


def _attachment(content: bytes, media_type: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _pdf(content: bytes, filename: str) -> Response:
    return _attachment(content, PDF_MEDIA_TYPE, filename)


def _xlsx(content: bytes, filename: str) -> Response:
    return _attachment(content, XLSX_MEDIA_TYPE, filename)


@router.get("/profit-and-loss.pdf", response_class=Response, summary="P&L as Arabic PDF")
async def read_profit_and_loss_pdf(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> Response:
    """Return the profit and loss statement as a PDF, optionally with the prior period."""
    start_date, end_date = period
    report = await service.profit_and_loss(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        compare_start, compare_end = _previous_period(start_date, end_date)
        comparison = await service.profit_and_loss(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            start_date=compare_start,
            end_date=compare_end,
        )
    meta = _meta(
        principal,
        company,
        start=report.start_date.isoformat(),
        end=report.end_date.isoformat(),
        **({"compare": "true"} if compare else {}),
    )
    content = render.render_profit_and_loss_pdf(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        comparison=comparison,
        meta_lines=_meta_lines(meta),
    )
    return _pdf(content, "profit-and-loss.pdf")


@router.get(
    "/profit-and-loss.xlsx",
    response_class=Response,
    summary="P&L as Excel",
)
async def read_profit_and_loss_xlsx(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> Response:
    """Return the profit and loss statement as a workbook, optionally with the prior period."""
    start_date, end_date = period
    report = await service.profit_and_loss(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        compare_start, compare_end = _previous_period(start_date, end_date)
        comparison = await service.profit_and_loss(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            start_date=compare_start,
            end_date=compare_end,
        )
    meta = _meta(
        principal,
        company,
        start=report.start_date.isoformat(),
        end=report.end_date.isoformat(),
        **({"compare": "true"} if compare else {}),
    )
    content = render.render_profit_and_loss_xlsx(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        comparison=comparison,
        meta_lines=_meta_lines(meta),
    )
    return _xlsx(content, "profit-and-loss.xlsx")


@router.get(
    "/balance-sheet.pdf",
    response_class=Response,
    summary="Balance sheet as an Arabic PDF",
)
async def read_balance_sheet_pdf(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    as_of: Annotated[date, Query(alias="as_of")],
    compare: Annotated[bool, Query()] = False,
) -> Response:
    """Return the balance sheet as a PDF, optionally against the prior year."""
    report = await service.balance_sheet(
        session, tenant_id=principal.tenant_id, company_id=company.id, as_of=as_of
    )
    comparison = None
    if compare:
        comparison = await service.balance_sheet(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            as_of=_previous_year(as_of),
        )
    meta = _meta(
        principal,
        company,
        as_of=report.as_of.isoformat(),
        **({"compare": "true"} if compare else {}),
    )
    content = render.render_balance_sheet_pdf(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        comparison=comparison,
        meta_lines=_meta_lines(meta),
    )
    return _pdf(content, "balance-sheet.pdf")


@router.get(
    "/balance-sheet.xlsx",
    response_class=Response,
    summary="Balance sheet as Excel",
)
async def read_balance_sheet_xlsx(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    as_of: Annotated[date, Query(alias="as_of")],
    compare: Annotated[bool, Query()] = False,
) -> Response:
    """Return the balance sheet as a workbook, optionally against the prior year."""
    report = await service.balance_sheet(
        session, tenant_id=principal.tenant_id, company_id=company.id, as_of=as_of
    )
    comparison = None
    if compare:
        comparison = await service.balance_sheet(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            as_of=_previous_year(as_of),
        )
    meta = _meta(
        principal,
        company,
        as_of=report.as_of.isoformat(),
        **({"compare": "true"} if compare else {}),
    )
    content = render.render_balance_sheet_xlsx(
        report,
        company_name_ar=company.name_ar,
        company_name_en=company.name_en,
        comparison=comparison,
        meta_lines=_meta_lines(meta),
    )
    return _xlsx(content, "balance-sheet.xlsx")


@router.get(
    "/customer-statement.pdf",
    response_class=Response,
    summary="Customer statement as an Arabic PDF",
)
async def read_customer_statement_pdf(
    customer_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> Response:
    """Return a customer statement as an Arabic PDF, optionally with the prior period."""
    start_date, end_date = period
    statement = await _load_party_statement(
        session,
        principal,
        company,
        party_kind="customer",
        party_id=customer_id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        comparison = await _comparison_statement(
            session,
            principal,
            company,
            party_kind="customer",
            party_id=customer_id,
            start_date=start_date,
            end_date=end_date,
        )
    meta = _meta(
        principal,
        company,
        customer_id=str(customer_id),
        start=statement.start_date.isoformat(),
        end=statement.end_date.isoformat(),
        **({"compare": "true"} if compare else {}),
    )
    content = render.render_statement_pdf(
        statement,
        company_name_ar=company.name_ar,
        title_ar="كشف حساب عميل",
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
        comparison=comparison,
    )
    return _pdf(content, "customer-statement.pdf")


@router.get("/customer-statement.xlsx", response_class=Response, summary="Customer statement Excel")
async def read_customer_statement_xlsx(
    customer_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> Response:
    """Return a customer statement as a right-to-left workbook, optionally with the prior period."""
    start_date, end_date = period
    statement = await _load_party_statement(
        session,
        principal,
        company,
        party_kind="customer",
        party_id=customer_id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        comparison = await _comparison_statement(
            session,
            principal,
            company,
            party_kind="customer",
            party_id=customer_id,
            start_date=start_date,
            end_date=end_date,
        )
    meta = _meta(
        principal,
        company,
        customer_id=str(customer_id),
        start=statement.start_date.isoformat(),
        end=statement.end_date.isoformat(),
        **({"compare": "true"} if compare else {}),
    )
    content = render.render_statement_xlsx(
        statement,
        company_name_ar=company.name_ar,
        title_ar="كشف حساب عميل",
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
        comparison=comparison,
    )
    return _xlsx(content, "customer-statement.xlsx")


@router.get("/supplier-statement.pdf", response_class=Response, summary="Supplier statement PDF")
async def read_supplier_statement_pdf(
    supplier_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> Response:
    """Return a supplier statement as an Arabic PDF, optionally with the prior period."""
    start_date, end_date = period
    statement = await _load_party_statement(
        session,
        principal,
        company,
        party_kind="supplier",
        party_id=supplier_id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        comparison = await _comparison_statement(
            session,
            principal,
            company,
            party_kind="supplier",
            party_id=supplier_id,
            start_date=start_date,
            end_date=end_date,
        )
    meta = _meta(
        principal,
        company,
        supplier_id=str(supplier_id),
        start=statement.start_date.isoformat(),
        end=statement.end_date.isoformat(),
        **({"compare": "true"} if compare else {}),
    )
    content = render.render_statement_pdf(
        statement,
        company_name_ar=company.name_ar,
        title_ar="كشف حساب مورد",
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
        comparison=comparison,
    )
    return _pdf(content, "supplier-statement.pdf")


@router.get("/supplier-statement.xlsx", response_class=Response, summary="Supplier statement Excel")
async def read_supplier_statement_xlsx(
    supplier_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    period: Annotated[tuple[date, date], Depends(_balance_args)],
    compare: Annotated[bool, Query()] = False,
) -> Response:
    """Return a supplier statement as a right-to-left workbook, optionally with the prior period."""
    start_date, end_date = period
    statement = await _load_party_statement(
        session,
        principal,
        company,
        party_kind="supplier",
        party_id=supplier_id,
        start_date=start_date,
        end_date=end_date,
    )
    comparison = None
    if compare:
        comparison = await _comparison_statement(
            session,
            principal,
            company,
            party_kind="supplier",
            party_id=supplier_id,
            start_date=start_date,
            end_date=end_date,
        )
    meta = _meta(
        principal,
        company,
        supplier_id=str(supplier_id),
        start=statement.start_date.isoformat(),
        end=statement.end_date.isoformat(),
        **({"compare": "true"} if compare else {}),
    )
    content = render.render_statement_xlsx(
        statement,
        company_name_ar=company.name_ar,
        title_ar="كشف حساب مورد",
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
        comparison=comparison,
    )
    return _xlsx(content, "supplier-statement.xlsx")


@router.get("/ar-aging.pdf", response_class=Response, summary="AR aging as Arabic PDF")
async def read_ar_aging_pdf(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    as_of: Annotated[date, Query(alias="as_of")],
) -> Response:
    """Return receivable aging as an Arabic PDF."""
    report = await service.receivables_aging(
        session, tenant_id=principal.tenant_id, company_id=company.id, as_of=as_of
    )
    meta = _meta(principal, company, as_of=report.as_of.isoformat(), kind="ar_aging")
    content = render.render_aging_pdf(
        report,
        company_name_ar=company.name_ar,
        title_ar="أعمار المدينين",
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _pdf(content, "ar-aging.pdf")


@router.get("/ar-aging.xlsx", response_class=Response, summary="AR aging as Excel")
async def read_ar_aging_xlsx(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    as_of: Annotated[date, Query(alias="as_of")],
) -> Response:
    """Return receivable aging as a right-to-left workbook."""
    report = await service.receivables_aging(
        session, tenant_id=principal.tenant_id, company_id=company.id, as_of=as_of
    )
    meta = _meta(principal, company, as_of=report.as_of.isoformat(), kind="ar_aging")
    content = render.render_aging_xlsx(
        report,
        company_name_ar=company.name_ar,
        title_ar="أعمار المدينين",
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _xlsx(content, "ar-aging.xlsx")


@router.get("/ap-aging.pdf", response_class=Response, summary="AP aging as Arabic PDF")
async def read_ap_aging_pdf(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    as_of: Annotated[date, Query(alias="as_of")],
) -> Response:
    """Return payable aging as an Arabic PDF."""
    report = await service.payables_aging(
        session, tenant_id=principal.tenant_id, company_id=company.id, as_of=as_of
    )
    meta = _meta(principal, company, as_of=report.as_of.isoformat(), kind="ap_aging")
    content = render.render_aging_pdf(
        report,
        company_name_ar=company.name_ar,
        title_ar="أعمار الدائنين",
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _pdf(content, "ap-aging.pdf")


@router.get("/ap-aging.xlsx", response_class=Response, summary="AP aging as Excel")
async def read_ap_aging_xlsx(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    as_of: Annotated[date, Query(alias="as_of")],
) -> Response:
    """Return payable aging as a right-to-left workbook."""
    report = await service.payables_aging(
        session, tenant_id=principal.tenant_id, company_id=company.id, as_of=as_of
    )
    meta = _meta(principal, company, as_of=report.as_of.isoformat(), kind="ap_aging")
    content = render.render_aging_xlsx(
        report,
        company_name_ar=company.name_ar,
        title_ar="أعمار الدائنين",
        company_name_en=company.name_en,
        meta_lines=_meta_lines(meta),
    )
    return _xlsx(content, "ap-aging.xlsx")
