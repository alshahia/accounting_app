"""Cash/bank master data, customer receipts and supplier payments (phase 1).

A receipt or payment is created and posted inside one retried transaction. The
posting keeps the settled document's historical base value: the receivable or
payable is relieved at the invoice/bill exchange rate, the money moves at the
receipt/payment rate, and the difference goes to exchange gain or loss. Money
that is not allocated stays on the customer/supplier advance account instead of
being hidden in a suspense balance.

Allocation is only within one currency. Cross-currency settlement and realised
FX on a different document currency is deliberately refused until the currency
conversion rules are reviewed by an accountant; refusing is safer than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.cashbank.models import (
    BankStatementLine,
    CashBankAccount,
    CashTransfer,
    Cheque,
    ChequeAllocation,
    ChequeEvent,
    Receipt,
    ReceiptAllocation,
    SupplierPayment,
    SupplierPaymentAllocation,
)
from app.modules.catalog.models import Customer, Supplier, TaxCode
from app.modules.catalog.tax import rate_on as tax_rate_on
from app.modules.ledger.mapping import resolve_posting_accounts
from app.modules.ledger.models import Account, AccountingPeriod, JournalLine
from app.modules.ledger.service import (
    EntryResult,
    JournalLineInput,
    post_entry_in_session,
)
from app.modules.purchasing.models import PurchaseBill
from app.modules.sales.models import SalesInvoice
from app.shared.errors import AppError
from app.shared.numbering import allocate_document_number
from app.shared.unit_of_work import UnitOfWork

RECEIPT_SCOPE = "receipt"
RECEIPT_PREFIX = "RC"
PAYMENT_SCOPE = "supplier_payment"
PAYMENT_PREFIX = "PY"

AR_ACCOUNT = "accounts_receivable"
AP_ACCOUNT = "accounts_payable"
CUSTOMER_ADVANCES_ACCOUNT = "customer_advances"
SUPPLIER_ADVANCES_ACCOUNT = "supplier_advances"
FX_GAIN_ACCOUNT = "fx_gain"
FX_LOSS_ACCOUNT = "fx_loss"
WITHHOLDING_RECEIVABLE_ACCOUNT = "withholding_receivable"
WITHHOLDING_PAYABLE_ACCOUNT = "withholding_payable"


@dataclass(frozen=True)
class AllocationInput:
    """An amount in the settled document's currency."""

    document_id: UUID
    amount_minor: int


@dataclass(frozen=True)
class CashBankAccountInput:
    """A cash box or bank account to create."""

    company_id: UUID
    code: str
    name_ar: str
    kind: str
    ledger_account_id: UUID
    currency: str = "USD"
    name_en: str | None = None


@dataclass(frozen=True)
class ReceiptInput:
    """A customer receipt with optional invoice allocations."""

    company_id: UUID
    customer_id: UUID
    cash_bank_account_id: UUID
    receipt_date: date
    amount_minor: int
    allocations: tuple[AllocationInput, ...] = ()
    currency: str = "USD"
    exchange_rate: Decimal = Decimal("1")
    memo: str | None = None
    number: str | None = None
    post: bool = False
    withholding_tax_code_id: UUID | None = None
    withholding_base_minor: int | None = None


@dataclass(frozen=True)
class PaymentInput:
    """A supplier payment with optional bill allocations."""

    company_id: UUID
    supplier_id: UUID
    cash_bank_account_id: UUID
    payment_date: date
    amount_minor: int
    allocations: tuple[AllocationInput, ...] = ()
    currency: str = "USD"
    exchange_rate: Decimal = Decimal("1")
    memo: str | None = None
    number: str | None = None
    post: bool = False
    withholding_tax_code_id: UUID | None = None
    withholding_base_minor: int | None = None


@dataclass(frozen=True)
class ReceiptAllocationPlan:
    """One validated allocation and the base amounts posting will use."""

    invoice: SalesInvoice
    amount_minor: int
    base_amount_minor: int
    settlement_base_minor: int
    allocated_before_minor: int


@dataclass(frozen=True)
class PaymentAllocationPlan:
    """One validated bill allocation and the base amounts posting will use."""

    bill: PurchaseBill
    amount_minor: int
    base_amount_minor: int
    settlement_base_minor: int
    allocated_before_minor: int


@dataclass(frozen=True)
class ReceiptResult:
    """A receipt, its allocations and the posting entry (if posted)."""

    receipt: Receipt
    allocations: tuple[ReceiptAllocation, ...]
    journal_entry: EntryResult | None


@dataclass(frozen=True)
class PaymentResult:
    """A supplier payment, its allocations and the posting entry (if posted)."""

    payment: SupplierPayment
    allocations: tuple[SupplierPaymentAllocation, ...]
    journal_entry: EntryResult | None


@dataclass(frozen=True)
class AccountResult:
    """A created cash/bank account."""

    account: CashBankAccount


def _bad(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Unprocessable entity", status_code=422, detail=detail)


def _conflict(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Conflict", status_code=409, detail=detail)


def _not_found(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Not found", status_code=404, detail=detail)


def _minor(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _validate_currency(currency: str) -> str:
    normalized = currency.upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise _bad("currency_invalid", "The currency must be a three-letter ISO code.")
    return normalized


async def load_withholding_code(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    tax_code_id: UUID,
) -> TaxCode:
    """Load a withholding tax code and reject codes of any other kind."""
    tax_code = (
        await session.execute(
            select(TaxCode).where(
                TaxCode.tenant_id == tenant_id,
                TaxCode.company_id == company_id,
                TaxCode.id == tax_code_id,
            )
        )
    ).scalar_one_or_none()
    if tax_code is None:
        raise _not_found("tax_code_not_found", "The withholding tax code does not exist.")
    if not tax_code.is_active:
        raise _bad("tax_code_inactive", f"Tax code {tax_code.code} is not active.")
    if tax_code.kind != "withholding":
        raise _bad(
            "tax_code_not_withholding",
            f"Tax code {tax_code.code} is not a withholding code.",
        )
    return tax_code


def _withholding_amount(base_minor: int, rate_basis_points: int) -> int:
    return _minor(Decimal(base_minor) * Decimal(rate_basis_points) / Decimal(10000))


async def _resolve_withholding(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    tax_code_id: UUID | None,
    withholding_base_minor: int | None,
    amount_minor: int,
    exchange_rate: Decimal,
    base_total: int,
    on_date: date,
) -> tuple[int, int, int]:
    """Return (base, amount, base amount) for an optional withholding code."""
    if tax_code_id is None:
        if withholding_base_minor is not None:
            raise _bad(
                "withholding_tax_code_required",
                "A withholding base requires a withholding tax code.",
            )
        return 0, 0, 0
    tax_code = await load_withholding_code(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        tax_code_id=tax_code_id,
    )
    base = withholding_base_minor if withholding_base_minor is not None else amount_minor
    if not 0 < base <= amount_minor:
        raise _bad(
            "withholding_base_invalid",
            "The withholding base must be positive and no more than the amount.",
        )
    rate = await tax_rate_on(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        tax_code_id=tax_code.id,
        on_date=on_date,
    )
    amount = _withholding_amount(base, rate)
    if amount >= amount_minor:
        raise _bad(
            "withholding_exceeds_amount",
            "The withholding amount must be less than the settlement amount.",
        )
    base_amount = _minor(Decimal(amount) * exchange_rate)
    if base_amount > base_total:
        raise _bad(
            "withholding_exceeds_amount",
            "The base withholding amount exceeds the settlement base value.",
        )
    return base, amount, base_amount


def _validate_amount(amount_minor: int, label: str) -> None:
    if amount_minor <= 0:
        raise _bad(f"{label}_invalid", f"The {label.replace('_', ' ')} must be a positive integer.")
    if amount_minor > 10**17:
        raise _bad(f"{label}_invalid", f"The {label.replace('_', ' ')} is too large.")


def _validate_allocations(allocations: tuple[AllocationInput, ...], amount_minor: int) -> None:
    seen: set[UUID] = set()
    total = 0
    for allocation in allocations:
        if allocation.amount_minor <= 0:
            raise _bad("allocation_invalid", "Every allocation amount must be positive.")
        if allocation.document_id in seen:
            raise _bad("allocation_invalid", "A document may appear only once in one receipt.")
        seen.add(allocation.document_id)
        total += allocation.amount_minor
    if total > amount_minor:
        raise _bad(
            "allocation_invalid",
            "The allocated amount cannot exceed the receipt or payment amount.",
        )


async def _load_period(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, on_date: date
) -> AccountingPeriod:
    period = (
        await session.execute(
            select(AccountingPeriod).where(
                AccountingPeriod.tenant_id == tenant_id,
                AccountingPeriod.company_id == company_id,
                AccountingPeriod.start_date <= on_date,
                AccountingPeriod.end_date >= on_date,
            )
        )
    ).scalar_one_or_none()
    if period is None:
        raise _not_found("period_not_found", f"No accounting period covers {on_date.isoformat()}.")
    if period.status != "open":
        raise _conflict("period_closed", "The accounting period for this date is closed.")
    return period


async def create_cash_bank_account(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: CashBankAccountInput,
) -> AccountResult:
    """Create a cash box or bank account linked to a postable asset account."""
    currency = _validate_currency(request.currency)
    if request.kind not in {"cash", "bank"}:
        raise _bad("account_kind_invalid", "The account kind must be cash or bank.")
    if not request.code.strip() or len(request.code) > 30:
        raise _bad("account_code_invalid", "The account code is 1-30 characters.")
    if not request.name_ar.strip() or len(request.name_ar) > 200:
        raise _bad("account_name_invalid", "The Arabic name is 1-200 characters.")

    async def operation(session: AsyncSession) -> AccountResult:
        ledger_account = (
            await session.execute(
                select(Account).where(
                    Account.tenant_id == tenant_id,
                    Account.company_id == request.company_id,
                    Account.id == request.ledger_account_id,
                )
            )
        ).scalar_one_or_none()
        if ledger_account is None:
            raise _not_found(
                "account_not_found", "The ledger account does not exist in the selected company."
            )
        if not ledger_account.is_postable or not ledger_account.is_active:
            raise _bad(
                "account_not_postable", "The ledger account is not an active postable account."
            )
        if ledger_account.account_type != "asset":
            raise _bad(
                "account_type_invalid", "A cash or bank account must map to an asset account."
            )
        account = CashBankAccount(
            tenant_id=tenant_id,
            company_id=request.company_id,
            code=request.code.strip(),
            name_ar=request.name_ar.strip(),
            name_en=request.name_en,
            kind=request.kind,
            ledger_account_id=ledger_account.id,
            currency=currency,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(account)
        await session.flush()
        return AccountResult(account=account)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_account(error) from None
    except DBAPIError as error:
        raise _translate_account(error) from None


async def list_cash_bank_accounts(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[CashBankAccount]:
    """List a company's cash and bank accounts by code."""
    return list(
        (
            await session.execute(
                select(CashBankAccount)
                .where(
                    CashBankAccount.tenant_id == tenant_id,
                    CashBankAccount.company_id == company_id,
                )
                .order_by(CashBankAccount.code)
            )
        )
        .scalars()
        .all()
    )


async def _load_cash_bank_account(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    cash_bank_account_id: UUID,
    currency: str,
) -> CashBankAccount:
    account = (
        await session.execute(
            select(CashBankAccount).where(
                CashBankAccount.tenant_id == tenant_id,
                CashBankAccount.company_id == company_id,
                CashBankAccount.id == cash_bank_account_id,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise _not_found(
            "cash_bank_account_not_found",
            "The cash or bank account does not exist in the selected company.",
        )
    if not account.is_active:
        raise _bad("cash_bank_account_inactive", f"Account {account.code} is not active.")
    if account.currency != currency:
        raise _bad(
            "cash_bank_currency_mismatch",
            f"Account {account.code} is in {account.currency}, not {currency}.",
        )
    return account


async def _load_customer(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, customer_id: UUID
) -> Customer:
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
        raise _not_found(
            "customer_not_found", "The customer does not exist in the selected company."
        )
    if not customer.is_active:
        raise _bad("customer_inactive", f"Customer {customer.code} is not active.")
    return customer


async def _load_supplier(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, supplier_id: UUID
) -> Supplier:
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
        raise _not_found(
            "supplier_not_found", "The supplier does not exist in the selected company."
        )
    if not supplier.is_active:
        raise _bad("supplier_inactive", f"Supplier {supplier.code} is not active.")
    return supplier


async def _load_invoices(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    invoice_ids: set[UUID],
    for_update: bool,
) -> dict[UUID, SalesInvoice]:
    if not invoice_ids:
        return {}
    statement = select(SalesInvoice).where(
        SalesInvoice.tenant_id == tenant_id,
        SalesInvoice.company_id == company_id,
        SalesInvoice.id.in_(invoice_ids),
    )
    if for_update:
        statement = statement.with_for_update()
    invoices = {row.id: row for row in (await session.execute(statement)).scalars().all()}
    missing = invoice_ids - set(invoices)
    if missing:
        raise _not_found(
            "invoice_not_found", "One or more invoices do not exist in the selected company."
        )
    return invoices


async def _load_bills(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    bill_ids: set[UUID],
    for_update: bool,
) -> dict[UUID, PurchaseBill]:
    if not bill_ids:
        return {}
    statement = select(PurchaseBill).where(
        PurchaseBill.tenant_id == tenant_id,
        PurchaseBill.company_id == company_id,
        PurchaseBill.id.in_(bill_ids),
    )
    if for_update:
        statement = statement.with_for_update()
    bills = {row.id: row for row in (await session.execute(statement)).scalars().all()}
    missing = bill_ids - set(bills)
    if missing:
        raise _not_found(
            "bill_not_found", "One or more bills do not exist in the selected company."
        )
    return bills


async def _invoice_posted_allocations(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    invoice_ids: set[UUID],
) -> dict[UUID, tuple[int, int]]:
    if not invoice_ids:
        return {}
    rows = (
        await session.execute(
            select(
                ReceiptAllocation.invoice_id,
                func.coalesce(func.sum(ReceiptAllocation.amount_minor), 0),
                func.coalesce(func.sum(ReceiptAllocation.base_amount_minor), 0),
            )
            .join(Receipt, Receipt.id == ReceiptAllocation.receipt_id)
            .where(
                ReceiptAllocation.tenant_id == tenant_id,
                ReceiptAllocation.company_id == company_id,
                ReceiptAllocation.invoice_id.in_(invoice_ids),
                Receipt.tenant_id == tenant_id,
                Receipt.company_id == company_id,
                Receipt.status == "posted",
            )
            .group_by(ReceiptAllocation.invoice_id)
        )
    ).all()
    totals = {invoice_id: (int(allocated), int(base)) for invoice_id, allocated, base in rows}
    cheque_rows = (
        await session.execute(
            select(
                ChequeAllocation.invoice_id,
                func.coalesce(func.sum(ChequeAllocation.amount_minor), 0),
                func.coalesce(func.sum(ChequeAllocation.base_amount_minor), 0),
            )
            .join(Cheque, Cheque.id == ChequeAllocation.cheque_id)
            .where(
                ChequeAllocation.tenant_id == tenant_id,
                ChequeAllocation.company_id == company_id,
                ChequeAllocation.invoice_id.in_(invoice_ids),
                ChequeAllocation.is_reversed.is_(False),
                Cheque.tenant_id == tenant_id,
                Cheque.company_id == company_id,
                Cheque.direction == "received",
            )
            .group_by(ChequeAllocation.invoice_id)
        )
    ).all()
    for row in cheque_rows:
        invoice_id = row[0]
        if invoice_id is None:
            continue
        previous = totals.get(invoice_id, (0, 0))
        totals[invoice_id] = (previous[0] + int(row[1]), previous[1] + int(row[2]))
    return totals


async def _bill_posted_allocations(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    bill_ids: set[UUID],
) -> dict[UUID, tuple[int, int]]:
    if not bill_ids:
        return {}
    rows = (
        await session.execute(
            select(
                SupplierPaymentAllocation.bill_id,
                func.coalesce(func.sum(SupplierPaymentAllocation.amount_minor), 0),
                func.coalesce(func.sum(SupplierPaymentAllocation.base_amount_minor), 0),
            )
            .join(SupplierPayment, SupplierPayment.id == SupplierPaymentAllocation.payment_id)
            .where(
                SupplierPaymentAllocation.tenant_id == tenant_id,
                SupplierPaymentAllocation.company_id == company_id,
                SupplierPaymentAllocation.bill_id.in_(bill_ids),
                SupplierPayment.tenant_id == tenant_id,
                SupplierPayment.company_id == company_id,
                SupplierPayment.status == "posted",
            )
            .group_by(SupplierPaymentAllocation.bill_id)
        )
    ).all()
    totals = {bill_id: (int(allocated), int(base)) for bill_id, allocated, base in rows}
    cheque_rows = (
        await session.execute(
            select(
                ChequeAllocation.bill_id,
                func.coalesce(func.sum(ChequeAllocation.amount_minor), 0),
                func.coalesce(func.sum(ChequeAllocation.base_amount_minor), 0),
            )
            .join(Cheque, Cheque.id == ChequeAllocation.cheque_id)
            .where(
                ChequeAllocation.tenant_id == tenant_id,
                ChequeAllocation.company_id == company_id,
                ChequeAllocation.bill_id.in_(bill_ids),
                ChequeAllocation.is_reversed.is_(False),
                Cheque.tenant_id == tenant_id,
                Cheque.company_id == company_id,
                Cheque.direction == "issued",
            )
            .group_by(ChequeAllocation.bill_id)
        )
    ).all()
    for row in cheque_rows:
        bill_id = row[0]
        if bill_id is None:
            continue
        previous = totals.get(bill_id, (0, 0))
        totals[bill_id] = (previous[0] + int(row[1]), previous[1] + int(row[2]))
    return totals


async def _prepare_receipt_allocations(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    customer_id: UUID,
    currency: str,
    exchange_rate: Decimal,
    amount_minor: int,
    allocations: tuple[AllocationInput, ...],
) -> tuple[tuple[ReceiptAllocationPlan, ...], int, int]:
    """Validate allocations against live invoice balances and value them."""
    invoices = await _load_invoices(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        invoice_ids={allocation.document_id for allocation in allocations},
        for_update=True,
    )
    posted = await _invoice_posted_allocations(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        invoice_ids=set(invoices),
    )
    remaining_doc: dict[UUID, int] = {}
    remaining_base: dict[UUID, int] = {}
    for invoice_id, invoice in invoices.items():
        allocated_doc, allocated_base = posted.get(invoice_id, (0, 0))
        remaining_doc[invoice_id] = max(0, invoice.total_minor - allocated_doc)
        remaining_base[invoice_id] = max(0, invoice.base_total_minor - allocated_base)

    plans: list[ReceiptAllocationPlan] = []
    for allocation in allocations:
        invoice = invoices[allocation.document_id]
        if invoice.customer_id != customer_id:
            raise _bad("allocation_invalid", "The invoice belongs to a different customer.")
        if invoice.currency != currency:
            raise _bad(
                "allocation_currency_mismatch",
                "A receipt can only allocate invoices in the same currency.",
            )
        if invoice.status not in {"posted", "part_paid", "paid"}:
            raise _bad("invoice_not_posted", "Only posted invoices can be allocated.")
        outstanding_doc = remaining_doc[invoice.id]
        outstanding_base = remaining_base[invoice.id]
        if outstanding_doc <= 0:
            raise _bad("invoice_already_settled", "The invoice is already fully paid.")
        if allocation.amount_minor > outstanding_doc:
            raise _bad(
                "allocation_exceeds_balance",
                "An allocation cannot exceed the invoice's outstanding balance.",
            )
        if allocation.amount_minor == outstanding_doc:
            base_amount = outstanding_base
        else:
            base_amount = _minor(Decimal(allocation.amount_minor) * invoice.exchange_rate)
            base_amount = min(base_amount, outstanding_base)
        plans.append(
            ReceiptAllocationPlan(
                invoice=invoice,
                amount_minor=allocation.amount_minor,
                base_amount_minor=base_amount,
                settlement_base_minor=0,
                allocated_before_minor=posted.get(invoice.id, (0, 0))[0],
            )
        )
        remaining_doc[invoice.id] = outstanding_doc - allocation.amount_minor
        remaining_base[invoice.id] = outstanding_base - base_amount

    unallocated_minor = amount_minor - sum(plan.amount_minor for plan in plans)
    base_total = _minor(Decimal(amount_minor) * exchange_rate)
    unallocated_base = _minor(Decimal(unallocated_minor) * exchange_rate)
    settlement_budget = base_total - unallocated_base
    settled: list[ReceiptAllocationPlan] = []
    remaining_settlement = settlement_budget
    for index, plan in enumerate(plans):
        if index == len(plans) - 1:
            settlement = remaining_settlement
        else:
            settlement = _minor(Decimal(plan.amount_minor) * exchange_rate)
            settlement = min(settlement, remaining_settlement)
        settled.append(
            ReceiptAllocationPlan(
                invoice=plan.invoice,
                amount_minor=plan.amount_minor,
                base_amount_minor=plan.base_amount_minor,
                settlement_base_minor=settlement,
                allocated_before_minor=plan.allocated_before_minor,
            )
        )
        remaining_settlement -= settlement
    return tuple(settled), unallocated_minor, base_total


async def _post_receipt_in_session(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    receipt: Receipt,
    cash_bank_account: CashBankAccount,
    plans: tuple[ReceiptAllocationPlan, ...],
) -> EntryResult:
    if receipt.status != "draft":
        raise _conflict("receipt_already_posted", "This receipt is already posted.")
    period = await _load_period(
        session,
        tenant_id=tenant_id,
        company_id=receipt.company_id,
        on_date=receipt.receipt_date,
    )
    accounts = await resolve_posting_accounts(
        session,
        tenant_id=tenant_id,
        company_id=receipt.company_id,
        on_date=receipt.receipt_date,
        keys=(
            AR_ACCOUNT,
            CUSTOMER_ADVANCES_ACCOUNT,
            FX_GAIN_ACCOUNT,
            FX_LOSS_ACCOUNT,
            WITHHOLDING_RECEIVABLE_ACCOUNT,
        ),
    )
    receivable_base = sum(plan.base_amount_minor for plan in plans)
    allocated_settlement_base = sum(plan.settlement_base_minor for plan in plans)
    unallocated_base = receipt.base_amount_minor - allocated_settlement_base
    fx_difference = receipt.base_amount_minor - receivable_base - unallocated_base
    cash_base = receipt.base_amount_minor - receipt.base_withholding_minor
    journal_lines = [
        JournalLineInput(
            account_id=cash_bank_account.ledger_account_id,
            debit_minor=cash_base,
            description=f"Receipt {receipt.number}",
        )
    ]
    if receipt.base_withholding_minor > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts[WITHHOLDING_RECEIVABLE_ACCOUNT].id,
                debit_minor=receipt.base_withholding_minor,
                description=f"Receipt {receipt.number} withholding receivable",
            )
        )
    if receivable_base > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts[AR_ACCOUNT].id,
                credit_minor=receivable_base,
                description=f"Receipt {receipt.number} receivable",
            )
        )
    if unallocated_base > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts[CUSTOMER_ADVANCES_ACCOUNT].id,
                credit_minor=unallocated_base,
                description=f"Receipt {receipt.number} customer advance",
            )
        )
    if fx_difference > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts[FX_GAIN_ACCOUNT].id,
                credit_minor=fx_difference,
                description=f"Receipt {receipt.number} exchange gain",
            )
        )
    elif fx_difference < 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts[FX_LOSS_ACCOUNT].id,
                debit_minor=-fx_difference,
                description=f"Receipt {receipt.number} exchange loss",
            )
        )
    entry = await post_entry_in_session(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        company_id=receipt.company_id,
        period_id=period.id,
        entry_date=receipt.receipt_date,
        memo=receipt.memo or f"Customer receipt {receipt.number}",
        lines=tuple(journal_lines),
        source_type="receipt",
        source_id=receipt.id,
    )
    receipt.status = "posted"
    receipt.journal_entry_id = entry.entry.id
    receipt.posted_at = datetime.now(UTC)
    receipt.posted_by = actor_user_id
    for plan in plans:
        settled_total = plan.allocated_before_minor + plan.amount_minor
        plan.invoice.status = "paid" if settled_total >= plan.invoice.total_minor else "part_paid"
    await session.flush()
    return entry


async def create_receipt(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: ReceiptInput,
) -> ReceiptResult:
    """Create a draft receipt, optionally posting it immediately."""
    currency = _validate_currency(request.currency)
    _validate_amount(request.amount_minor, "amount")
    if request.exchange_rate <= 0:
        raise _bad("exchange_rate_invalid", "The exchange rate must be positive.")
    if request.number is not None and not 1 <= len(request.number) <= 40:
        raise _bad("document_number_invalid", "A document number is 1-40 characters.")
    _validate_allocations(request.allocations, request.amount_minor)

    async def operation(session: AsyncSession) -> ReceiptResult:
        await _load_customer(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            customer_id=request.customer_id,
        )
        cash_account = await _load_cash_bank_account(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            cash_bank_account_id=request.cash_bank_account_id,
            currency=currency,
        )
        plans, unallocated, base_total = await _prepare_receipt_allocations(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            customer_id=request.customer_id,
            currency=currency,
            exchange_rate=request.exchange_rate,
            amount_minor=request.amount_minor,
            allocations=request.allocations,
        )
        withholding_base, withholding_amount, base_withholding = await _resolve_withholding(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            tax_code_id=request.withholding_tax_code_id,
            withholding_base_minor=request.withholding_base_minor,
            amount_minor=request.amount_minor,
            exchange_rate=request.exchange_rate,
            base_total=base_total,
            on_date=request.receipt_date,
        )
        if request.number is not None:
            number = request.number
        else:
            number = await allocate_document_number(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                scope=RECEIPT_SCOPE,
                period_key=f"{request.receipt_date.year:04d}",
                prefix=RECEIPT_PREFIX,
            )
        receipt = Receipt(
            tenant_id=tenant_id,
            company_id=request.company_id,
            number=number,
            customer_id=request.customer_id,
            cash_bank_account_id=cash_account.id,
            receipt_date=request.receipt_date,
            currency=currency,
            exchange_rate=request.exchange_rate,
            amount_minor=request.amount_minor,
            base_amount_minor=base_total,
            unallocated_minor=unallocated,
            withholding_tax_code_id=request.withholding_tax_code_id,
            withholding_base_minor=withholding_base,
            withholding_amount_minor=withholding_amount,
            base_withholding_minor=base_withholding,
            memo=request.memo,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(receipt)
        await session.flush()
        allocations = tuple(
            ReceiptAllocation(
                tenant_id=tenant_id,
                company_id=request.company_id,
                receipt_id=receipt.id,
                invoice_id=plan.invoice.id,
                amount_minor=plan.amount_minor,
                base_amount_minor=plan.base_amount_minor,
                settlement_base_minor=plan.settlement_base_minor,
            )
            for plan in plans
        )
        session.add_all(allocations)
        await session.flush()
        entry: EntryResult | None = None
        if request.post:
            entry = await _post_receipt_in_session(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                receipt=receipt,
                cash_bank_account=cash_account,
                plans=plans,
            )
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="cashbank.receipt.posted" if request.post else "cashbank.receipt.created",
            entity_type="receipt",
            entity_id=receipt.id,
            detail={"number": receipt.number, "amount_minor": receipt.amount_minor},
        )
        return ReceiptResult(receipt=receipt, allocations=allocations, journal_entry=entry)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_receipt(error) from None
    except DBAPIError as error:
        raise _translate_receipt(error) from None


async def post_receipt(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    receipt_id: UUID,
) -> ReceiptResult:
    """Post a draft receipt and its allocations."""

    async def operation(session: AsyncSession) -> ReceiptResult:
        receipt = (
            await session.execute(
                select(Receipt)
                .where(
                    Receipt.tenant_id == tenant_id,
                    Receipt.company_id == company_id,
                    Receipt.id == receipt_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if receipt is None:
            raise _not_found(
                "receipt_not_found", "The receipt does not exist in the selected company."
            )
        rows = (
            (
                await session.execute(
                    select(ReceiptAllocation)
                    .where(
                        ReceiptAllocation.tenant_id == tenant_id,
                        ReceiptAllocation.receipt_id == receipt.id,
                    )
                    .order_by(ReceiptAllocation.created_at)
                )
            )
            .scalars()
            .all()
        )
        inputs = tuple(
            AllocationInput(document_id=row.invoice_id, amount_minor=row.amount_minor)
            for row in rows
        )
        cash_account = await _load_cash_bank_account(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            cash_bank_account_id=receipt.cash_bank_account_id,
            currency=receipt.currency,
        )
        plans, unallocated, base_total = await _prepare_receipt_allocations(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            customer_id=receipt.customer_id,
            currency=receipt.currency,
            exchange_rate=receipt.exchange_rate,
            amount_minor=receipt.amount_minor,
            allocations=inputs,
        )
        if unallocated != receipt.unallocated_minor or base_total != receipt.base_amount_minor:
            raise _conflict(
                "receipt_changed",
                "The receipt totals no longer match its allocations; recreate the draft.",
            )
        for row, plan in zip(rows, plans, strict=True):
            row.base_amount_minor = plan.base_amount_minor
            row.settlement_base_minor = plan.settlement_base_minor
        await session.flush()
        entry = await _post_receipt_in_session(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            receipt=receipt,
            cash_bank_account=cash_account,
            plans=plans,
        )
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="cashbank.receipt.posted",
            entity_type="receipt",
            entity_id=receipt.id,
            detail={"number": receipt.number, "amount_minor": receipt.amount_minor},
        )
        return ReceiptResult(receipt=receipt, allocations=tuple(rows), journal_entry=entry)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_receipt(error) from None
    except DBAPIError as error:
        raise _translate_receipt(error) from None


async def get_receipt(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, receipt_id: UUID
) -> tuple[Receipt, tuple[ReceiptAllocation, ...]]:
    """Load one receipt and its allocations."""
    receipt = (
        await session.execute(
            select(Receipt).where(
                Receipt.tenant_id == tenant_id,
                Receipt.company_id == company_id,
                Receipt.id == receipt_id,
            )
        )
    ).scalar_one_or_none()
    if receipt is None:
        raise _not_found("receipt_not_found", "The receipt does not exist in the selected company.")
    allocations = tuple(
        (
            await session.execute(
                select(ReceiptAllocation)
                .where(
                    ReceiptAllocation.tenant_id == tenant_id,
                    ReceiptAllocation.receipt_id == receipt.id,
                )
                .order_by(ReceiptAllocation.created_at)
            )
        )
        .scalars()
        .all()
    )
    return receipt, allocations


async def list_receipts(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[Receipt]:
    """List receipts newest first."""
    return list(
        (
            await session.execute(
                select(Receipt)
                .where(Receipt.tenant_id == tenant_id, Receipt.company_id == company_id)
                .order_by(Receipt.receipt_date.desc(), Receipt.number.desc())
            )
        )
        .scalars()
        .all()
    )


async def _prepare_payment_allocations(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    supplier_id: UUID,
    currency: str,
    exchange_rate: Decimal,
    amount_minor: int,
    allocations: tuple[AllocationInput, ...],
) -> tuple[tuple[PaymentAllocationPlan, ...], int, int]:
    """Validate bill allocations against live balances and value them."""
    bills = await _load_bills(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        bill_ids={allocation.document_id for allocation in allocations},
        for_update=True,
    )
    posted = await _bill_posted_allocations(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        bill_ids=set(bills),
    )
    remaining_doc: dict[UUID, int] = {}
    remaining_base: dict[UUID, int] = {}
    for bill_id, bill in bills.items():
        allocated_doc, allocated_base = posted.get(bill_id, (0, 0))
        remaining_doc[bill_id] = max(0, bill.total_minor - allocated_doc)
        remaining_base[bill_id] = max(0, bill.base_total_minor - allocated_base)

    plans: list[PaymentAllocationPlan] = []
    for allocation in allocations:
        bill = bills[allocation.document_id]
        if bill.supplier_id != supplier_id:
            raise _bad("allocation_invalid", "The bill belongs to a different supplier.")
        if bill.currency != currency:
            raise _bad(
                "allocation_currency_mismatch",
                "A payment can only allocate bills in the same currency.",
            )
        if bill.status not in {"posted", "part_paid", "paid"}:
            raise _bad("bill_not_posted", "Only posted bills can be allocated.")
        outstanding_doc = remaining_doc[bill.id]
        outstanding_base = remaining_base[bill.id]
        if outstanding_doc <= 0:
            raise _bad("bill_already_settled", "The bill is already fully paid.")
        if allocation.amount_minor > outstanding_doc:
            raise _bad(
                "allocation_exceeds_balance",
                "An allocation cannot exceed the bill's outstanding balance.",
            )
        if allocation.amount_minor == outstanding_doc:
            base_amount = outstanding_base
        else:
            base_amount = _minor(Decimal(allocation.amount_minor) * bill.exchange_rate)
            base_amount = min(base_amount, outstanding_base)
        plans.append(
            PaymentAllocationPlan(
                bill=bill,
                amount_minor=allocation.amount_minor,
                base_amount_minor=base_amount,
                settlement_base_minor=0,
                allocated_before_minor=posted.get(bill.id, (0, 0))[0],
            )
        )
        remaining_doc[bill.id] = outstanding_doc - allocation.amount_minor
        remaining_base[bill.id] = outstanding_base - base_amount

    unallocated_minor = amount_minor - sum(plan.amount_minor for plan in plans)
    base_total = _minor(Decimal(amount_minor) * exchange_rate)
    unallocated_base = _minor(Decimal(unallocated_minor) * exchange_rate)
    settlement_budget = base_total - unallocated_base
    settled: list[PaymentAllocationPlan] = []
    remaining_settlement = settlement_budget
    for index, plan in enumerate(plans):
        if index == len(plans) - 1:
            settlement = remaining_settlement
        else:
            settlement = _minor(Decimal(plan.amount_minor) * exchange_rate)
            settlement = min(settlement, remaining_settlement)
        settled.append(
            PaymentAllocationPlan(
                bill=plan.bill,
                amount_minor=plan.amount_minor,
                base_amount_minor=plan.base_amount_minor,
                settlement_base_minor=settlement,
                allocated_before_minor=plan.allocated_before_minor,
            )
        )
        remaining_settlement -= settlement
    return tuple(settled), unallocated_minor, base_total


async def _post_payment_in_session(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    payment: SupplierPayment,
    cash_bank_account: CashBankAccount,
    plans: tuple[PaymentAllocationPlan, ...],
) -> EntryResult:
    if payment.status != "draft":
        raise _conflict("payment_already_posted", "This payment is already posted.")
    period = await _load_period(
        session,
        tenant_id=tenant_id,
        company_id=payment.company_id,
        on_date=payment.payment_date,
    )
    accounts = await resolve_posting_accounts(
        session,
        tenant_id=tenant_id,
        company_id=payment.company_id,
        on_date=payment.payment_date,
        keys=(
            AP_ACCOUNT,
            SUPPLIER_ADVANCES_ACCOUNT,
            FX_GAIN_ACCOUNT,
            FX_LOSS_ACCOUNT,
            WITHHOLDING_PAYABLE_ACCOUNT,
        ),
    )
    payable_base = sum(plan.base_amount_minor for plan in plans)
    allocated_settlement_base = sum(plan.settlement_base_minor for plan in plans)
    unallocated_base = payment.base_amount_minor - allocated_settlement_base
    fx_difference = payable_base + unallocated_base - payment.base_amount_minor
    journal_lines: list[JournalLineInput] = []
    if payable_base > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts[AP_ACCOUNT].id,
                debit_minor=payable_base,
                description=f"Payment {payment.number} payable",
            )
        )
    if unallocated_base > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts[SUPPLIER_ADVANCES_ACCOUNT].id,
                debit_minor=unallocated_base,
                description=f"Payment {payment.number} supplier advance",
            )
        )
    if fx_difference > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts[FX_GAIN_ACCOUNT].id,
                credit_minor=fx_difference,
                description=f"Payment {payment.number} exchange gain",
            )
        )
    elif fx_difference < 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts[FX_LOSS_ACCOUNT].id,
                debit_minor=-fx_difference,
                description=f"Payment {payment.number} exchange loss",
            )
        )
    cash_base = payment.base_amount_minor - payment.base_withholding_minor
    journal_lines.append(
        JournalLineInput(
            account_id=cash_bank_account.ledger_account_id,
            credit_minor=cash_base,
            description=f"Payment {payment.number}",
        )
    )
    if payment.base_withholding_minor > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts[WITHHOLDING_PAYABLE_ACCOUNT].id,
                credit_minor=payment.base_withholding_minor,
                description=f"Payment {payment.number} withholding payable",
            )
        )
    entry = await post_entry_in_session(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        company_id=payment.company_id,
        period_id=period.id,
        entry_date=payment.payment_date,
        memo=payment.memo or f"Supplier payment {payment.number}",
        lines=tuple(journal_lines),
        source_type="supplier_payment",
        source_id=payment.id,
    )
    payment.status = "posted"
    payment.journal_entry_id = entry.entry.id
    payment.posted_at = datetime.now(UTC)
    payment.posted_by = actor_user_id
    for plan in plans:
        settled_total = plan.allocated_before_minor + plan.amount_minor
        plan.bill.status = "paid" if settled_total >= plan.bill.total_minor else "part_paid"
    await session.flush()
    return entry


async def create_payment(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: PaymentInput,
) -> PaymentResult:
    """Create a draft supplier payment, optionally posting it immediately."""
    currency = _validate_currency(request.currency)
    _validate_amount(request.amount_minor, "amount")
    if request.exchange_rate <= 0:
        raise _bad("exchange_rate_invalid", "The exchange rate must be positive.")
    if request.number is not None and not 1 <= len(request.number) <= 40:
        raise _bad("document_number_invalid", "A document number is 1-40 characters.")
    _validate_allocations(request.allocations, request.amount_minor)

    async def operation(session: AsyncSession) -> PaymentResult:
        await _load_supplier(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            supplier_id=request.supplier_id,
        )
        cash_account = await _load_cash_bank_account(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            cash_bank_account_id=request.cash_bank_account_id,
            currency=currency,
        )
        plans, unallocated, base_total = await _prepare_payment_allocations(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            supplier_id=request.supplier_id,
            currency=currency,
            exchange_rate=request.exchange_rate,
            amount_minor=request.amount_minor,
            allocations=request.allocations,
        )
        withholding_base, withholding_amount, base_withholding = await _resolve_withholding(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            tax_code_id=request.withholding_tax_code_id,
            withholding_base_minor=request.withholding_base_minor,
            amount_minor=request.amount_minor,
            exchange_rate=request.exchange_rate,
            base_total=base_total,
            on_date=request.payment_date,
        )
        if request.number is not None:
            number = request.number
        else:
            number = await allocate_document_number(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                scope=PAYMENT_SCOPE,
                period_key=f"{request.payment_date.year:04d}",
                prefix=PAYMENT_PREFIX,
            )
        payment = SupplierPayment(
            tenant_id=tenant_id,
            company_id=request.company_id,
            number=number,
            supplier_id=request.supplier_id,
            cash_bank_account_id=cash_account.id,
            payment_date=request.payment_date,
            currency=currency,
            exchange_rate=request.exchange_rate,
            amount_minor=request.amount_minor,
            base_amount_minor=base_total,
            unallocated_minor=unallocated,
            withholding_tax_code_id=request.withholding_tax_code_id,
            withholding_base_minor=withholding_base,
            withholding_amount_minor=withholding_amount,
            base_withholding_minor=base_withholding,
            memo=request.memo,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(payment)
        await session.flush()
        allocations = tuple(
            SupplierPaymentAllocation(
                tenant_id=tenant_id,
                company_id=request.company_id,
                payment_id=payment.id,
                bill_id=plan.bill.id,
                amount_minor=plan.amount_minor,
                base_amount_minor=plan.base_amount_minor,
                settlement_base_minor=plan.settlement_base_minor,
            )
            for plan in plans
        )
        session.add_all(allocations)
        await session.flush()
        entry: EntryResult | None = None
        if request.post:
            entry = await _post_payment_in_session(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                payment=payment,
                cash_bank_account=cash_account,
                plans=plans,
            )
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="cashbank.payment.posted" if request.post else "cashbank.payment.created",
            entity_type="supplier_payment",
            entity_id=payment.id,
            detail={"number": payment.number, "amount_minor": payment.amount_minor},
        )
        return PaymentResult(payment=payment, allocations=allocations, journal_entry=entry)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_payment(error) from None
    except DBAPIError as error:
        raise _translate_payment(error) from None


async def post_payment(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    payment_id: UUID,
) -> PaymentResult:
    """Post a draft supplier payment and its allocations."""

    async def operation(session: AsyncSession) -> PaymentResult:
        payment = (
            await session.execute(
                select(SupplierPayment)
                .where(
                    SupplierPayment.tenant_id == tenant_id,
                    SupplierPayment.company_id == company_id,
                    SupplierPayment.id == payment_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if payment is None:
            raise _not_found(
                "payment_not_found", "The payment does not exist in the selected company."
            )
        rows = (
            (
                await session.execute(
                    select(SupplierPaymentAllocation)
                    .where(
                        SupplierPaymentAllocation.tenant_id == tenant_id,
                        SupplierPaymentAllocation.payment_id == payment.id,
                    )
                    .order_by(SupplierPaymentAllocation.created_at)
                )
            )
            .scalars()
            .all()
        )
        inputs = tuple(
            AllocationInput(document_id=row.bill_id, amount_minor=row.amount_minor) for row in rows
        )
        cash_account = await _load_cash_bank_account(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            cash_bank_account_id=payment.cash_bank_account_id,
            currency=payment.currency,
        )
        await _load_supplier(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            supplier_id=payment.supplier_id,
        )
        plans, unallocated, base_total = await _prepare_payment_allocations(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            supplier_id=payment.supplier_id,
            currency=payment.currency,
            exchange_rate=payment.exchange_rate,
            amount_minor=payment.amount_minor,
            allocations=inputs,
        )
        if unallocated != payment.unallocated_minor or base_total != payment.base_amount_minor:
            raise _conflict(
                "payment_changed",
                "The payment totals no longer match its allocations; recreate the draft.",
            )
        for row, plan in zip(rows, plans, strict=True):
            row.base_amount_minor = plan.base_amount_minor
            row.settlement_base_minor = plan.settlement_base_minor
        await session.flush()
        entry = await _post_payment_in_session(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            payment=payment,
            cash_bank_account=cash_account,
            plans=plans,
        )
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="cashbank.payment.posted",
            entity_type="supplier_payment",
            entity_id=payment.id,
            detail={"number": payment.number, "amount_minor": payment.amount_minor},
        )
        return PaymentResult(payment=payment, allocations=tuple(rows), journal_entry=entry)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_payment(error) from None
    except DBAPIError as error:
        raise _translate_payment(error) from None


async def get_payment(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, payment_id: UUID
) -> tuple[SupplierPayment, tuple[SupplierPaymentAllocation, ...]]:
    """Load one supplier payment and its allocations."""
    payment = (
        await session.execute(
            select(SupplierPayment).where(
                SupplierPayment.tenant_id == tenant_id,
                SupplierPayment.company_id == company_id,
                SupplierPayment.id == payment_id,
            )
        )
    ).scalar_one_or_none()
    if payment is None:
        raise _not_found("payment_not_found", "The payment does not exist in the selected company.")
    allocations = tuple(
        (
            await session.execute(
                select(SupplierPaymentAllocation)
                .where(
                    SupplierPaymentAllocation.tenant_id == tenant_id,
                    SupplierPaymentAllocation.payment_id == payment.id,
                )
                .order_by(SupplierPaymentAllocation.created_at)
            )
        )
        .scalars()
        .all()
    )
    return payment, allocations


async def list_payments(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[SupplierPayment]:
    """List supplier payments newest first."""
    return list(
        (
            await session.execute(
                select(SupplierPayment)
                .where(
                    SupplierPayment.tenant_id == tenant_id,
                    SupplierPayment.company_id == company_id,
                )
                .order_by(SupplierPayment.payment_date.desc(), SupplierPayment.number.desc())
            )
        )
        .scalars()
        .all()
    )


CASH_TRANSFER_SCOPE = "cash_transfer"
CASH_TRANSFER_PREFIX = "CT"
CHEQUES_UNDER_COLLECTION_ACCOUNT = "cheques_under_collection"
CHEQUES_ISSUED_ACCOUNT = "cheques_issued"


@dataclass(frozen=True)
class CashTransferInput:
    """A transfer between two cash or bank accounts, posted immediately."""

    company_id: UUID
    transfer_date: date
    from_account_id: UUID
    to_account_id: UUID
    amount_minor: int
    currency: str = "USD"
    exchange_rate: Decimal = Decimal("1")
    memo: str | None = None
    number: str | None = None


@dataclass(frozen=True)
class ChequeInput:
    """A received or issued cheque with optional invoice/bill allocations."""

    company_id: UUID
    direction: str
    number: str
    cash_bank_account_id: UUID
    issue_date: date
    amount_minor: int
    customer_id: UUID | None = None
    supplier_id: UUID | None = None
    due_date: date | None = None
    currency: str = "USD"
    exchange_rate: Decimal = Decimal("1")
    bank_name: str | None = None
    party_name: str | None = None
    memo: str | None = None
    allocations: tuple[AllocationInput, ...] = ()
    post: bool = False


@dataclass(frozen=True)
class ChequeResult:
    """A cheque, its allocations and its posted lifecycle events."""

    cheque: Cheque
    allocations: tuple[ChequeAllocation, ...]
    events: tuple[ChequeEvent, ...]


@dataclass(frozen=True)
class CashTransferResult:
    """A posted transfer and the journal entry that moved the money."""

    transfer: CashTransfer
    journal_entry: EntryResult


def _recalculate_invoice_status(invoice: SalesInvoice, settled_amount_minor: int) -> None:
    if settled_amount_minor >= invoice.total_minor:
        invoice.status = "paid"
    elif settled_amount_minor > 0:
        invoice.status = "part_paid"
    else:
        invoice.status = "posted"


def _recalculate_bill_status(bill: PurchaseBill, settled_amount_minor: int) -> None:
    if settled_amount_minor >= bill.total_minor:
        bill.status = "paid"
    elif settled_amount_minor > 0:
        bill.status = "part_paid"
    else:
        bill.status = "posted"


async def _reverse_cheque_allocations(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    cheque: Cheque,
) -> None:
    allocations = tuple(
        (
            await session.execute(
                select(ChequeAllocation).where(
                    ChequeAllocation.tenant_id == tenant_id,
                    ChequeAllocation.company_id == company_id,
                    ChequeAllocation.cheque_id == cheque.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not allocations:
        return
    now = datetime.now(UTC)
    for allocation in allocations:
        if not allocation.is_reversed:
            allocation.is_reversed = True
            allocation.reversed_at = now
    await session.flush()
    if cheque.direction == "received":
        invoice_ids = {allocation.invoice_id for allocation in allocations if allocation.invoice_id}
        invoices = await _load_invoices(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            invoice_ids=invoice_ids,
            for_update=False,
        )
        settled = await _invoice_posted_allocations(
            session, tenant_id=tenant_id, company_id=company_id, invoice_ids=invoice_ids
        )
        for invoice_id, invoice in invoices.items():
            _recalculate_invoice_status(invoice, settled.get(invoice_id, (0, 0))[0])
    else:
        bill_ids = {allocation.bill_id for allocation in allocations if allocation.bill_id}
        bills = await _load_bills(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            bill_ids=bill_ids,
            for_update=False,
        )
        settled = await _bill_posted_allocations(
            session, tenant_id=tenant_id, company_id=company_id, bill_ids=bill_ids
        )
        for bill_id, bill in bills.items():
            _recalculate_bill_status(bill, settled.get(bill_id, (0, 0))[0])
    await session.flush()


async def _load_cheque(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    cheque_id: UUID,
    for_update: bool = False,
) -> Cheque:
    statement = select(Cheque).where(
        Cheque.tenant_id == tenant_id,
        Cheque.company_id == company_id,
        Cheque.id == cheque_id,
    )
    if for_update:
        statement = statement.with_for_update()
    cheque = (await session.execute(statement)).scalar_one_or_none()
    if cheque is None:
        raise _not_found("cheque_not_found", "The cheque does not exist in the selected company.")
    return cheque


async def _cheque_payload(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, cheque: Cheque
) -> ChequeResult:
    allocations = tuple(
        (
            await session.execute(
                select(ChequeAllocation)
                .where(
                    ChequeAllocation.tenant_id == tenant_id,
                    ChequeAllocation.company_id == company_id,
                    ChequeAllocation.cheque_id == cheque.id,
                )
                .order_by(ChequeAllocation.created_at)
            )
        )
        .scalars()
        .all()
    )
    events = tuple(
        (
            await session.execute(
                select(ChequeEvent)
                .where(
                    ChequeEvent.tenant_id == tenant_id,
                    ChequeEvent.company_id == company_id,
                    ChequeEvent.cheque_id == cheque.id,
                )
                .order_by(ChequeEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    return ChequeResult(cheque=cheque, allocations=allocations, events=events)


async def _load_bank_account_for_currency(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    cash_bank_account_id: UUID,
    currency: str,
) -> CashBankAccount:
    return await _load_cash_bank_account(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        cash_bank_account_id=cash_bank_account_id,
        currency=currency,
    )


async def _post_cheque_received_in_session(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    cheque: Cheque,
    event: ChequeEvent,
    plans: tuple[ReceiptAllocationPlan, ...],
) -> EntryResult:
    period = await _load_period(
        session,
        tenant_id=tenant_id,
        company_id=cheque.company_id,
        on_date=cheque.issue_date,
    )
    accounts = await resolve_posting_accounts(
        session,
        tenant_id=tenant_id,
        company_id=cheque.company_id,
        on_date=cheque.issue_date,
        keys=(
            CHEQUES_UNDER_COLLECTION_ACCOUNT,
            AR_ACCOUNT,
            CUSTOMER_ADVANCES_ACCOUNT,
            FX_GAIN_ACCOUNT,
            FX_LOSS_ACCOUNT,
        ),
    )
    receivable_base = sum(plan.base_amount_minor for plan in plans)
    allocated_settlement = sum(plan.settlement_base_minor for plan in plans)
    unallocated_base = cheque.base_amount_minor - allocated_settlement
    fx_difference = cheque.base_amount_minor - receivable_base - unallocated_base
    lines = [
        JournalLineInput(
            account_id=accounts[CHEQUES_UNDER_COLLECTION_ACCOUNT].id,
            debit_minor=cheque.base_amount_minor,
            description=f"Cheque {cheque.number} received",
        )
    ]
    if receivable_base > 0:
        lines.append(
            JournalLineInput(
                account_id=accounts[AR_ACCOUNT].id,
                credit_minor=receivable_base,
                description=f"Cheque {cheque.number} receivable",
            )
        )
    if unallocated_base > 0:
        lines.append(
            JournalLineInput(
                account_id=accounts[CUSTOMER_ADVANCES_ACCOUNT].id,
                credit_minor=unallocated_base,
                description=f"Cheque {cheque.number} customer advance",
            )
        )
    if fx_difference > 0:
        lines.append(
            JournalLineInput(
                account_id=accounts[FX_GAIN_ACCOUNT].id,
                credit_minor=fx_difference,
                description=f"Cheque {cheque.number} exchange gain",
            )
        )
    elif fx_difference < 0:
        lines.append(
            JournalLineInput(
                account_id=accounts[FX_LOSS_ACCOUNT].id,
                debit_minor=-fx_difference,
                description=f"Cheque {cheque.number} exchange loss",
            )
        )
    return await post_entry_in_session(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        company_id=cheque.company_id,
        period_id=period.id,
        entry_date=cheque.issue_date,
        memo=cheque.memo or f"Received cheque {cheque.number}",
        lines=tuple(lines),
        source_type="cheque_event",
        source_id=event.id,
    )


async def _post_cheque_issued_in_session(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    cheque: Cheque,
    event: ChequeEvent,
    plans: tuple[PaymentAllocationPlan, ...],
) -> EntryResult:
    period = await _load_period(
        session,
        tenant_id=tenant_id,
        company_id=cheque.company_id,
        on_date=cheque.issue_date,
    )
    accounts = await resolve_posting_accounts(
        session,
        tenant_id=tenant_id,
        company_id=cheque.company_id,
        on_date=cheque.issue_date,
        keys=(
            CHEQUES_ISSUED_ACCOUNT,
            AP_ACCOUNT,
            SUPPLIER_ADVANCES_ACCOUNT,
            FX_GAIN_ACCOUNT,
            FX_LOSS_ACCOUNT,
        ),
    )
    payable_base = sum(plan.base_amount_minor for plan in plans)
    allocated_settlement = sum(plan.settlement_base_minor for plan in plans)
    unallocated_base = cheque.base_amount_minor - allocated_settlement
    fx_difference = cheque.base_amount_minor - payable_base - unallocated_base
    lines = [
        JournalLineInput(
            account_id=accounts[CHEQUES_ISSUED_ACCOUNT].id,
            credit_minor=cheque.base_amount_minor,
            description=f"Cheque {cheque.number} issued",
        )
    ]
    if payable_base > 0:
        lines.append(
            JournalLineInput(
                account_id=accounts[AP_ACCOUNT].id,
                debit_minor=payable_base,
                description=f"Cheque {cheque.number} payable",
            )
        )
    if unallocated_base > 0:
        lines.append(
            JournalLineInput(
                account_id=accounts[SUPPLIER_ADVANCES_ACCOUNT].id,
                debit_minor=unallocated_base,
                description=f"Cheque {cheque.number} supplier advance",
            )
        )
    if fx_difference > 0:
        lines.append(
            JournalLineInput(
                account_id=accounts[FX_LOSS_ACCOUNT].id,
                debit_minor=fx_difference,
                description=f"Cheque {cheque.number} exchange loss",
            )
        )
    elif fx_difference < 0:
        lines.append(
            JournalLineInput(
                account_id=accounts[FX_GAIN_ACCOUNT].id,
                credit_minor=-fx_difference,
                description=f"Cheque {cheque.number} exchange gain",
            )
        )
    return await post_entry_in_session(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        company_id=cheque.company_id,
        period_id=period.id,
        entry_date=cheque.issue_date,
        memo=cheque.memo or f"Issued cheque {cheque.number}",
        lines=tuple(lines),
        source_type="cheque_event",
        source_id=event.id,
    )


async def create_cash_transfer(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: CashTransferInput,
) -> CashTransferResult:
    """Create and post a transfer between two accounts in the same currency."""
    currency = _validate_currency(request.currency)
    _validate_amount(request.amount_minor, "amount")
    if request.exchange_rate <= 0:
        raise _bad("exchange_rate_invalid", "The exchange rate must be positive.")
    if request.from_account_id == request.to_account_id:
        raise _bad("transfer_accounts_same", "A transfer needs two different accounts.")
    if request.number is not None and not 1 <= len(request.number) <= 40:
        raise _bad("document_number_invalid", "A document number is 1-40 characters.")

    async def operation(session: AsyncSession) -> CashTransferResult:
        from_account = await _load_bank_account_for_currency(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            cash_bank_account_id=request.from_account_id,
            currency=currency,
        )
        to_account = await _load_bank_account_for_currency(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            cash_bank_account_id=request.to_account_id,
            currency=currency,
        )
        period = await _load_period(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            on_date=request.transfer_date,
        )
        number = request.number or await allocate_document_number(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            scope=CASH_TRANSFER_SCOPE,
            period_key=f"{request.transfer_date.year:04d}",
            prefix=CASH_TRANSFER_PREFIX,
        )
        base_amount = _minor(Decimal(request.amount_minor) * request.exchange_rate)
        if base_amount <= 0:
            raise _bad("amount_invalid", "The base amount rounds to zero.")
        transfer = CashTransfer(
            tenant_id=tenant_id,
            company_id=request.company_id,
            number=number,
            transfer_date=request.transfer_date,
            from_account_id=from_account.id,
            to_account_id=to_account.id,
            currency=currency,
            exchange_rate=request.exchange_rate,
            amount_minor=request.amount_minor,
            base_amount_minor=base_amount,
            memo=request.memo,
            status="posted",
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(transfer)
        await session.flush()
        entry = await post_entry_in_session(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            company_id=request.company_id,
            period_id=period.id,
            entry_date=request.transfer_date,
            memo=request.memo or f"Cash transfer {number}",
            lines=(
                JournalLineInput(
                    account_id=to_account.ledger_account_id,
                    debit_minor=base_amount,
                    description=f"Transfer {number} to {to_account.code}",
                ),
                JournalLineInput(
                    account_id=from_account.ledger_account_id,
                    credit_minor=base_amount,
                    description=f"Transfer {number} from {from_account.code}",
                ),
            ),
            source_type="cash_transfer",
            source_id=transfer.id,
        )
        transfer.journal_entry_id = entry.entry.id
        transfer.posted_at = datetime.now(UTC)
        transfer.posted_by = actor_user_id
        await session.flush()
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="cashbank.transfer.posted",
            entity_type="cash_transfer",
            entity_id=transfer.id,
            detail={"number": number, "amount_minor": base_amount},
        )
        return CashTransferResult(transfer=transfer, journal_entry=entry)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_account(error) from None
    except DBAPIError as error:
        raise _translate_account(error) from None


async def list_cash_transfers(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[CashTransfer]:
    """List transfers newest first."""
    return list(
        (
            await session.execute(
                select(CashTransfer)
                .where(
                    CashTransfer.tenant_id == tenant_id,
                    CashTransfer.company_id == company_id,
                )
                .order_by(CashTransfer.transfer_date.desc(), CashTransfer.number.desc())
            )
        )
        .scalars()
        .all()
    )


async def get_cash_transfer(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, transfer_id: UUID
) -> tuple[CashTransfer, CashBankAccount, CashBankAccount]:
    """Load one posted transfer with its source and destination accounts."""
    transfer = (
        await session.execute(
            select(CashTransfer).where(
                CashTransfer.tenant_id == tenant_id,
                CashTransfer.company_id == company_id,
                CashTransfer.id == transfer_id,
            )
        )
    ).scalar_one_or_none()
    if transfer is None:
        raise _not_found(
            "cash_transfer_not_found", "The transfer does not exist in the selected company."
        )
    accounts = (
        (
            await session.execute(
                select(CashBankAccount).where(
                    CashBankAccount.tenant_id == tenant_id,
                    CashBankAccount.company_id == company_id,
                    CashBankAccount.id.in_({transfer.from_account_id, transfer.to_account_id}),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {account.id: account for account in accounts}
    source = by_id.get(transfer.from_account_id)
    destination = by_id.get(transfer.to_account_id)
    if source is None or destination is None:
        raise _not_found(
            "cash_account_not_found",
            "The cash or bank account of this transfer does not exist in the selected company.",
        )
    return transfer, source, destination


async def create_cheque(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: ChequeInput,
) -> ChequeResult:
    """Create a received or issued cheque, posting its first lifecycle entry."""
    if request.direction not in {"received", "issued"}:
        raise _bad("cheque_direction_invalid", "A cheque is either received or issued.")
    currency = _validate_currency(request.currency)
    _validate_amount(request.amount_minor, "amount")
    if request.exchange_rate <= 0:
        raise _bad("exchange_rate_invalid", "The exchange rate must be positive.")
    if not 1 <= len(request.number.strip()) <= 40:
        raise _bad("cheque_number_invalid", "A cheque number is 1-40 characters.")
    due_date = request.due_date or request.issue_date
    if due_date < request.issue_date:
        raise _bad("cheque_due_date_invalid", "The due date cannot precede the issue date.")
    if request.direction == "received" and request.customer_id is None:
        raise _bad("cheque_customer_required", "A received cheque needs a customer.")
    if request.direction == "issued" and request.supplier_id is None:
        raise _bad("cheque_supplier_required", "An issued cheque needs a supplier.")
    _validate_allocations(request.allocations, request.amount_minor)

    async def operation(session: AsyncSession) -> ChequeResult:
        cash_account = await _load_bank_account_for_currency(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            cash_bank_account_id=request.cash_bank_account_id,
            currency=currency,
        )
        base_amount = _minor(Decimal(request.amount_minor) * request.exchange_rate)
        if base_amount <= 0:
            raise _bad("amount_invalid", "The base amount rounds to zero.")
        receipt_plans: tuple[ReceiptAllocationPlan, ...] = ()
        payment_plans: tuple[PaymentAllocationPlan, ...] = ()
        if request.direction == "received":
            if request.customer_id is None:
                raise _bad("cheque_customer_required", "A received cheque needs a customer.")
            await _load_customer(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                customer_id=request.customer_id,
            )
            receipt_plans, unallocated, _ = await _prepare_receipt_allocations(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                customer_id=request.customer_id,
                currency=currency,
                exchange_rate=request.exchange_rate,
                amount_minor=request.amount_minor,
                allocations=request.allocations,
            )
            status = "in_hand"
        else:
            if request.supplier_id is None:
                raise _bad("cheque_supplier_required", "An issued cheque needs a supplier.")
            await _load_supplier(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                supplier_id=request.supplier_id,
            )
            payment_plans, unallocated, _ = await _prepare_payment_allocations(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                supplier_id=request.supplier_id,
                currency=currency,
                exchange_rate=request.exchange_rate,
                amount_minor=request.amount_minor,
                allocations=request.allocations,
            )
            status = "issued"
        cheque = Cheque(
            tenant_id=tenant_id,
            company_id=request.company_id,
            direction=request.direction,
            number=request.number.strip(),
            customer_id=request.customer_id,
            supplier_id=request.supplier_id,
            cash_bank_account_id=cash_account.id,
            bank_name=request.bank_name,
            party_name=request.party_name,
            issue_date=request.issue_date,
            due_date=due_date,
            currency=currency,
            exchange_rate=request.exchange_rate,
            amount_minor=request.amount_minor,
            base_amount_minor=base_amount,
            unallocated_minor=unallocated,
            status=status,
            memo=request.memo,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(cheque)
        await session.flush()
        allocations = tuple(
            ChequeAllocation(
                tenant_id=tenant_id,
                company_id=request.company_id,
                cheque_id=cheque.id,
                invoice_id=plan.invoice.id,
                bill_id=None,
                amount_minor=plan.amount_minor,
                base_amount_minor=plan.base_amount_minor,
                settlement_base_minor=plan.settlement_base_minor,
            )
            for plan in receipt_plans
        ) + tuple(
            ChequeAllocation(
                tenant_id=tenant_id,
                company_id=request.company_id,
                cheque_id=cheque.id,
                invoice_id=None,
                bill_id=plan.bill.id,
                amount_minor=plan.amount_minor,
                base_amount_minor=plan.base_amount_minor,
                settlement_base_minor=plan.settlement_base_minor,
            )
            for plan in payment_plans
        )
        session.add_all(allocations)
        event = ChequeEvent(
            tenant_id=tenant_id,
            company_id=request.company_id,
            cheque_id=cheque.id,
            event_type="received" if request.direction == "received" else "issued",
            event_date=request.issue_date,
            notes=request.memo,
        )
        session.add(event)
        await session.flush()
        if request.post:
            if request.direction == "received":
                entry = await _post_cheque_received_in_session(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    cheque=cheque,
                    event=event,
                    plans=receipt_plans,
                )
                for plan in receipt_plans:
                    settled_total = plan.allocated_before_minor + plan.amount_minor
                    plan.invoice.status = (
                        "paid" if settled_total >= plan.invoice.total_minor else "part_paid"
                    )
            else:
                entry = await _post_cheque_issued_in_session(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    cheque=cheque,
                    event=event,
                    plans=payment_plans,
                )
                for plan in payment_plans:
                    settled_total = plan.allocated_before_minor + plan.amount_minor
                    plan.bill.status = (
                        "paid" if settled_total >= plan.bill.total_minor else "part_paid"
                    )
            event.journal_entry_id = entry.entry.id
            await session.flush()
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="cashbank.cheque.posted" if request.post else "cashbank.cheque.created",
            entity_type="cheque",
            entity_id=cheque.id,
            detail={
                "number": cheque.number,
                "direction": cheque.direction,
                "amount_minor": cheque.amount_minor,
                "status": cheque.status,
            },
        )
        return await _cheque_payload(
            session, tenant_id=tenant_id, company_id=request.company_id, cheque=cheque
        )

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_account(error) from None
    except DBAPIError as error:
        raise _translate_account(error) from None


async def _post_cheque_event(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    cheque_id: UUID,
    event_type: str,
    event_date: date,
    notes: str | None,
    allowed_from: set[str],
    new_status: str,
    journal: str,
) -> ChequeResult:
    """Move one cheque to a new status, posting the value movement when needed."""

    async def operation(session: AsyncSession) -> ChequeResult:
        cheque = await _load_cheque(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            cheque_id=cheque_id,
            for_update=True,
        )
        if cheque.status not in allowed_from:
            raise _conflict(
                "cheque_status_invalid",
                f"Cheque {cheque.number} is {cheque.status}; the requested event is not allowed.",
            )
        journal_kind = journal
        if journal_kind == "cancel":
            journal_kind = "receive" if cheque.direction == "received" else "issue_reverse"
        cash_account = await _load_bank_account_for_currency(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            cash_bank_account_id=cheque.cash_bank_account_id,
            currency=cheque.currency,
        )
        period = await _load_period(
            session, tenant_id=tenant_id, company_id=company_id, on_date=event_date
        )
        event = ChequeEvent(
            tenant_id=tenant_id,
            company_id=company_id,
            cheque_id=cheque.id,
            event_type=event_type,
            event_date=event_date,
            notes=notes,
        )
        session.add(event)
        await session.flush()
        entry: EntryResult | None = None
        if journal_kind == "deposit":
            if cheque.status != "in_hand":
                raise _conflict("cheque_status_invalid", "Only a cheque in hand can be deposited.")
            accounts = await resolve_posting_accounts(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                on_date=event_date,
                keys=(CHEQUES_UNDER_COLLECTION_ACCOUNT,),
            )
            entry = await post_entry_in_session(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                company_id=company_id,
                period_id=period.id,
                entry_date=event_date,
                memo=notes or f"Deposit cheque {cheque.number}",
                lines=(
                    JournalLineInput(
                        account_id=cash_account.ledger_account_id,
                        debit_minor=cheque.base_amount_minor,
                        description=f"Deposit cheque {cheque.number}",
                    ),
                    JournalLineInput(
                        account_id=accounts[CHEQUES_UNDER_COLLECTION_ACCOUNT].id,
                        credit_minor=cheque.base_amount_minor,
                        description=f"Deposit cheque {cheque.number}",
                    ),
                ),
                source_type="cheque_event",
                source_id=event.id,
            )
        elif journal_kind == "receive":
            accounts = await resolve_posting_accounts(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                on_date=event_date,
                keys=(
                    CHEQUES_UNDER_COLLECTION_ACCOUNT,
                    AR_ACCOUNT,
                    CUSTOMER_ADVANCES_ACCOUNT,
                    FX_GAIN_ACCOUNT,
                    FX_LOSS_ACCOUNT,
                ),
            )
            allocations = tuple(
                (
                    await session.execute(
                        select(ChequeAllocation).where(
                            ChequeAllocation.tenant_id == tenant_id,
                            ChequeAllocation.company_id == company_id,
                            ChequeAllocation.cheque_id == cheque.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            receivable_base = sum(
                allocation.base_amount_minor
                for allocation in allocations
                if not allocation.is_reversed
            )
            unallocated_base = cheque.base_amount_minor - sum(
                allocation.settlement_base_minor
                for allocation in allocations
                if not allocation.is_reversed
            )
            fx_difference = cheque.base_amount_minor - receivable_base - unallocated_base
            lines = [
                JournalLineInput(
                    account_id=(
                        cash_account.ledger_account_id
                        if cheque.status == "deposited"
                        else accounts[CHEQUES_UNDER_COLLECTION_ACCOUNT].id
                    ),
                    credit_minor=cheque.base_amount_minor,
                    description=f"Cheque {cheque.number} reversed",
                )
            ]
            if receivable_base > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[AR_ACCOUNT].id,
                        debit_minor=receivable_base,
                        description=f"Cheque {cheque.number} receivable restored",
                    )
                )
            if unallocated_base > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[CUSTOMER_ADVANCES_ACCOUNT].id,
                        debit_minor=unallocated_base,
                        description=f"Cheque {cheque.number} advance restored",
                    )
                )
            if fx_difference > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[FX_LOSS_ACCOUNT].id,
                        debit_minor=fx_difference,
                        description=f"Cheque {cheque.number} exchange loss reversed",
                    )
                )
            elif fx_difference < 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[FX_GAIN_ACCOUNT].id,
                        credit_minor=-fx_difference,
                        description=f"Cheque {cheque.number} exchange gain reversed",
                    )
                )
            entry = await post_entry_in_session(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                company_id=company_id,
                period_id=period.id,
                entry_date=event_date,
                memo=notes or f"Reverse cheque {cheque.number}",
                lines=tuple(lines),
                source_type="cheque_event",
                source_id=event.id,
            )
            await _reverse_cheque_allocations(
                session, tenant_id=tenant_id, company_id=company_id, cheque=cheque
            )
        elif journal_kind == "issue_reverse":
            accounts = await resolve_posting_accounts(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                on_date=event_date,
                keys=(
                    CHEQUES_ISSUED_ACCOUNT,
                    AP_ACCOUNT,
                    SUPPLIER_ADVANCES_ACCOUNT,
                    FX_GAIN_ACCOUNT,
                    FX_LOSS_ACCOUNT,
                ),
            )
            allocations = tuple(
                (
                    await session.execute(
                        select(ChequeAllocation).where(
                            ChequeAllocation.tenant_id == tenant_id,
                            ChequeAllocation.company_id == company_id,
                            ChequeAllocation.cheque_id == cheque.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            payable_base = sum(
                allocation.base_amount_minor
                for allocation in allocations
                if not allocation.is_reversed
            )
            unallocated_base = cheque.base_amount_minor - sum(
                allocation.settlement_base_minor
                for allocation in allocations
                if not allocation.is_reversed
            )
            fx_difference = cheque.base_amount_minor - payable_base - unallocated_base
            lines = [
                JournalLineInput(
                    account_id=accounts[CHEQUES_ISSUED_ACCOUNT].id,
                    debit_minor=cheque.base_amount_minor,
                    description=f"Cheque {cheque.number} cancelled",
                )
            ]
            if payable_base > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[AP_ACCOUNT].id,
                        credit_minor=payable_base,
                        description=f"Cheque {cheque.number} payable restored",
                    )
                )
            if unallocated_base > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[SUPPLIER_ADVANCES_ACCOUNT].id,
                        credit_minor=unallocated_base,
                        description=f"Cheque {cheque.number} advance restored",
                    )
                )
            if fx_difference > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[FX_GAIN_ACCOUNT].id,
                        credit_minor=fx_difference,
                        description=f"Cheque {cheque.number} exchange gain reversed",
                    )
                )
            elif fx_difference < 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[FX_LOSS_ACCOUNT].id,
                        debit_minor=-fx_difference,
                        description=f"Cheque {cheque.number} exchange loss reversed",
                    )
                )
            entry = await post_entry_in_session(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                company_id=company_id,
                period_id=period.id,
                entry_date=event_date,
                memo=notes or f"Reverse issued cheque {cheque.number}",
                lines=tuple(lines),
                source_type="cheque_event",
                source_id=event.id,
            )
            await _reverse_cheque_allocations(
                session, tenant_id=tenant_id, company_id=company_id, cheque=cheque
            )
        elif journal_kind == "clear_issued":
            accounts = await resolve_posting_accounts(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                on_date=event_date,
                keys=(CHEQUES_ISSUED_ACCOUNT,),
            )
            entry = await post_entry_in_session(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                company_id=company_id,
                period_id=period.id,
                entry_date=event_date,
                memo=notes or f"Clear cheque {cheque.number}",
                lines=(
                    JournalLineInput(
                        account_id=accounts[CHEQUES_ISSUED_ACCOUNT].id,
                        debit_minor=cheque.base_amount_minor,
                        description=f"Cheque {cheque.number} cleared",
                    ),
                    JournalLineInput(
                        account_id=cash_account.ledger_account_id,
                        credit_minor=cheque.base_amount_minor,
                        description=f"Cheque {cheque.number} cleared",
                    ),
                ),
                source_type="cheque_event",
                source_id=event.id,
            )
        if entry is not None:
            event.journal_entry_id = entry.entry.id
        cheque.status = new_status
        await session.flush()
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=f"cashbank.cheque.{event_type}",
            entity_type="cheque",
            entity_id=cheque.id,
            detail={"number": cheque.number, "status": new_status},
        )
        return await _cheque_payload(
            session, tenant_id=tenant_id, company_id=company_id, cheque=cheque
        )

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_account(error) from None
    except DBAPIError as error:
        raise _translate_account(error) from None


async def deposit_cheque(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    cheque_id: UUID,
    event_date: date,
    notes: str | None = None,
) -> ChequeResult:
    """Record a received cheque as deposited into its bank account."""
    return await _post_cheque_event(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        company_id=company_id,
        cheque_id=cheque_id,
        event_type="deposited",
        event_date=event_date,
        notes=notes,
        allowed_from={"in_hand"},
        new_status="deposited",
        journal="deposit",
    )


async def clear_cheque(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    cheque_id: UUID,
    event_date: date,
    notes: str | None = None,
) -> ChequeResult:
    """Mark a cheque as cleared; a received cheque moves no value at clearing."""

    async def operation(session: AsyncSession) -> ChequeResult:
        cheque = await _load_cheque(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            cheque_id=cheque_id,
            for_update=True,
        )
        if cheque.direction == "received":
            allowed = {"deposited"}
            journal = "none"
        else:
            allowed = {"issued"}
            journal = "clear_issued"
        if cheque.status not in allowed:
            raise _conflict(
                "cheque_status_invalid",
                f"Cheque {cheque.number} is {cheque.status}; the requested event is not allowed.",
            )
        event = ChequeEvent(
            tenant_id=tenant_id,
            company_id=company_id,
            cheque_id=cheque.id,
            event_type="cleared",
            event_date=event_date,
            notes=notes,
        )
        session.add(event)
        await session.flush()
        if journal == "clear_issued":
            cash_account = await _load_bank_account_for_currency(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                cash_bank_account_id=cheque.cash_bank_account_id,
                currency=cheque.currency,
            )
            period = await _load_period(
                session, tenant_id=tenant_id, company_id=company_id, on_date=event_date
            )
            accounts = await resolve_posting_accounts(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                on_date=event_date,
                keys=(CHEQUES_ISSUED_ACCOUNT,),
            )
            entry = await post_entry_in_session(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                company_id=company_id,
                period_id=period.id,
                entry_date=event_date,
                memo=notes or f"Clear cheque {cheque.number}",
                lines=(
                    JournalLineInput(
                        account_id=accounts[CHEQUES_ISSUED_ACCOUNT].id,
                        debit_minor=cheque.base_amount_minor,
                        description=f"Cheque {cheque.number} cleared",
                    ),
                    JournalLineInput(
                        account_id=cash_account.ledger_account_id,
                        credit_minor=cheque.base_amount_minor,
                        description=f"Cheque {cheque.number} cleared",
                    ),
                ),
                source_type="cheque_event",
                source_id=event.id,
            )
            event.journal_entry_id = entry.entry.id
        cheque.status = "cleared"
        await session.flush()
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="cashbank.cheque.cleared",
            entity_type="cheque",
            entity_id=cheque.id,
            detail={"number": cheque.number},
        )
        return await _cheque_payload(
            session, tenant_id=tenant_id, company_id=company_id, cheque=cheque
        )

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_account(error) from None
    except DBAPIError as error:
        raise _translate_account(error) from None


async def bounce_cheque(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    cheque_id: UUID,
    event_date: date,
    notes: str | None = None,
) -> ChequeResult:
    """Record a bounced cheque, restoring the receivable/payable and allocations."""

    async def operation(session: AsyncSession) -> ChequeResult:
        cheque = await _load_cheque(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            cheque_id=cheque_id,
            for_update=True,
        )
        allowed = {"in_hand", "deposited"} if cheque.direction == "received" else {"issued"}
        if cheque.status not in allowed:
            raise _conflict(
                "cheque_status_invalid",
                f"Cheque {cheque.number} is {cheque.status}; a bounce is not allowed now.",
            )
        event = ChequeEvent(
            tenant_id=tenant_id,
            company_id=company_id,
            cheque_id=cheque.id,
            event_type="bounced",
            event_date=event_date,
            notes=notes,
        )
        session.add(event)
        await session.flush()
        period = await _load_period(
            session, tenant_id=tenant_id, company_id=company_id, on_date=event_date
        )
        accounts = await resolve_posting_accounts(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            on_date=event_date,
            keys=(
                CHEQUES_UNDER_COLLECTION_ACCOUNT,
                AR_ACCOUNT,
                CUSTOMER_ADVANCES_ACCOUNT,
                FX_GAIN_ACCOUNT,
                FX_LOSS_ACCOUNT,
            )
            if cheque.direction == "received"
            else (
                CHEQUES_ISSUED_ACCOUNT,
                AP_ACCOUNT,
                SUPPLIER_ADVANCES_ACCOUNT,
                FX_GAIN_ACCOUNT,
                FX_LOSS_ACCOUNT,
            ),
        )
        allocations = tuple(
            (
                await session.execute(
                    select(ChequeAllocation).where(
                        ChequeAllocation.tenant_id == tenant_id,
                        ChequeAllocation.company_id == company_id,
                        ChequeAllocation.cheque_id == cheque.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        settled = sum(
            allocation.base_amount_minor for allocation in allocations if not allocation.is_reversed
        )
        allocated = sum(
            allocation.settlement_base_minor
            for allocation in allocations
            if not allocation.is_reversed
        )
        unallocated = cheque.base_amount_minor - allocated
        if cheque.direction == "received":
            cash_account = await _load_bank_account_for_currency(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                cash_bank_account_id=cheque.cash_bank_account_id,
                currency=cheque.currency,
            )
            fx_difference = cheque.base_amount_minor - settled - unallocated
            lines = [
                JournalLineInput(
                    account_id=(
                        cash_account.ledger_account_id
                        if cheque.status == "deposited"
                        else accounts[CHEQUES_UNDER_COLLECTION_ACCOUNT].id
                    ),
                    credit_minor=cheque.base_amount_minor,
                    description=f"Cheque {cheque.number} bounced",
                )
            ]
            if settled > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[AR_ACCOUNT].id,
                        debit_minor=settled,
                        description=f"Cheque {cheque.number} receivable restored",
                    )
                )
            if unallocated > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[CUSTOMER_ADVANCES_ACCOUNT].id,
                        debit_minor=unallocated,
                        description=f"Cheque {cheque.number} advance restored",
                    )
                )
            if fx_difference > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[FX_LOSS_ACCOUNT].id,
                        debit_minor=fx_difference,
                        description=f"Cheque {cheque.number} exchange loss",
                    )
                )
            elif fx_difference < 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[FX_GAIN_ACCOUNT].id,
                        credit_minor=-fx_difference,
                        description=f"Cheque {cheque.number} exchange gain",
                    )
                )
        else:
            fx_difference = cheque.base_amount_minor - settled - unallocated
            lines = [
                JournalLineInput(
                    account_id=accounts[CHEQUES_ISSUED_ACCOUNT].id,
                    debit_minor=cheque.base_amount_minor,
                    description=f"Cheque {cheque.number} bounced",
                )
            ]
            if settled > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[AP_ACCOUNT].id,
                        credit_minor=settled,
                        description=f"Cheque {cheque.number} payable restored",
                    )
                )
            if unallocated > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[SUPPLIER_ADVANCES_ACCOUNT].id,
                        credit_minor=unallocated,
                        description=f"Cheque {cheque.number} advance restored",
                    )
                )
            if fx_difference > 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[FX_GAIN_ACCOUNT].id,
                        credit_minor=fx_difference,
                        description=f"Cheque {cheque.number} exchange gain",
                    )
                )
            elif fx_difference < 0:
                lines.append(
                    JournalLineInput(
                        account_id=accounts[FX_LOSS_ACCOUNT].id,
                        debit_minor=-fx_difference,
                        description=f"Cheque {cheque.number} exchange loss",
                    )
                )
        entry = await post_entry_in_session(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            company_id=company_id,
            period_id=period.id,
            entry_date=event_date,
            memo=notes or f"Bounce cheque {cheque.number}",
            lines=tuple(lines),
            source_type="cheque_event",
            source_id=event.id,
        )
        event.journal_entry_id = entry.entry.id
        await _reverse_cheque_allocations(
            session, tenant_id=tenant_id, company_id=company_id, cheque=cheque
        )
        cheque.status = "bounced"
        await session.flush()
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="cashbank.cheque.bounced",
            entity_type="cheque",
            entity_id=cheque.id,
            detail={"number": cheque.number},
        )
        return await _cheque_payload(
            session, tenant_id=tenant_id, company_id=company_id, cheque=cheque
        )

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_account(error) from None
    except DBAPIError as error:
        raise _translate_account(error) from None


async def cancel_cheque(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    cheque_id: UUID,
    event_date: date,
    notes: str | None = None,
) -> ChequeResult:
    """Cancel a cheque that has not been deposited or cleared."""
    return await _post_cheque_event(
        unit_of_work,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        company_id=company_id,
        cheque_id=cheque_id,
        event_type="cancelled",
        event_date=event_date,
        notes=notes,
        allowed_from={"in_hand", "issued"},
        new_status="cancelled",
        journal="cancel",
    )


async def get_cheque(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, cheque_id: UUID
) -> ChequeResult:
    """Read one cheque with its allocations and lifecycle events."""
    cheque = await _load_cheque(
        session, tenant_id=tenant_id, company_id=company_id, cheque_id=cheque_id
    )
    return await _cheque_payload(session, tenant_id=tenant_id, company_id=company_id, cheque=cheque)


async def list_cheques(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, direction: str | None = None
) -> list[Cheque]:
    """List cheques newest first, optionally filtered by direction."""
    statement = select(Cheque).where(Cheque.tenant_id == tenant_id, Cheque.company_id == company_id)
    if direction is not None:
        statement = statement.where(Cheque.direction == direction)
    return list(
        (
            await session.execute(
                statement.order_by(Cheque.issue_date.desc(), Cheque.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


@dataclass(frozen=True)
class BankStatementLineInput:
    """One line of an imported bank statement; positive means money in."""

    statement_date: date
    description: str
    amount_minor: int
    reference: str | None = None


BANK_STATEMENT_SOURCE_TYPES = frozenset(
    {"receipt", "supplier_payment", "cheque_event", "cash_transfer"}
)


async def import_bank_statement_lines(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    cash_bank_account_id: UUID,
    currency: str,
    lines: tuple[BankStatementLineInput, ...],
) -> list[BankStatementLine]:
    """Import bank statement lines as unmatched, all or nothing."""
    normalized_currency = _validate_currency(currency)
    if not lines:
        raise _bad("bank_statement_empty", "A bank statement import needs at least one line.")
    if len(lines) > 5000:
        raise _bad("bank_statement_too_large", "A bank statement import is limited to 5000 lines.")
    for line in lines:
        if line.amount_minor == 0:
            raise _bad("bank_statement_amount_invalid", "A statement line amount cannot be zero.")
        if not line.description.strip() or len(line.description) > 500:
            raise _bad(
                "bank_statement_description_invalid",
                "A statement line description is 1-500 characters.",
            )
        if line.reference is not None and len(line.reference) > 100:
            raise _bad("bank_statement_reference_invalid", "A reference is at most 100 characters.")

    async def operation(session: AsyncSession) -> list[BankStatementLine]:
        account = await _load_bank_account_for_currency(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            cash_bank_account_id=cash_bank_account_id,
            currency=normalized_currency,
        )
        created: list[BankStatementLine] = []
        for row in lines:
            record = BankStatementLine(
                tenant_id=tenant_id,
                company_id=company_id,
                cash_bank_account_id=account.id,
                statement_date=row.statement_date,
                description=row.description.strip(),
                reference=row.reference.strip() if row.reference else None,
                amount_minor=row.amount_minor,
                currency=account.currency,
                status="unmatched",
                created_by=actor_user_id,
                updated_by=actor_user_id,
            )
            session.add(record)
            created.append(record)
        await session.flush()
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="cashbank.bank_statement.imported",
            entity_type="cash_bank_account",
            entity_id=account.id,
            detail={"lines": len(created)},
        )
        return created

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_account(error) from None
    except DBAPIError as error:
        raise _translate_account(error) from None


async def list_bank_statement_lines(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    cash_bank_account_id: UUID | None = None,
    status: str | None = None,
) -> list[BankStatementLine]:
    """List bank statement lines newest first, optionally filtered."""
    statement = select(BankStatementLine).where(
        BankStatementLine.tenant_id == tenant_id,
        BankStatementLine.company_id == company_id,
    )
    if cash_bank_account_id is not None:
        statement = statement.where(BankStatementLine.cash_bank_account_id == cash_bank_account_id)
    if status is not None:
        statement = statement.where(BankStatementLine.status == status)
    return list(
        (
            await session.execute(
                statement.order_by(
                    BankStatementLine.statement_date.desc(), BankStatementLine.created_at.desc()
                )
            )
        )
        .scalars()
        .all()
    )


async def match_bank_statement_line(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    line_id: UUID,
    source_type: str,
    source_id: UUID,
) -> BankStatementLine:
    """Match one statement line to the posted document that moved the money.

    Matching never edits the posted document: it only records which existing
    journal entry the bank line corresponds to, after checking the amount and
    side against that entry's cash/bank line.
    """
    if source_type not in BANK_STATEMENT_SOURCE_TYPES:
        raise _bad("bank_match_source_invalid", "The match source type is not supported.")

    async def operation(session: AsyncSession) -> BankStatementLine:
        line = (
            await session.execute(
                select(BankStatementLine)
                .where(
                    BankStatementLine.tenant_id == tenant_id,
                    BankStatementLine.company_id == company_id,
                    BankStatementLine.id == line_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if line is None:
            raise _not_found(
                "bank_statement_line_not_found",
                "The statement line does not exist in the selected company.",
            )
        if line.status == "matched":
            raise _conflict("bank_statement_line_matched", "The statement line is already matched.")
        account = await _load_bank_account_for_currency(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            cash_bank_account_id=line.cash_bank_account_id,
            currency=line.currency,
        )
        entry_id = await _resolve_bank_match_source(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            line=line,
            source_type=source_type,
            source_id=source_id,
        )
        amount = abs(line.amount_minor)
        side_filter = (
            JournalLine.debit_minor == amount
            if line.amount_minor > 0
            else JournalLine.credit_minor == amount
        )
        journal_line = (
            await session.execute(
                select(JournalLine.id).where(
                    JournalLine.tenant_id == tenant_id,
                    JournalLine.company_id == company_id,
                    JournalLine.entry_id == entry_id,
                    JournalLine.account_id == account.ledger_account_id,
                    side_filter,
                )
            )
        ).first()
        if journal_line is None:
            raise _conflict(
                "bank_match_amount_mismatch",
                "The statement line amount or direction does not match the posted entry.",
            )
        line.status = "matched"
        line.matched_source_type = source_type
        line.matched_source_id = source_id
        line.matched_at = datetime.now(UTC)
        line.matched_by = actor_user_id
        line.updated_by = actor_user_id
        await session.flush()
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="cashbank.bank_statement.matched",
            entity_type="bank_statement_line",
            entity_id=line.id,
            detail={"source_type": source_type, "source_id": str(source_id)},
        )
        return line

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_account(error) from None
    except DBAPIError as error:
        raise _translate_account(error) from None


async def unmatch_bank_statement_line(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    line_id: UUID,
) -> BankStatementLine:
    """Undo a match, leaving the posted documents untouched."""

    async def operation(session: AsyncSession) -> BankStatementLine:
        line = (
            await session.execute(
                select(BankStatementLine)
                .where(
                    BankStatementLine.tenant_id == tenant_id,
                    BankStatementLine.company_id == company_id,
                    BankStatementLine.id == line_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if line is None:
            raise _not_found(
                "bank_statement_line_not_found",
                "The statement line does not exist in the selected company.",
            )
        if line.status != "matched":
            raise _conflict("bank_statement_line_unmatched", "The statement line is not matched.")
        line.status = "unmatched"
        line.matched_source_type = None
        line.matched_source_id = None
        line.matched_at = None
        line.matched_by = None
        line.updated_by = actor_user_id
        await session.flush()
        await _record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="cashbank.bank_statement.unmatched",
            entity_type="bank_statement_line",
            entity_id=line.id,
        )
        return line

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_account(error) from None
    except DBAPIError as error:
        raise _translate_account(error) from None


async def _resolve_bank_match_source(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    line: BankStatementLine,
    source_type: str,
    source_id: UUID,
) -> UUID:
    """Return the journal entry of the source and check it moved this account."""
    if source_type == "receipt":
        receipt = (
            await session.execute(
                select(Receipt).where(
                    Receipt.tenant_id == tenant_id,
                    Receipt.company_id == company_id,
                    Receipt.id == source_id,
                )
            )
        ).scalar_one_or_none()
        if receipt is None:
            raise _not_found("receipt_not_found", "The receipt does not exist in the company.")
        if receipt.cash_bank_account_id != line.cash_bank_account_id:
            raise _conflict(
                "bank_match_account_mismatch",
                "The receipt belongs to a different cash or bank account.",
            )
        if receipt.journal_entry_id is None:
            raise _conflict("bank_match_not_posted", "The receipt has not been posted.")
        return receipt.journal_entry_id
    if source_type == "supplier_payment":
        payment = (
            await session.execute(
                select(SupplierPayment).where(
                    SupplierPayment.tenant_id == tenant_id,
                    SupplierPayment.company_id == company_id,
                    SupplierPayment.id == source_id,
                )
            )
        ).scalar_one_or_none()
        if payment is None:
            raise _not_found("payment_not_found", "The payment does not exist in the company.")
        if payment.cash_bank_account_id != line.cash_bank_account_id:
            raise _conflict(
                "bank_match_account_mismatch",
                "The payment belongs to a different cash or bank account.",
            )
        if payment.journal_entry_id is None:
            raise _conflict("bank_match_not_posted", "The payment has not been posted.")
        return payment.journal_entry_id
    if source_type == "cheque_event":
        event = (
            await session.execute(
                select(ChequeEvent)
                .join(Cheque, Cheque.id == ChequeEvent.cheque_id)
                .where(
                    ChequeEvent.tenant_id == tenant_id,
                    ChequeEvent.company_id == company_id,
                    ChequeEvent.id == source_id,
                )
            )
        ).scalar_one_or_none()
        if event is None:
            raise _not_found(
                "cheque_event_not_found", "The cheque event does not exist in the company."
            )
        cheque = await session.get(Cheque, event.cheque_id)
        if cheque is None or cheque.cash_bank_account_id != line.cash_bank_account_id:
            raise _conflict(
                "bank_match_account_mismatch",
                "The cheque belongs to a different cash or bank account.",
            )
        if event.journal_entry_id is None:
            raise _conflict("bank_match_not_posted", "The cheque event posted no movement.")
        return event.journal_entry_id
    transfer = (
        await session.execute(
            select(CashTransfer).where(
                CashTransfer.tenant_id == tenant_id,
                CashTransfer.company_id == company_id,
                CashTransfer.id == source_id,
            )
        )
    ).scalar_one_or_none()
    if transfer is None:
        raise _not_found("cash_transfer_not_found", "The transfer does not exist in the company.")
    if line.cash_bank_account_id not in {transfer.from_account_id, transfer.to_account_id}:
        raise _conflict(
            "bank_match_account_mismatch",
            "The transfer does not involve this cash or bank account.",
        )
    if transfer.journal_entry_id is None:
        raise _conflict("bank_match_not_posted", "The transfer has no journal entry.")
    return transfer.journal_entry_id


def _translate_account(error: IntegrityError | DBAPIError) -> AppError:
    """Map a database error on an account, transfer or cheque write to a problem."""
    message = str(getattr(error, "orig", error)).lower()
    if "uq_cash_bank_account_tenant_company_code" in message:
        return _conflict(
            "account_code_conflict",
            "A cash or bank account with this code already exists in the company.",
        )
    if "uq_cash_transfer_tenant_company_number" in message:
        return _conflict(
            "cash_transfer_number_conflict",
            "A cash transfer with this number already exists in the company.",
        )
    if "foreign key" in message:
        return _not_found(
            "account_reference_invalid",
            "A referenced account or document does not exist in the selected company.",
        )
    if not isinstance(error, IntegrityError):
        return AppError(
            code="database_unavailable",
            title="Service unavailable",
            status_code=503,
            detail="The database could not complete the request; retry shortly.",
        )
    return _conflict("database_conflict", "The write conflicts with an existing record.")


def _translate_receipt(error: IntegrityError | DBAPIError) -> AppError:
    """Map a database error on a receipt write to a problem."""
    message = str(getattr(error, "orig", error)).lower()
    if "uq_receipt_tenant_company_number" in message:
        return _conflict(
            "receipt_number_conflict", "A receipt with this number already exists in the company."
        )
    if "fk_receipt_customer" in message:
        return _not_found("customer_not_found", "The customer does not exist in the company.")
    if "fk_receipt_allocation" in message or "uq_receipt_allocation" in message:
        return _conflict(
            "receipt_allocation_conflict", "A receipt allocation refers to a missing invoice."
        )
    if not isinstance(error, IntegrityError):
        return AppError(
            code="database_unavailable",
            title="Service unavailable",
            status_code=503,
            detail="The database could not complete the request; retry shortly.",
        )
    return _conflict("database_conflict", "The write conflicts with an existing record.")


def _translate_payment(error: IntegrityError | DBAPIError) -> AppError:
    """Map a database error on a supplier payment write to a problem."""
    message = str(getattr(error, "orig", error)).lower()
    if "uq_supplier_payment_tenant_company_number" in message:
        return _conflict(
            "payment_number_conflict", "A payment with this number already exists in the company."
        )
    if "fk_supplier_payment_supplier" in message:
        return _not_found("supplier_not_found", "The supplier does not exist in the company.")
    if "fk_supplier_payment_allocation" in message or "uq_supplier_payment_allocation" in message:
        return _conflict(
            "payment_allocation_conflict", "A payment allocation refers to a missing bill."
        )
    if not isinstance(error, IntegrityError):
        return AppError(
            code="database_unavailable",
            title="Service unavailable",
            status_code=503,
            detail="The database could not complete the request; retry shortly.",
        )
    return _conflict("database_conflict", "The write conflicts with an existing record.")


async def _record_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    action: str,
    entity_type: str,
    entity_id: UUID,
    detail: dict[str, object] | None = None,
) -> None:
    """Append one cash-and-bank audit event lazily, keeping imports flat."""
    from app.modules.identity.audit import record_audit

    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail,
    )
