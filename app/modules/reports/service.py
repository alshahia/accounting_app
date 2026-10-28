"""Reporting queries (phase 0, day 8).

The trial balance is computed from posted journal lines only, so drafts and
unposted documents never appear. Amounts stay in integer minor units through the
query; rendering decides how to display them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.cashbank.models import CashTransfer, Cheque, Receipt, SupplierPayment
from app.modules.catalog.models import Customer, Item, Supplier
from app.modules.identity.models import AuditLog
from app.modules.inventory.models import StockMove, Warehouse
from app.modules.ledger.models import Account, JournalEntry, JournalLine
from app.modules.purchasing.models import PurchaseBill, PurchaseDebitNote
from app.modules.sales.models import SalesCreditNote, SalesInvoice
from app.shared.errors import AppError


@dataclass(frozen=True)
class TrialBalanceRow:
    """One account's debit, credit and net balance in minor units."""

    account_id: UUID
    code: str
    name_ar: str
    name_en: str | None
    account_type: str
    debit_minor: int
    credit_minor: int

    @property
    def balance_minor(self) -> int:
        return self.debit_minor - self.credit_minor


@dataclass(frozen=True)
class TrialBalance:
    """A trial balance for one company and date range."""

    start_date: date
    end_date: date
    rows: tuple[TrialBalanceRow, ...]

    @property
    def total_debit_minor(self) -> int:
        return sum(row.debit_minor for row in self.rows)

    @property
    def total_credit_minor(self) -> int:
        return sum(row.credit_minor for row in self.rows)

    @property
    def balanced(self) -> bool:
        return self.total_debit_minor == self.total_credit_minor


async def trial_balance(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    start_date: date,
    end_date: date,
) -> TrialBalance:
    """Return every account with its posted movement in the date range."""
    if start_date > end_date:
        raise AppError(
            code="report_range_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="The report start date must not be after its end date.",
        )
    accounts = (
        (
            await session.execute(
                select(Account)
                .where(Account.tenant_id == tenant_id, Account.company_id == company_id)
                .order_by(Account.code)
            )
        )
        .scalars()
        .all()
    )
    movement = {
        account_id: (int(debit), int(credit))
        for account_id, debit, credit in (
            await session.execute(
                select(
                    JournalLine.account_id,
                    func.coalesce(func.sum(JournalLine.debit_minor), 0),
                    func.coalesce(func.sum(JournalLine.credit_minor), 0),
                )
                .select_from(JournalLine)
                .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                .where(
                    JournalLine.tenant_id == tenant_id,
                    JournalLine.company_id == company_id,
                    JournalEntry.status == "posted",
                    JournalEntry.entry_date >= start_date,
                    JournalEntry.entry_date <= end_date,
                )
                .group_by(JournalLine.account_id)
            )
        ).all()
    }
    rows = tuple(
        TrialBalanceRow(
            account_id=account.id,
            code=account.code,
            name_ar=account.name_ar,
            name_en=account.name_en,
            account_type=account.account_type,
            debit_minor=movement.get(account.id, (0, 0))[0],
            credit_minor=movement.get(account.id, (0, 0))[1],
        )
        for account in accounts
    )
    return TrialBalance(start_date=start_date, end_date=end_date, rows=rows)


# Aging buckets follow the usual AR/AP convention: not-yet-due, then 30-day
# bands, then everything older than 90 days. They are a presentation choice, not
# a business rule, so the boundaries live here and every report uses the same
# function.
AGING_BUCKETS: tuple[str, ...] = ("current", "1-30", "31-60", "61-90", "90+")


def _aging_bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


@dataclass(frozen=True)
class AgingRow:
    """One open invoice or bill with its outstanding amount and age."""

    party_id: UUID
    party_code: str
    party_name_ar: str
    document_id: UUID
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


@dataclass(frozen=True)
class AgingReport:
    """An AR or AP aging report as of one business date."""

    as_of: date
    kind: str
    rows: tuple[AgingRow, ...]

    @property
    def total_base_outstanding_minor(self) -> int:
        return sum(row.base_outstanding_minor for row in self.rows)

    def bucket_totals(self) -> tuple[tuple[str, int], ...]:
        totals = dict.fromkeys(AGING_BUCKETS, 0)
        for row in self.rows:
            totals[row.bucket] += row.base_outstanding_minor
        return tuple((bucket, totals[bucket]) for bucket in AGING_BUCKETS)


def _validate_as_of(as_of: date) -> None:
    if as_of.year < 1900 or as_of.year > 9999:
        raise AppError(
            code="report_date_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="The report date is outside the supported calendar.",
        )


async def receivables_aging(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    as_of: date,
) -> AgingReport:
    """Return open customer invoices aged by due date as of one date."""
    _validate_as_of(as_of)
    from app.modules.cashbank.models import Receipt, ReceiptAllocation
    from app.modules.catalog.models import Customer
    from app.modules.sales.models import SalesInvoice

    posted_allocations = (
        select(
            ReceiptAllocation.invoice_id.label("invoice_id"),
            func.coalesce(func.sum(ReceiptAllocation.amount_minor), 0).label("allocated_minor"),
            func.coalesce(func.sum(ReceiptAllocation.base_amount_minor), 0).label("allocated_base"),
        )
        .join(Receipt, Receipt.id == ReceiptAllocation.receipt_id)
        .where(
            ReceiptAllocation.tenant_id == tenant_id,
            ReceiptAllocation.company_id == company_id,
            Receipt.tenant_id == tenant_id,
            Receipt.company_id == company_id,
            Receipt.status == "posted",
        )
        .group_by(ReceiptAllocation.invoice_id)
        .subquery()
    )
    statement = (
        select(
            SalesInvoice,
            Customer.code,
            Customer.name_ar,
            func.coalesce(posted_allocations.c.allocated_minor, 0),
            func.coalesce(posted_allocations.c.allocated_base, 0),
        )
        .join(
            Customer,
            (Customer.tenant_id == SalesInvoice.tenant_id)
            & (Customer.company_id == SalesInvoice.company_id)
            & (Customer.id == SalesInvoice.customer_id),
        )
        .outerjoin(
            posted_allocations,
            posted_allocations.c.invoice_id == SalesInvoice.id,
        )
        .where(
            SalesInvoice.tenant_id == tenant_id,
            SalesInvoice.company_id == company_id,
            SalesInvoice.status.in_(("posted", "part_paid", "paid")),
        )
        .order_by(SalesInvoice.invoice_date, SalesInvoice.number)
    )
    rows: list[AgingRow] = []
    for invoice, code, name_ar, allocated, allocated_base in (
        await session.execute(statement)
    ).all():
        outstanding = int(invoice.total_minor) - int(allocated)
        if outstanding <= 0:
            continue
        due = invoice.due_date or invoice.invoice_date
        days_overdue = (as_of - due).days
        rows.append(
            AgingRow(
                party_id=invoice.customer_id,
                party_code=code,
                party_name_ar=name_ar,
                document_id=invoice.id,
                document_number=invoice.number,
                document_date=invoice.invoice_date,
                due_date=due,
                currency=invoice.currency,
                total_minor=int(invoice.total_minor),
                allocated_minor=int(allocated),
                outstanding_minor=outstanding,
                base_outstanding_minor=max(0, int(invoice.base_total_minor) - int(allocated_base)),
                days_overdue=days_overdue,
                bucket=_aging_bucket(days_overdue),
            )
        )
    return AgingReport(as_of=as_of, kind="receivable", rows=tuple(rows))


async def payables_aging(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    as_of: date,
) -> AgingReport:
    """Return open supplier bills aged by due date as of one date."""
    _validate_as_of(as_of)
    from app.modules.cashbank.models import SupplierPayment, SupplierPaymentAllocation
    from app.modules.catalog.models import Supplier
    from app.modules.purchasing.models import PurchaseBill

    posted_allocations = (
        select(
            SupplierPaymentAllocation.bill_id.label("bill_id"),
            func.coalesce(func.sum(SupplierPaymentAllocation.amount_minor), 0).label(
                "allocated_minor"
            ),
            func.coalesce(func.sum(SupplierPaymentAllocation.base_amount_minor), 0).label(
                "allocated_base"
            ),
        )
        .join(SupplierPayment, SupplierPayment.id == SupplierPaymentAllocation.payment_id)
        .where(
            SupplierPaymentAllocation.tenant_id == tenant_id,
            SupplierPaymentAllocation.company_id == company_id,
            SupplierPayment.tenant_id == tenant_id,
            SupplierPayment.company_id == company_id,
            SupplierPayment.status == "posted",
        )
        .group_by(SupplierPaymentAllocation.bill_id)
        .subquery()
    )
    statement = (
        select(
            PurchaseBill,
            Supplier.code,
            Supplier.name_ar,
            func.coalesce(posted_allocations.c.allocated_minor, 0),
            func.coalesce(posted_allocations.c.allocated_base, 0),
        )
        .join(
            Supplier,
            (Supplier.tenant_id == PurchaseBill.tenant_id)
            & (Supplier.company_id == PurchaseBill.company_id)
            & (Supplier.id == PurchaseBill.supplier_id),
        )
        .outerjoin(
            posted_allocations,
            posted_allocations.c.bill_id == PurchaseBill.id,
        )
        .where(
            PurchaseBill.tenant_id == tenant_id,
            PurchaseBill.company_id == company_id,
            PurchaseBill.status.in_(("posted", "part_paid", "paid")),
        )
        .order_by(PurchaseBill.bill_date, PurchaseBill.number)
    )
    rows: list[AgingRow] = []
    for bill, code, name_ar, allocated, allocated_base in (await session.execute(statement)).all():
        outstanding = int(bill.total_minor) - int(allocated)
        if outstanding <= 0:
            continue
        due = bill.due_date or bill.bill_date
        days_overdue = (as_of - due).days
        rows.append(
            AgingRow(
                party_id=bill.supplier_id,
                party_code=code,
                party_name_ar=name_ar,
                document_id=bill.id,
                document_number=bill.number,
                document_date=bill.bill_date,
                due_date=due,
                currency=bill.currency,
                total_minor=int(bill.total_minor),
                allocated_minor=int(allocated),
                outstanding_minor=outstanding,
                base_outstanding_minor=max(0, int(bill.base_total_minor) - int(allocated_base)),
                days_overdue=days_overdue,
                bucket=_aging_bucket(days_overdue),
            )
        )
    return AgingReport(as_of=as_of, kind="payable", rows=tuple(rows))


POSTED_DOCUMENT_STATUSES = ("posted", "part_paid", "paid")


@dataclass(frozen=True)
class StatementRow:
    """One movement on a customer or supplier statement, in base minor units."""

    entry_date: date
    document_id: UUID
    document_type: str
    number: str
    description: str | None
    debit_minor: int
    credit_minor: int
    balance_minor: int


@dataclass(frozen=True)
class PartyStatement:
    """A customer or supplier statement with an opening and closing balance.

    Balances are in the company base currency. For a customer, a positive
    balance is owed by the customer; for a supplier, a positive balance is owed
    to the supplier. Customer advances (2300) and supplier advances (1400) are
    included in the movement so the statement shows the whole relationship, not
    only the receivable/payable account.
    """

    party_id: UUID
    party_name_ar: str
    party_name_en: str | None
    start_date: date
    end_date: date
    opening_balance_minor: int
    rows: tuple[StatementRow, ...]

    @property
    def total_debit_minor(self) -> int:
        return sum(row.debit_minor for row in self.rows)

    @property
    def total_credit_minor(self) -> int:
        return sum(row.credit_minor for row in self.rows)

    @property
    def closing_balance_minor(self) -> int:
        return self.opening_balance_minor + self.total_debit_minor - self.total_credit_minor


def _check_statement_dates(start_date: date, end_date: date) -> None:
    if start_date > end_date:
        raise AppError(
            code="report_range_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="The report start date must not be after its end date.",
        )


def _statement_rows(
    movements: list[tuple[date, UUID, str, str, str | None, int, int]],
    opening: int,
) -> list[StatementRow]:
    movements.sort(key=lambda item: (item[0], item[2]))
    balance = opening
    rows: list[StatementRow] = []
    for movement_date, document_id, document_type, number, description, debit, credit in movements:
        balance += debit - credit
        rows.append(
            StatementRow(
                entry_date=movement_date,
                document_id=document_id,
                document_type=document_type,
                number=number,
                description=description,
                debit_minor=debit,
                credit_minor=credit,
                balance_minor=balance,
            )
        )
    return rows


async def customer_statement(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    customer_id: UUID,
    start_date: date,
    end_date: date,
) -> PartyStatement:
    """Return one customer's posted invoices, credit notes and receipts."""
    _check_statement_dates(start_date, end_date)
    customer = (
        await session.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.company_id == company_id,
                Customer.id == customer_id,
            )
        )
    ).scalar_one_or_none()
    if customer is None:
        raise AppError(
            code="customer_not_found",
            title="Not found",
            status_code=404,
            detail="The customer does not exist in the selected company.",
        )
    invoices = (
        (
            await session.execute(
                select(SalesInvoice).where(
                    SalesInvoice.tenant_id == tenant_id,
                    SalesInvoice.company_id == company_id,
                    SalesInvoice.customer_id == customer_id,
                    SalesInvoice.status.in_(POSTED_DOCUMENT_STATUSES),
                    SalesInvoice.invoice_date <= end_date,
                )
            )
        )
        .scalars()
        .all()
    )
    credit_notes = (
        (
            await session.execute(
                select(SalesCreditNote).where(
                    SalesCreditNote.tenant_id == tenant_id,
                    SalesCreditNote.company_id == company_id,
                    SalesCreditNote.customer_id == customer_id,
                    SalesCreditNote.status == "posted",
                    SalesCreditNote.credit_note_date <= end_date,
                )
            )
        )
        .scalars()
        .all()
    )
    receipts = (
        (
            await session.execute(
                select(Receipt).where(
                    Receipt.tenant_id == tenant_id,
                    Receipt.company_id == company_id,
                    Receipt.customer_id == customer_id,
                    Receipt.status == "posted",
                    Receipt.receipt_date <= end_date,
                )
            )
        )
        .scalars()
        .all()
    )
    opening = 0
    movements: list[tuple[date, UUID, str, str, str | None, int, int]] = []
    for invoice in invoices:
        if invoice.invoice_date < start_date:
            opening += invoice.base_total_minor
        else:
            movements.append(
                (
                    invoice.invoice_date,
                    invoice.id,
                    "invoice",
                    invoice.number,
                    invoice.memo,
                    invoice.base_total_minor,
                    0,
                )
            )
    for note in credit_notes:
        if note.credit_note_date < start_date:
            opening -= note.base_total_minor
        else:
            movements.append(
                (
                    note.credit_note_date,
                    note.id,
                    "credit_note",
                    note.number,
                    note.memo,
                    0,
                    note.base_total_minor,
                )
            )
    for receipt in receipts:
        if receipt.receipt_date < start_date:
            opening -= receipt.base_amount_minor
        else:
            movements.append(
                (
                    receipt.receipt_date,
                    receipt.id,
                    "receipt",
                    receipt.number,
                    receipt.memo,
                    0,
                    receipt.base_amount_minor,
                )
            )
    return PartyStatement(
        party_id=customer.id,
        party_name_ar=customer.name_ar,
        party_name_en=customer.name_en,
        start_date=start_date,
        end_date=end_date,
        opening_balance_minor=opening,
        rows=tuple(_statement_rows(movements, opening)),
    )


async def supplier_statement(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    supplier_id: UUID,
    start_date: date,
    end_date: date,
) -> PartyStatement:
    """Return one supplier's posted bills, debit notes and payments."""
    _check_statement_dates(start_date, end_date)
    supplier = (
        await session.execute(
            select(Supplier).where(
                Supplier.tenant_id == tenant_id,
                Supplier.company_id == company_id,
                Supplier.id == supplier_id,
            )
        )
    ).scalar_one_or_none()
    if supplier is None:
        raise AppError(
            code="supplier_not_found",
            title="Not found",
            status_code=404,
            detail="The supplier does not exist in the selected company.",
        )
    bills = (
        (
            await session.execute(
                select(PurchaseBill).where(
                    PurchaseBill.tenant_id == tenant_id,
                    PurchaseBill.company_id == company_id,
                    PurchaseBill.supplier_id == supplier_id,
                    PurchaseBill.status.in_(POSTED_DOCUMENT_STATUSES),
                    PurchaseBill.bill_date <= end_date,
                )
            )
        )
        .scalars()
        .all()
    )
    debit_notes = (
        (
            await session.execute(
                select(PurchaseDebitNote).where(
                    PurchaseDebitNote.tenant_id == tenant_id,
                    PurchaseDebitNote.company_id == company_id,
                    PurchaseDebitNote.supplier_id == supplier_id,
                    PurchaseDebitNote.status == "posted",
                    PurchaseDebitNote.debit_note_date <= end_date,
                )
            )
        )
        .scalars()
        .all()
    )
    payments = (
        (
            await session.execute(
                select(SupplierPayment).where(
                    SupplierPayment.tenant_id == tenant_id,
                    SupplierPayment.company_id == company_id,
                    SupplierPayment.supplier_id == supplier_id,
                    SupplierPayment.status == "posted",
                    SupplierPayment.payment_date <= end_date,
                )
            )
        )
        .scalars()
        .all()
    )
    opening = 0
    movements: list[tuple[date, UUID, str, str, str | None, int, int]] = []
    for bill in bills:
        if bill.bill_date < start_date:
            opening += bill.base_total_minor
        else:
            movements.append(
                (
                    bill.bill_date,
                    bill.id,
                    "bill",
                    bill.number,
                    bill.memo,
                    bill.base_total_minor,
                    0,
                )
            )
    for note in debit_notes:
        if note.debit_note_date < start_date:
            opening -= note.base_total_minor
        else:
            movements.append(
                (
                    note.debit_note_date,
                    note.id,
                    "debit_note",
                    note.number,
                    note.memo,
                    0,
                    note.base_total_minor,
                )
            )
    for payment in payments:
        if payment.payment_date < start_date:
            opening -= payment.base_amount_minor
        else:
            movements.append(
                (
                    payment.payment_date,
                    payment.id,
                    "payment",
                    payment.number,
                    payment.memo,
                    0,
                    payment.base_amount_minor,
                )
            )
    return PartyStatement(
        party_id=supplier.id,
        party_name_ar=supplier.name_ar,
        party_name_en=supplier.name_en,
        start_date=start_date,
        end_date=end_date,
        opening_balance_minor=opening,
        rows=tuple(_statement_rows(movements, opening)),
    )


@dataclass(frozen=True)
class ProfitAndLossRow:
    """One revenue or expense account's movement over the period."""

    account_id: UUID
    code: str
    name_ar: str
    name_en: str | None
    account_type: str
    amount_minor: int


@dataclass(frozen=True)
class ProfitAndLoss:
    """Revenue and expense movements for one period, with the net result."""

    start_date: date
    end_date: date
    revenue: tuple[ProfitAndLossRow, ...]
    expenses: tuple[ProfitAndLossRow, ...]

    @property
    def total_revenue_minor(self) -> int:
        return sum(row.amount_minor for row in self.revenue)

    @property
    def total_expense_minor(self) -> int:
        return sum(row.amount_minor for row in self.expenses)

    @property
    def net_profit_minor(self) -> int:
        return self.total_revenue_minor - self.total_expense_minor


async def profit_and_loss(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    start_date: date,
    end_date: date,
) -> ProfitAndLoss:
    """Return revenue and expense movements in the date range."""
    _check_statement_dates(start_date, end_date)
    rows = (
        await session.execute(
            select(
                Account.id,
                Account.code,
                Account.name_ar,
                Account.name_en,
                Account.account_type,
                func.coalesce(func.sum(JournalLine.debit_minor), 0),
                func.coalesce(func.sum(JournalLine.credit_minor), 0),
            )
            .select_from(Account)
            .join(JournalLine, JournalLine.account_id == Account.id)
            .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(
                Account.tenant_id == tenant_id,
                Account.company_id == company_id,
                Account.account_type.in_(("revenue", "expense")),
                JournalEntry.status == "posted",
                JournalEntry.entry_date >= start_date,
                JournalEntry.entry_date <= end_date,
            )
            .group_by(
                Account.id, Account.code, Account.name_ar, Account.name_en, Account.account_type
            )
            .order_by(Account.code)
        )
    ).all()
    revenue: list[ProfitAndLossRow] = []
    expenses: list[ProfitAndLossRow] = []
    for account_id, code, name_ar, name_en, account_type, debit, credit in rows:
        movement = (
            int(credit) - int(debit) if account_type == "revenue" else int(debit) - int(credit)
        )
        row = ProfitAndLossRow(
            account_id=account_id,
            code=code,
            name_ar=name_ar,
            name_en=name_en,
            account_type=account_type,
            amount_minor=movement,
        )
        if account_type == "revenue":
            revenue.append(row)
        else:
            expenses.append(row)
    return ProfitAndLoss(
        start_date=start_date,
        end_date=end_date,
        revenue=tuple(revenue),
        expenses=tuple(expenses),
    )


@dataclass(frozen=True)
class BalanceSheetRow:
    """One asset, liability or equity account balance as of a date."""

    account_id: UUID
    code: str
    name_ar: str
    name_en: str | None
    account_type: str
    amount_minor: int


@dataclass(frozen=True)
class BalanceSheet:
    """Assets, liabilities and equity as of a date, with current earnings."""

    as_of: date
    assets: tuple[BalanceSheetRow, ...]
    liabilities: tuple[BalanceSheetRow, ...]
    equity: tuple[BalanceSheetRow, ...]
    current_earnings_minor: int

    @property
    def total_assets_minor(self) -> int:
        return sum(row.amount_minor for row in self.assets)

    @property
    def total_liabilities_minor(self) -> int:
        return sum(row.amount_minor for row in self.liabilities)

    @property
    def total_equity_minor(self) -> int:
        return sum(row.amount_minor for row in self.equity) + self.current_earnings_minor

    @property
    def balanced(self) -> bool:
        return self.total_assets_minor == self.total_liabilities_minor + self.total_equity_minor


async def balance_sheet(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    as_of: date,
) -> BalanceSheet:
    """Return the balance sheet as of a date, including unclosed earnings."""
    rows = (
        await session.execute(
            select(
                Account.id,
                Account.code,
                Account.name_ar,
                Account.name_en,
                Account.account_type,
                func.coalesce(func.sum(JournalLine.debit_minor - JournalLine.credit_minor), 0),
            )
            .select_from(Account)
            .join(JournalLine, JournalLine.account_id == Account.id)
            .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(
                Account.tenant_id == tenant_id,
                Account.company_id == company_id,
                JournalEntry.status == "posted",
                JournalEntry.entry_date <= as_of,
            )
            .group_by(
                Account.id, Account.code, Account.name_ar, Account.name_en, Account.account_type
            )
            .order_by(Account.code)
        )
    ).all()
    assets: list[BalanceSheetRow] = []
    liabilities: list[BalanceSheetRow] = []
    equity: list[BalanceSheetRow] = []
    current_earnings = 0
    for account_id, code, name_ar, name_en, account_type, balance in rows:
        amount = int(balance)
        if account_type == "asset":
            assets.append(BalanceSheetRow(account_id, code, name_ar, name_en, account_type, amount))
        elif account_type == "liability":
            liabilities.append(
                BalanceSheetRow(account_id, code, name_ar, name_en, account_type, -amount)
            )
        elif account_type == "equity":
            equity.append(
                BalanceSheetRow(account_id, code, name_ar, name_en, account_type, -amount)
            )
        else:
            current_earnings -= amount
    return BalanceSheet(
        as_of=as_of,
        assets=tuple(assets),
        liabilities=tuple(liabilities),
        equity=tuple(equity),
        current_earnings_minor=current_earnings,
    )


@dataclass(frozen=True)
class GeneralLedgerRow:
    """One posted journal line in an account's ledger."""

    entry_id: UUID
    entry_date: date
    entry_number: str
    description: str | None
    debit_minor: int
    credit_minor: int
    balance_minor: int


@dataclass(frozen=True)
class GeneralLedger:
    """One account's opening balance, movements and closing balance."""

    account_id: UUID
    account_code: str
    account_name_ar: str
    start_date: date
    end_date: date
    opening_balance_minor: int
    rows: tuple[GeneralLedgerRow, ...]

    @property
    def closing_balance_minor(self) -> int:
        if not self.rows:
            return self.opening_balance_minor
        return self.rows[-1].balance_minor


async def general_ledger(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    account_id: UUID,
    start_date: date,
    end_date: date,
) -> GeneralLedger:
    """Return one account's posted movements with a running balance."""
    _check_statement_dates(start_date, end_date)
    account = (
        await session.execute(
            select(Account).where(
                Account.tenant_id == tenant_id,
                Account.company_id == company_id,
                Account.id == account_id,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise AppError(
            code="account_not_found",
            title="Not found",
            status_code=404,
            detail="The account does not exist in the selected company.",
        )
    opening = int(
        (
            await session.execute(
                select(
                    func.coalesce(func.sum(JournalLine.debit_minor - JournalLine.credit_minor), 0)
                )
                .select_from(JournalLine)
                .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                .where(
                    JournalLine.tenant_id == tenant_id,
                    JournalLine.company_id == company_id,
                    JournalLine.account_id == account_id,
                    JournalEntry.status == "posted",
                    JournalEntry.entry_date < start_date,
                )
            )
        ).scalar_one()
    )
    lines = (
        await session.execute(
            select(
                JournalLine.entry_id,
                JournalEntry.entry_date,
                JournalEntry.number,
                JournalLine.description,
                JournalLine.debit_minor,
                JournalLine.credit_minor,
            )
            .select_from(JournalLine)
            .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .where(
                JournalLine.tenant_id == tenant_id,
                JournalLine.company_id == company_id,
                JournalLine.account_id == account_id,
                JournalEntry.status == "posted",
                JournalEntry.entry_date >= start_date,
                JournalEntry.entry_date <= end_date,
            )
            .order_by(JournalEntry.entry_date, JournalEntry.number, JournalLine.line_no)
        )
    ).all()
    balance = opening
    rows: list[GeneralLedgerRow] = []
    for entry_id, entry_date, entry_number, description, debit, credit in lines:
        balance += int(debit) - int(credit)
        rows.append(
            GeneralLedgerRow(
                entry_id=entry_id,
                entry_date=entry_date,
                entry_number=entry_number,
                description=description,
                debit_minor=int(debit),
                credit_minor=int(credit),
                balance_minor=balance,
            )
        )
    return GeneralLedger(
        account_id=account.id,
        account_code=account.code,
        account_name_ar=account.name_ar,
        start_date=start_date,
        end_date=end_date,
        opening_balance_minor=opening,
        rows=tuple(rows),
    )


@dataclass(frozen=True)
class InventoryValuationRow:
    """One item's quantity and value in the current stock position."""

    item_id: UUID
    code: str
    name_ar: str
    name_en: str | None
    unit_of_measure: str
    quantity_on_hand: Decimal
    stock_value_minor: int


@dataclass(frozen=True)
class InventoryValuation:
    """The current inventory position for one company."""

    rows: tuple[InventoryValuationRow, ...]

    @property
    def total_value_minor(self) -> int:
        return sum(row.stock_value_minor for row in self.rows)


async def inventory_valuation(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> InventoryValuation:
    """Return every item's quantity and weighted-average value."""
    items = (
        (
            await session.execute(
                select(Item)
                .where(
                    Item.tenant_id == tenant_id,
                    Item.company_id == company_id,
                    Item.is_active.is_(True),
                )
                .order_by(Item.code)
            )
        )
        .scalars()
        .all()
    )
    return InventoryValuation(
        rows=tuple(
            InventoryValuationRow(
                item_id=item.id,
                code=item.code,
                name_ar=item.name_ar,
                name_en=item.name_en,
                unit_of_measure=item.unit_of_measure,
                quantity_on_hand=item.quantity_on_hand,
                stock_value_minor=item.stock_value_minor,
            )
            for item in items
        )
    )


@dataclass(frozen=True)
class StockMovementRow:
    """One receipt or issue in the stock-movement report."""

    occurred_on: date
    move_type: str
    warehouse_id: UUID
    warehouse_code: str
    warehouse_name_ar: str
    warehouse_name_en: str | None
    item_code: str
    item_name_ar: str
    quantity: Decimal
    unit_cost_minor: int
    value_minor: int
    document_type: str
    document_id: UUID


@dataclass(frozen=True)
class StockMovementReport:
    """Stock movements in a date range."""

    start_date: date
    end_date: date
    rows: tuple[StockMovementRow, ...]

    @property
    def total_receipt_value_minor(self) -> int:
        return sum(row.value_minor for row in self.rows if row.move_type == "receipt")

    @property
    def total_issue_value_minor(self) -> int:
        return sum(row.value_minor for row in self.rows if row.move_type == "issue")


async def stock_movement(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    start_date: date,
    end_date: date,
) -> StockMovementReport:
    """Return stock receipts and issues in the date range."""
    _check_statement_dates(start_date, end_date)
    rows = (
        await session.execute(
            select(
                StockMove.occurred_on,
                StockMove.move_type,
                StockMove.warehouse_id,
                Warehouse.code.label("warehouse_code"),
                Warehouse.name_ar.label("warehouse_name_ar"),
                Warehouse.name_en.label("warehouse_name_en"),
                Item.code.label("item_code"),
                Item.name_ar.label("item_name_ar"),
                StockMove.quantity,
                StockMove.unit_cost_minor,
                StockMove.value_minor,
                StockMove.document_type,
                StockMove.document_id,
            )
            .select_from(StockMove)
            .join(Item, Item.id == StockMove.item_id)
            .join(Warehouse, Warehouse.id == StockMove.warehouse_id)
            .where(
                StockMove.tenant_id == tenant_id,
                StockMove.company_id == company_id,
                StockMove.occurred_on >= start_date,
                StockMove.occurred_on <= end_date,
            )
            .order_by(StockMove.occurred_on, StockMove.created_at)
        )
    ).all()
    records: list[StockMovementRow] = []
    for row in rows:
        records.append(
            StockMovementRow(
                occurred_on=row.occurred_on,
                move_type=row.move_type,
                warehouse_id=row.warehouse_id,
                warehouse_code=row.warehouse_code,
                warehouse_name_ar=row.warehouse_name_ar,
                warehouse_name_en=row.warehouse_name_en,
                item_code=row.item_code,
                item_name_ar=row.item_name_ar,
                quantity=row.quantity,
                unit_cost_minor=row.unit_cost_minor,
                value_minor=row.value_minor,
                document_type=row.document_type,
                document_id=row.document_id,
            )
        )
    return StockMovementReport(
        start_date=start_date,
        end_date=end_date,
        rows=tuple(records),
    )


@dataclass(frozen=True)
class TaxReturn:
    """Output and input VAT totals for a period, with the net position."""

    start_date: date
    end_date: date
    output_vat_minor: int
    output_credit_notes_minor: int
    input_vat_minor: int
    input_debit_notes_minor: int
    invoice_count: int
    bill_count: int

    @property
    def net_output_vat_minor(self) -> int:
        return self.output_vat_minor - self.output_credit_notes_minor

    @property
    def net_input_vat_minor(self) -> int:
        return self.input_vat_minor - self.input_debit_notes_minor

    @property
    def payable_minor(self) -> int:
        return self.net_output_vat_minor - self.net_input_vat_minor


async def tax_return(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    start_date: date,
    end_date: date,
) -> TaxReturn:
    """Return output and input VAT totals from posted documents in the range."""
    _check_statement_dates(start_date, end_date)

    output_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(SalesInvoice.base_tax_minor), 0),
                func.count(),
            ).where(
                SalesInvoice.tenant_id == tenant_id,
                SalesInvoice.company_id == company_id,
                SalesInvoice.status.in_(POSTED_DOCUMENT_STATUSES),
                SalesInvoice.invoice_date >= start_date,
                SalesInvoice.invoice_date <= end_date,
            )
        )
    ).one()
    output_credit_row = (
        await session.execute(
            select(func.coalesce(func.sum(SalesCreditNote.base_tax_minor), 0)).where(
                SalesCreditNote.tenant_id == tenant_id,
                SalesCreditNote.company_id == company_id,
                SalesCreditNote.status == "posted",
                SalesCreditNote.credit_note_date >= start_date,
                SalesCreditNote.credit_note_date <= end_date,
            )
        )
    ).scalar_one()
    input_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(PurchaseBill.base_tax_minor), 0),
                func.count(),
            ).where(
                PurchaseBill.tenant_id == tenant_id,
                PurchaseBill.company_id == company_id,
                PurchaseBill.status.in_(POSTED_DOCUMENT_STATUSES),
                PurchaseBill.bill_date >= start_date,
                PurchaseBill.bill_date <= end_date,
            )
        )
    ).one()
    input_debit_row = (
        await session.execute(
            select(func.coalesce(func.sum(PurchaseDebitNote.base_tax_minor), 0)).where(
                PurchaseDebitNote.tenant_id == tenant_id,
                PurchaseDebitNote.company_id == company_id,
                PurchaseDebitNote.status == "posted",
                PurchaseDebitNote.debit_note_date >= start_date,
                PurchaseDebitNote.debit_note_date <= end_date,
            )
        )
    ).scalar_one()
    output_vat, invoice_count = int(output_row[0]), int(output_row[1])
    output_credit_notes = int(output_credit_row)
    input_vat, bill_count = int(input_row[0]), int(input_row[1])
    input_debit_notes = int(input_debit_row)
    return TaxReturn(
        start_date=start_date,
        end_date=end_date,
        output_vat_minor=output_vat,
        output_credit_notes_minor=output_credit_notes,
        input_vat_minor=input_vat,
        input_debit_notes_minor=input_debit_notes,
        invoice_count=invoice_count,
        bill_count=bill_count,
    )


@dataclass(frozen=True)
class DocumentRegisterRow:
    """One business document in the register."""

    document_type: str
    document_id: UUID
    number: str
    document_date: date
    status: str
    currency: str
    amount_minor: int
    base_amount_minor: int


@dataclass(frozen=True)
class DocumentRegister:
    """Every business document touching a date range, oldest first."""

    start_date: date
    end_date: date
    rows: tuple[DocumentRegisterRow, ...]


REGISTER_DOCUMENT_TYPES = (
    "sales_invoice",
    "purchase_bill",
    "sales_credit_note",
    "purchase_debit_note",
    "receipt",
    "supplier_payment",
    "cash_transfer",
    "cheque",
)


async def _register_rows(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    document_type: str,
    model: type[Any],
    date_column: Any,
    amount_column: Any,
    base_column: Any,
    start_date: date,
    end_date: date,
) -> list[DocumentRegisterRow]:
    statement = (
        select(
            model.id,
            model.number,
            date_column,
            model.status,
            model.currency,
            amount_column,
            base_column,
        )
        .where(
            model.tenant_id == tenant_id,
            model.company_id == company_id,
            date_column >= start_date,
            date_column <= end_date,
        )
        .order_by(date_column, model.number)
    )
    records = cast("Sequence[tuple[Any, ...]]", (await session.execute(statement)).all())
    return [
        DocumentRegisterRow(
            document_type=document_type,
            document_id=cast("UUID", record[0]),
            number=str(record[1]),
            document_date=cast("date", record[2]),
            status=str(record[3]),
            currency=str(record[4]),
            amount_minor=int(cast("int", record[5])),
            base_amount_minor=int(cast("int", record[6])),
        )
        for record in records
    ]


async def document_register(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    start_date: date,
    end_date: date,
    document_type: str | None = None,
) -> DocumentRegister:
    """Return invoices, bills, notes, receipts, payments, transfers and cheques in range."""
    if start_date > end_date:
        raise AppError(
            code="report_range_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="The report start date must not be after its end date.",
        )
    selected = REGISTER_DOCUMENT_TYPES if document_type in (None, "") else (document_type,)
    unknown = [item for item in selected if item not in REGISTER_DOCUMENT_TYPES]
    if unknown:
        raise AppError(
            code="document_type_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail=f"Unknown document type: {unknown[0]}.",
        )
    rows: list[DocumentRegisterRow] = []
    specs = {
        "sales_invoice": (
            SalesInvoice,
            SalesInvoice.invoice_date,
            SalesInvoice.total_minor,
            SalesInvoice.base_total_minor,
        ),
        "purchase_bill": (
            PurchaseBill,
            PurchaseBill.bill_date,
            PurchaseBill.total_minor,
            PurchaseBill.base_total_minor,
        ),
        "sales_credit_note": (
            SalesCreditNote,
            SalesCreditNote.credit_note_date,
            SalesCreditNote.total_minor,
            SalesCreditNote.base_total_minor,
        ),
        "purchase_debit_note": (
            PurchaseDebitNote,
            PurchaseDebitNote.debit_note_date,
            PurchaseDebitNote.total_minor,
            PurchaseDebitNote.base_total_minor,
        ),
        "receipt": (Receipt, Receipt.receipt_date, Receipt.amount_minor, Receipt.base_amount_minor),
        "supplier_payment": (
            SupplierPayment,
            SupplierPayment.payment_date,
            SupplierPayment.amount_minor,
            SupplierPayment.base_amount_minor,
        ),
        "cash_transfer": (
            CashTransfer,
            CashTransfer.transfer_date,
            CashTransfer.amount_minor,
            CashTransfer.base_amount_minor,
        ),
        "cheque": (Cheque, Cheque.issue_date, Cheque.amount_minor, Cheque.base_amount_minor),
    }
    for kind in selected:
        model, date_column, amount_column, base_column = specs[kind]
        rows.extend(
            await _register_rows(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                document_type=kind,
                model=model,
                date_column=date_column,
                amount_column=amount_column,
                base_column=base_column,
                start_date=start_date,
                end_date=end_date,
            )
        )
    rows.sort(key=lambda row: (row.document_date, row.number))
    return DocumentRegister(start_date=start_date, end_date=end_date, rows=tuple(rows))


@dataclass(frozen=True)
class AuditTrailRow:
    """One append-only audit event in the trail report."""

    id: UUID
    created_at: datetime
    actor_user_id: UUID | None
    action: str
    entity_type: str | None
    entity_id: UUID | None
    request_id: str | None
    ip_address: str | None
    detail: dict[str, object] | None


@dataclass(frozen=True)
class AuditTrail:
    """Tenant audit events in a date range, newest first."""

    start_date: date
    end_date: date
    rows: tuple[AuditTrailRow, ...]


async def audit_trail(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    start_date: date,
    end_date: date,
    action: str | None = None,
    entity_type: str | None = None,
) -> AuditTrail:
    """Return the tenant audit trail for a date range, newest first."""
    _check_statement_dates(start_date, end_date)
    start_at = datetime.combine(start_date, time.min, tzinfo=UTC)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
    statement = select(AuditLog).where(
        AuditLog.tenant_id == tenant_id,
        AuditLog.created_at >= start_at,
        AuditLog.created_at < end_at,
    )
    if action is not None:
        statement = statement.where(AuditLog.action == action)
    if entity_type is not None:
        statement = statement.where(AuditLog.entity_type == entity_type)
    events = (
        (await session.execute(statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())))
        .scalars()
        .all()
    )
    return AuditTrail(
        start_date=start_date,
        end_date=end_date,
        rows=tuple(
            AuditTrailRow(
                id=event.id,
                created_at=event.created_at,
                actor_user_id=event.actor_user_id,
                action=event.action,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                request_id=event.request_id,
                ip_address=event.ip_address,
                detail=event.detail,
            )
            for event in events
        ),
    )
