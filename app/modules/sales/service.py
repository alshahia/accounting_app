"""Sales invoice creation and posting (phase 0, day 7).

A draft is stored with its computed lines; posting runs in one transaction and
creates the balanced journal entry (receivable / revenue / VAT), issues stock
for stocked lines at weighted-average cost and posts a second cost-of-sales
entry, then marks the invoice posted with links to both entries. Duplicate
document numbers are refused by the unique constraint and mapped to 409. The
document currency amount is converted to base currency at the invoice's own
exchange rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import Customer, Item, TaxCode
from app.modules.catalog.tax import rates_on as tax_rates_on
from app.modules.inventory.service import issue_stock
from app.modules.ledger.mapping import resolve_posting_accounts
from app.modules.ledger.models import AccountingPeriod
from app.modules.ledger.service import (
    EntryResult,
    JournalLineInput,
    post_entry_in_session,
)
from app.modules.sales.models import SalesInvoice, SalesInvoiceLine
from app.shared.errors import AppError
from app.shared.numbering import allocate_document_number
from app.shared.unit_of_work import UnitOfWork

NUMBER_SCOPE = "sales_invoice"
NUMBER_PREFIX = "SI"


@dataclass(frozen=True)
class SalesLineInput:
    """One invoice line in the document currency."""

    description: str
    quantity: Decimal
    unit_price_minor: int
    item_id: UUID | None = None
    tax_code_id: UUID | None = None


@dataclass(frozen=True)
class SalesInvoiceInput:
    """Everything needed to create a draft or posted customer invoice."""

    company_id: UUID
    customer_id: UUID
    invoice_date: date
    lines: tuple[SalesLineInput, ...]
    due_date: date | None = None
    currency: str = "IQD"
    exchange_rate: Decimal = Decimal("1")
    memo: str | None = None
    number: str | None = None
    post: bool = False


@dataclass(frozen=True)
class SalesInvoiceResult:
    """The invoice, its lines and the entries posting produced (if any)."""

    invoice: SalesInvoice
    lines: tuple[SalesInvoiceLine, ...]
    journal_entry: EntryResult | None
    cogs_entry: EntryResult | None


def bad(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Unprocessable entity", status_code=422, detail=detail)


def conflict(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Conflict", status_code=409, detail=detail)


def minor(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def load_customer(
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
        raise AppError(
            code="customer_not_found",
            title="Not found",
            status_code=404,
            detail="The customer does not exist in the selected company.",
        )
    if not customer.is_active:
        raise bad("customer_inactive", f"Customer {customer.code} is not active.")
    return customer


async def load_tax_rates(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, ids: set[UUID]
) -> dict[UUID, TaxCode]:
    if not ids:
        return {}
    rows = (
        (
            await session.execute(
                select(TaxCode).where(
                    TaxCode.tenant_id == tenant_id,
                    TaxCode.company_id == company_id,
                    TaxCode.id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    rates = {row.id: row for row in rows}
    missing = ids - set(rates)
    if missing:
        raise AppError(
            code="tax_code_not_found",
            title="Not found",
            status_code=404,
            detail="One or more tax codes do not exist in the selected company.",
        )
    for tax_code in rates.values():
        if not tax_code.is_active:
            raise bad("tax_code_inactive", f"Tax code {tax_code.code} is not active.")
        if tax_code.kind == "withholding":
            raise bad(
                "tax_code_not_applicable",
                f"Tax code {tax_code.code} is a withholding code and applies at payment.",
            )
    return rates


async def load_items(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, ids: set[UUID]
) -> dict[UUID, Item]:
    if not ids:
        return {}
    rows = (
        (
            await session.execute(
                select(Item).where(
                    Item.tenant_id == tenant_id,
                    Item.company_id == company_id,
                    Item.id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    items = {row.id: row for row in rows}
    missing = ids - set(items)
    if missing:
        raise AppError(
            code="item_not_found",
            title="Not found",
            status_code=404,
            detail="One or more items do not exist in the selected company.",
        )
    for item in items.values():
        if not item.is_active:
            raise bad("item_inactive", f"Item {item.code} is not active.")
    return items


async def resolve_open_period(
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
        raise AppError(
            code="period_not_found",
            title="Not found",
            status_code=404,
            detail=f"No accounting period covers {on_date.isoformat()}.",
        )
    if period.status != "open":
        raise conflict("period_closed", "The accounting period for this date is closed.")
    return period


def _validate_input(request: SalesInvoiceInput) -> None:
    if not request.lines:
        raise bad("invoice_lines_invalid", "An invoice needs at least one line.")
    if len(request.lines) > 200:
        raise bad("invoice_lines_invalid", "An invoice can have at most 200 lines.")
    if len(request.currency) != 3 or not request.currency.isalpha():
        raise bad("currency_invalid", "The currency must be a three-letter ISO code.")
    if request.exchange_rate <= 0:
        raise bad("exchange_rate_invalid", "The exchange rate must be positive.")
    if request.number is not None and not 1 <= len(request.number) <= 40:
        raise bad("document_number_invalid", "A document number is 1-40 characters.")
    for index, line in enumerate(request.lines, start=1):
        if line.quantity <= 0:
            raise bad("invoice_lines_invalid", f"Line {index} must have a positive quantity.")
        if line.unit_price_minor <= 0:
            raise bad("invoice_lines_invalid", f"Line {index} must have a positive unit price.")


async def _post_invoice(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    invoice: SalesInvoice,
    lines: tuple[SalesInvoiceLine, ...],
) -> tuple[EntryResult | None, EntryResult | None]:
    """Post the invoice, issue stock and post cost of sales, all in one transaction."""
    if invoice.status != "draft":
        raise conflict("invoice_already_posted", "This invoice is already posted.")
    period = await resolve_open_period(
        session,
        tenant_id=tenant_id,
        company_id=invoice.company_id,
        on_date=invoice.invoice_date,
    )
    accounts = await resolve_posting_accounts(
        session,
        tenant_id=tenant_id,
        company_id=invoice.company_id,
        on_date=invoice.invoice_date,
        keys=(
            "accounts_receivable",
            "sales_revenue",
            "inventory",
            "vat_payable",
            "cost_of_sales",
        ),
    )
    journal_lines = [
        JournalLineInput(
            account_id=accounts["accounts_receivable"].id,
            debit_minor=invoice.base_total_minor,
            description=f"Invoice {invoice.number}",
        ),
        JournalLineInput(
            account_id=accounts["sales_revenue"].id,
            credit_minor=invoice.base_subtotal_minor,
            description=f"Invoice {invoice.number} revenue",
        ),
    ]
    if invoice.base_tax_minor > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts["vat_payable"].id,
                credit_minor=invoice.base_tax_minor,
                description=f"Invoice {invoice.number} VAT",
            )
        )
    journal_entry = await post_entry_in_session(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        company_id=invoice.company_id,
        period_id=period.id,
        entry_date=invoice.invoice_date,
        memo=invoice.memo or f"Sales invoice {invoice.number}",
        lines=tuple(journal_lines),
        source_type="sales_invoice",
        source_id=invoice.id,
    )

    cogs_total = 0
    for line in sorted(lines, key=lambda row: row.line_number):
        if line.item_id is None:
            continue
        issue = await issue_stock(
            session,
            tenant_id=tenant_id,
            company_id=invoice.company_id,
            item_id=line.item_id,
            quantity=line.quantity,
            document_type="sales_invoice",
            document_id=invoice.id,
            occurred_on=invoice.invoice_date,
            actor_user_id=actor_user_id,
        )
        cogs_total += issue.value_minor
    cogs_entry: EntryResult | None = None
    if cogs_total > 0:
        cogs_entry = await post_entry_in_session(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            company_id=invoice.company_id,
            period_id=period.id,
            entry_date=invoice.invoice_date,
            memo=f"Cost of sales for invoice {invoice.number}",
            lines=(
                JournalLineInput(
                    account_id=accounts["cost_of_sales"].id,
                    debit_minor=cogs_total,
                    description=f"Invoice {invoice.number} cost of sales",
                ),
                JournalLineInput(
                    account_id=accounts["inventory"].id,
                    credit_minor=cogs_total,
                    description=f"Invoice {invoice.number} inventory issue",
                ),
            ),
            source_type="sales_invoice_cogs",
            source_id=invoice.id,
        )

    invoice.status = "posted"
    invoice.journal_entry_id = journal_entry.entry.id
    invoice.cogs_entry_id = cogs_entry.entry.id if cogs_entry is not None else None
    invoice.posted_at = datetime.now(UTC)
    invoice.posted_by = actor_user_id
    await session.flush()
    return journal_entry, cogs_entry


async def _load_invoice(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, invoice_id: UUID
) -> tuple[SalesInvoice, tuple[SalesInvoiceLine, ...]]:
    invoice = (
        await session.execute(
            select(SalesInvoice).where(
                SalesInvoice.tenant_id == tenant_id,
                SalesInvoice.company_id == company_id,
                SalesInvoice.id == invoice_id,
            )
        )
    ).scalar_one_or_none()
    if invoice is None:
        raise AppError(
            code="invoice_not_found",
            title="Not found",
            status_code=404,
            detail="The sales invoice does not exist in the selected company.",
        )
    lines = (
        (
            await session.execute(
                select(SalesInvoiceLine)
                .where(
                    SalesInvoiceLine.tenant_id == tenant_id,
                    SalesInvoiceLine.company_id == company_id,
                    SalesInvoiceLine.invoice_id == invoice.id,
                )
                .order_by(SalesInvoiceLine.line_number)
            )
        )
        .scalars()
        .all()
    )
    return invoice, tuple(lines)


async def create_sales_invoice(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: SalesInvoiceInput,
) -> SalesInvoiceResult:
    """Create a draft invoice, optionally posting it immediately."""
    _validate_input(request)

    async def operation(session: AsyncSession) -> SalesInvoiceResult:
        customer = await load_customer(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            customer_id=request.customer_id,
        )
        tax_ids = {line.tax_code_id for line in request.lines if line.tax_code_id is not None}
        item_ids = {line.item_id for line in request.lines if line.item_id is not None}
        await load_tax_rates(
            session, tenant_id=tenant_id, company_id=request.company_id, ids=tax_ids
        )
        tax_rates = await tax_rates_on(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            tax_code_ids=tax_ids,
            on_date=request.invoice_date,
        )
        await load_items(session, tenant_id=tenant_id, company_id=request.company_id, ids=item_ids)

        prepared: list[tuple[SalesLineInput, int, int, int, int]] = []
        subtotal = 0
        tax_total = 0
        base_subtotal = 0
        base_tax_total = 0
        for line in request.lines:
            line_subtotal = minor(line.quantity * Decimal(line.unit_price_minor))
            rate = tax_rates[line.tax_code_id] if line.tax_code_id else 0
            line_tax = minor(Decimal(line_subtotal) * rate / Decimal(10000))
            base_line_subtotal = minor(Decimal(line_subtotal) * request.exchange_rate)
            base_line_tax = minor(Decimal(line_tax) * request.exchange_rate)
            prepared.append((line, line_subtotal, line_tax, base_line_subtotal, base_line_tax))
            subtotal += line_subtotal
            tax_total += line_tax
            base_subtotal += base_line_subtotal
            base_tax_total += base_line_tax

        if request.number is not None:
            number = request.number
        else:
            number = await allocate_document_number(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                scope=NUMBER_SCOPE,
                period_key=f"{request.invoice_date.year:04d}",
                prefix=NUMBER_PREFIX,
            )
        invoice = SalesInvoice(
            tenant_id=tenant_id,
            company_id=request.company_id,
            number=number,
            customer_id=customer.id,
            invoice_date=request.invoice_date,
            due_date=request.due_date,
            currency=request.currency,
            exchange_rate=request.exchange_rate,
            memo=request.memo,
            subtotal_minor=subtotal,
            tax_minor=tax_total,
            total_minor=subtotal + tax_total,
            base_subtotal_minor=base_subtotal,
            base_tax_minor=base_tax_total,
            base_total_minor=base_subtotal + base_tax_total,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(invoice)
        await session.flush()
        lines = tuple(
            SalesInvoiceLine(
                tenant_id=tenant_id,
                company_id=request.company_id,
                invoice_id=invoice.id,
                line_number=index,
                item_id=line.item_id,
                tax_code_id=line.tax_code_id,
                description=line.description,
                quantity=line.quantity,
                unit_price_minor=line.unit_price_minor,
                line_subtotal_minor=line_subtotal,
                line_tax_minor=line_tax,
                line_total_minor=line_subtotal + line_tax,
                base_line_subtotal_minor=base_line_subtotal,
                base_line_tax_minor=base_line_tax,
            )
            for index, (
                line,
                line_subtotal,
                line_tax,
                base_line_subtotal,
                base_line_tax,
            ) in enumerate(prepared, start=1)
        )
        session.add_all(lines)
        await session.flush()

        journal_entry: EntryResult | None = None
        cogs_entry: EntryResult | None = None
        if request.post:
            journal_entry, cogs_entry = await _post_invoice(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                invoice=invoice,
                lines=lines,
            )
        await record_sales_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="sales.invoice.posted" if request.post else "sales.invoice.created",
            invoice=invoice,
        )
        return SalesInvoiceResult(
            invoice=invoice, lines=lines, journal_entry=journal_entry, cogs_entry=cogs_entry
        )

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate(error) from None
    except DBAPIError as error:
        raise _translate(error) from None


async def post_sales_invoice(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    invoice_id: UUID,
) -> SalesInvoiceResult:
    """Post an existing draft invoice."""

    async def operation(session: AsyncSession) -> SalesInvoiceResult:
        invoice = (
            await session.execute(
                select(SalesInvoice)
                .where(
                    SalesInvoice.tenant_id == tenant_id,
                    SalesInvoice.company_id == company_id,
                    SalesInvoice.id == invoice_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if invoice is None:
            raise AppError(
                code="invoice_not_found",
                title="Not found",
                status_code=404,
                detail="The sales invoice does not exist in the selected company.",
            )
        lines = (
            (
                await session.execute(
                    select(SalesInvoiceLine)
                    .where(
                        SalesInvoiceLine.tenant_id == tenant_id,
                        SalesInvoiceLine.invoice_id == invoice.id,
                    )
                    .order_by(SalesInvoiceLine.line_number)
                )
            )
            .scalars()
            .all()
        )
        journal_entry, cogs_entry = await _post_invoice(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            invoice=invoice,
            lines=tuple(lines),
        )
        await record_sales_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="sales.invoice.posted",
            invoice=invoice,
        )
        return SalesInvoiceResult(
            invoice=invoice,
            lines=tuple(lines),
            journal_entry=journal_entry,
            cogs_entry=cogs_entry,
        )

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate(error) from None
    except DBAPIError as error:
        raise _translate(error) from None


async def get_sales_invoice(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, invoice_id: UUID
) -> tuple[SalesInvoice, tuple[SalesInvoiceLine, ...]]:
    """Load one invoice and its lines."""
    return await _load_invoice(
        session, tenant_id=tenant_id, company_id=company_id, invoice_id=invoice_id
    )


async def list_sales_invoices(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[SalesInvoice]:
    """List invoices newest first."""
    return list(
        (
            await session.execute(
                select(SalesInvoice)
                .where(SalesInvoice.tenant_id == tenant_id, SalesInvoice.company_id == company_id)
                .order_by(SalesInvoice.invoice_date.desc(), SalesInvoice.number.desc())
            )
        )
        .scalars()
        .all()
    )


def _translate(error: DBAPIError) -> AppError:
    message = str(error.orig).lower()
    if "uq_sales_invoice_tenant_company_number" in message:
        return conflict("duplicate_document_number", "This invoice number already exists.")
    if "uq_journal_entry_source" in message:
        return conflict("source_duplicate", "This invoice is already posted.")
    if "uq_stock_move_document_item_type" in message:
        return conflict("stock_move_duplicate", "This invoice already moved stock.")
    return AppError(
        code="invoice_rejected",
        title="Conflict",
        status_code=409,
        detail="The invoice could not be saved because it conflicts with existing data.",
    )


async def record_sales_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    action: str,
    invoice: SalesInvoice,
) -> None:
    """Record the invoice lifecycle event."""
    from app.modules.identity.audit import record_audit

    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type="sales_invoice",
        entity_id=invoice.id,
        detail={
            "number": invoice.number,
            "total_minor": invoice.total_minor,
            "currency": invoice.currency,
        },
    )
