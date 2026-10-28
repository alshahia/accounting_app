"""Customer credit notes: goods returns against a posted invoice (phase 1).

A credit note stores its own document-currency amounts and converts at its own
rate. Posting reverses the invoice in one transaction: revenue and output VAT
are debited, receivable is credited, and every stocked line is put back at the
cost the invoice issued it, debiting inventory and crediting cost of sales. The
returned quantity is capped by what the invoice line still has outstanding
across posted credit notes, so the same goods cannot be returned twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.tax import rates_on as tax_rates_on
from app.modules.inventory.models import StockMove
from app.modules.inventory.service import receive_stock
from app.modules.ledger.mapping import resolve_posting_accounts
from app.modules.ledger.service import (
    EntryResult,
    JournalLineInput,
    post_entry_in_session,
)
from app.modules.sales.models import (
    SalesCreditNote,
    SalesCreditNoteLine,
    SalesInvoice,
    SalesInvoiceLine,
)
from app.modules.sales.service import (
    bad,
    conflict,
    load_customer,
    load_items,
    load_tax_rates,
    minor,
    resolve_open_period,
)
from app.shared.errors import AppError
from app.shared.numbering import allocate_document_number
from app.shared.unit_of_work import UnitOfWork

NUMBER_SCOPE = "sales_credit_note"
NUMBER_PREFIX = "CN"
POSTED_INVOICE_STATUSES = ("posted", "part_paid", "paid")


@dataclass(frozen=True)
class CreditNoteLineInput:
    """One credit-note line in the document currency."""

    description: str
    quantity: Decimal
    unit_price_minor: int
    item_id: UUID | None = None
    tax_code_id: UUID | None = None
    invoice_line_id: UUID | None = None


@dataclass(frozen=True)
class CreditNoteInput:
    """Everything needed to create a draft or posted customer credit note."""

    company_id: UUID
    customer_id: UUID
    credit_note_date: date
    lines: tuple[CreditNoteLineInput, ...]
    invoice_id: UUID | None = None
    currency: str = "IQD"
    exchange_rate: Decimal = Decimal("1")
    memo: str | None = None
    number: str | None = None
    post: bool = False


@dataclass(frozen=True)
class CreditNoteResult:
    """The credit note, its lines and the posting entry (if any)."""

    credit_note: SalesCreditNote
    lines: tuple[SalesCreditNoteLine, ...]
    journal_entry: EntryResult | None


def _validate_input(request: CreditNoteInput) -> None:
    if not request.lines:
        raise bad("credit_note_lines_invalid", "A credit note needs at least one line.")
    if len(request.lines) > 200:
        raise bad("credit_note_lines_invalid", "A credit note can have at most 200 lines.")
    if len(request.currency) != 3 or not request.currency.isalpha():
        raise bad("currency_invalid", "The currency must be a three-letter ISO code.")
    if request.exchange_rate <= 0:
        raise bad("exchange_rate_invalid", "The exchange rate must be positive.")
    if request.number is not None and not 1 <= len(request.number) <= 40:
        raise bad("document_number_invalid", "A document number is 1-40 characters.")
    for index, line in enumerate(request.lines, start=1):
        if line.quantity <= 0:
            raise bad("credit_note_lines_invalid", f"Line {index} must have a positive quantity.")
        if line.unit_price_minor < 0:
            raise bad(
                "credit_note_lines_invalid", f"Line {index} must have a non-negative unit price."
            )
        if line.item_id is not None and line.invoice_line_id is None:
            raise bad(
                "credit_note_invoice_line_required",
                f"Line {index} returns a stocked item and must name its invoice line.",
            )


async def _load_invoice(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, invoice_id: UUID
) -> SalesInvoice:
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
    if invoice.status not in POSTED_INVOICE_STATUSES:
        raise conflict("invoice_not_posted", "Credit notes apply to posted invoices only.")
    return invoice


async def _load_invoice_lines(
    session: AsyncSession, *, tenant_id: UUID, invoice_id: UUID
) -> dict[UUID, SalesInvoiceLine]:
    rows = (
        (
            await session.execute(
                select(SalesInvoiceLine).where(
                    SalesInvoiceLine.tenant_id == tenant_id,
                    SalesInvoiceLine.invoice_id == invoice_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return {line.id: line for line in rows}


async def _returned_quantity(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, invoice_line_id: UUID
) -> Decimal:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(SalesCreditNoteLine.quantity), 0))
            .join(
                SalesCreditNote,
                (SalesCreditNote.tenant_id == SalesCreditNoteLine.tenant_id)
                & (SalesCreditNote.id == SalesCreditNoteLine.credit_note_id),
            )
            .where(
                SalesCreditNoteLine.tenant_id == tenant_id,
                SalesCreditNoteLine.company_id == company_id,
                SalesCreditNoteLine.invoice_line_id == invoice_line_id,
                SalesCreditNote.status == "posted",
            )
        )
    ).scalar_one()
    return Decimal(str(total)).quantize(Decimal("0.0001"))


async def _prepare_lines(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    request: CreditNoteInput,
    invoice: SalesInvoice | None,
    invoice_lines: dict[UUID, SalesInvoiceLine],
    tax_rates: dict[UUID, int],
) -> list[tuple[CreditNoteLineInput, int, int, int, int]]:
    prepared: list[tuple[CreditNoteLineInput, int, int, int, int]] = []
    touched: set[UUID] = set()
    for index, line in enumerate(request.lines, start=1):
        invoice_line = None
        if line.invoice_line_id is not None:
            if invoice is None:
                raise bad(
                    "credit_note_invoice_required",
                    "A credit note line cannot name an invoice line without an invoice.",
                )
            invoice_line = invoice_lines.get(line.invoice_line_id)
            if invoice_line is None:
                raise bad(
                    "credit_note_invoice_line_invalid",
                    f"Line {index} does not belong to invoice {invoice.number}.",
                )
            if invoice_line.id in touched:
                raise bad(
                    "credit_note_invoice_line_duplicate",
                    f"Invoice line {invoice_line.line_number} appears twice.",
                )
            touched.add(invoice_line.id)
            if invoice_line.item_id != line.item_id:
                raise bad(
                    "credit_note_item_mismatch",
                    f"Line {index} does not match the item of invoice line "
                    f"{invoice_line.line_number}.",
                )
            returned = await _returned_quantity(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                invoice_line_id=invoice_line.id,
            )
            remaining = invoice_line.quantity - returned
            if line.quantity > remaining:
                raise bad(
                    "credit_note_quantity_exceeds_invoice",
                    f"Line {index} returns {line.quantity}; only {remaining} remains on "
                    f"invoice line {invoice_line.line_number}.",
                )
        elif line.item_id is not None:
            raise bad(
                "credit_note_invoice_required",
                "A stocked return needs an invoice and its invoice line.",
            )
        line_subtotal = minor(line.quantity * Decimal(line.unit_price_minor))
        rate = tax_rates[line.tax_code_id] if line.tax_code_id else 0
        line_tax = minor(Decimal(line_subtotal) * rate / Decimal(10000))
        base_line_subtotal = minor(Decimal(line_subtotal) * request.exchange_rate)
        base_line_tax = minor(Decimal(line_tax) * request.exchange_rate)
        prepared.append((line, line_subtotal, line_tax, base_line_subtotal, base_line_tax))
    return prepared


async def _post_credit_note(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    credit_note: SalesCreditNote,
    lines: tuple[SalesCreditNoteLine, ...],
) -> EntryResult:
    if credit_note.status != "draft":
        raise conflict("credit_note_already_posted", "This credit note is already posted.")
    period = await resolve_open_period(
        session,
        tenant_id=tenant_id,
        company_id=credit_note.company_id,
        on_date=credit_note.credit_note_date,
    )
    accounts = await resolve_posting_accounts(
        session,
        tenant_id=tenant_id,
        company_id=credit_note.company_id,
        on_date=credit_note.credit_note_date,
        keys=(
            "accounts_receivable",
            "sales_revenue",
            "vat_payable",
            "inventory",
            "cost_of_sales",
        ),
    )

    return_qty_by_item: dict[UUID, Decimal] = {}
    for line in lines:
        if line.item_id is None:
            continue
        return_qty_by_item[line.item_id] = (
            return_qty_by_item.get(line.item_id, Decimal("0")) + line.quantity
        )

    inventory_value = 0
    if return_qty_by_item:
        if credit_note.invoice_id is None:
            raise bad(
                "credit_note_invoice_required", "A stocked return needs the original invoice."
            )
        for item_id, return_qty in return_qty_by_item.items():
            issue = (
                await session.execute(
                    select(StockMove).where(
                        StockMove.tenant_id == tenant_id,
                        StockMove.company_id == credit_note.company_id,
                        StockMove.document_type == "sales_invoice",
                        StockMove.document_id == credit_note.invoice_id,
                        StockMove.move_type == "issue",
                        StockMove.item_id == item_id,
                    )
                )
            ).scalar_one_or_none()
            if issue is None:
                raise conflict(
                    "credit_note_stock_missing",
                    "The original invoice has no stock issue for one of the returned items.",
                )
            if return_qty > issue.quantity:
                raise bad(
                    "credit_note_quantity_exceeds_invoice",
                    "The returned quantity exceeds the quantity issued by the invoice.",
                )
            returned_value = minor(
                Decimal(issue.value_minor) * return_qty / Decimal(issue.quantity)
            )
            returned_value = min(returned_value, issue.value_minor)
            await receive_stock(
                session,
                tenant_id=tenant_id,
                company_id=credit_note.company_id,
                item_id=item_id,
                quantity=return_qty,
                value_minor=returned_value,
                document_type="sales_credit_note",
                document_id=credit_note.id,
                occurred_on=credit_note.credit_note_date,
                actor_user_id=actor_user_id,
            )
            inventory_value += returned_value

    journal_lines = [
        JournalLineInput(
            account_id=accounts["sales_revenue"].id,
            debit_minor=credit_note.base_subtotal_minor,
            description=f"Credit note {credit_note.number} revenue reversal",
        )
    ]
    if credit_note.base_tax_minor > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts["vat_payable"].id,
                debit_minor=credit_note.base_tax_minor,
                description=f"Credit note {credit_note.number} output VAT reversal",
            )
        )
    journal_lines.append(
        JournalLineInput(
            account_id=accounts["accounts_receivable"].id,
            credit_minor=credit_note.base_total_minor,
            description=f"Credit note {credit_note.number} receivable relief",
        )
    )
    if inventory_value > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts["inventory"].id,
                debit_minor=inventory_value,
                description=f"Credit note {credit_note.number} stock return",
            )
        )
        journal_lines.append(
            JournalLineInput(
                account_id=accounts["cost_of_sales"].id,
                credit_minor=inventory_value,
                description=f"Credit note {credit_note.number} cost of sales reversal",
            )
        )
    journal_entry = await post_entry_in_session(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        company_id=credit_note.company_id,
        period_id=period.id,
        entry_date=credit_note.credit_note_date,
        memo=credit_note.memo or f"Sales credit note {credit_note.number}",
        lines=tuple(journal_lines),
        source_type="sales_credit_note",
        source_id=credit_note.id,
    )
    credit_note.status = "posted"
    credit_note.journal_entry_id = journal_entry.entry.id
    credit_note.posted_at = datetime.now(UTC)
    credit_note.posted_by = actor_user_id
    await session.flush()
    return journal_entry


async def create_credit_note(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: CreditNoteInput,
) -> CreditNoteResult:
    """Create a draft credit note, optionally posting it immediately."""
    _validate_input(request)

    async def operation(session: AsyncSession) -> CreditNoteResult:
        customer = await load_customer(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            customer_id=request.customer_id,
        )
        invoice = None
        invoice_lines: dict[UUID, SalesInvoiceLine] = {}
        if request.invoice_id is not None:
            invoice = await _load_invoice(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                invoice_id=request.invoice_id,
            )
            if invoice.customer_id != customer.id:
                raise bad(
                    "credit_note_customer_mismatch",
                    "The credit note customer is not the customer of the invoice.",
                )
            invoice_lines = await _load_invoice_lines(
                session, tenant_id=tenant_id, invoice_id=invoice.id
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
            on_date=request.credit_note_date,
        )
        await load_items(session, tenant_id=tenant_id, company_id=request.company_id, ids=item_ids)
        prepared = await _prepare_lines(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            request=request,
            invoice=invoice,
            invoice_lines=invoice_lines,
            tax_rates=tax_rates,
        )
        subtotal = sum(row[1] for row in prepared)
        tax_total = sum(row[2] for row in prepared)
        base_subtotal = sum(row[3] for row in prepared)
        base_tax_total = sum(row[4] for row in prepared)

        if request.number is not None:
            number = request.number
        else:
            number = await allocate_document_number(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                scope=NUMBER_SCOPE,
                period_key=f"{request.credit_note_date.year:04d}",
                prefix=NUMBER_PREFIX,
            )
        credit_note = SalesCreditNote(
            tenant_id=tenant_id,
            company_id=request.company_id,
            number=number,
            customer_id=customer.id,
            invoice_id=invoice.id if invoice is not None else None,
            credit_note_date=request.credit_note_date,
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
        session.add(credit_note)
        await session.flush()
        lines = tuple(
            SalesCreditNoteLine(
                tenant_id=tenant_id,
                company_id=request.company_id,
                credit_note_id=credit_note.id,
                invoice_line_id=line.invoice_line_id,
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
        if request.post:
            journal_entry = await _post_credit_note(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                credit_note=credit_note,
                lines=lines,
            )
        await record_credit_note_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="sales.credit_note.posted" if request.post else "sales.credit_note.created",
            credit_note=credit_note,
        )
        return CreditNoteResult(credit_note=credit_note, lines=lines, journal_entry=journal_entry)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_credit_note(error) from None
    except DBAPIError as error:
        raise _translate_credit_note(error) from None


async def post_credit_note(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    credit_note_id: UUID,
) -> CreditNoteResult:
    """Post an existing draft credit note."""

    async def operation(session: AsyncSession) -> CreditNoteResult:
        credit_note = (
            await session.execute(
                select(SalesCreditNote)
                .where(
                    SalesCreditNote.tenant_id == tenant_id,
                    SalesCreditNote.company_id == company_id,
                    SalesCreditNote.id == credit_note_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if credit_note is None:
            raise AppError(
                code="credit_note_not_found",
                title="Not found",
                status_code=404,
                detail="The sales credit note does not exist in the selected company.",
            )
        lines = (
            (
                await session.execute(
                    select(SalesCreditNoteLine)
                    .where(
                        SalesCreditNoteLine.tenant_id == tenant_id,
                        SalesCreditNoteLine.credit_note_id == credit_note.id,
                    )
                    .order_by(SalesCreditNoteLine.line_number)
                )
            )
            .scalars()
            .all()
        )
        journal_entry = await _post_credit_note(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            credit_note=credit_note,
            lines=tuple(lines),
        )
        await record_credit_note_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="sales.credit_note.posted",
            credit_note=credit_note,
        )
        return CreditNoteResult(
            credit_note=credit_note, lines=tuple(lines), journal_entry=journal_entry
        )

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_credit_note(error) from None
    except DBAPIError as error:
        raise _translate_credit_note(error) from None


async def get_credit_note(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, credit_note_id: UUID
) -> tuple[SalesCreditNote, tuple[SalesCreditNoteLine, ...]]:
    """Load one credit note and its lines."""
    credit_note = (
        await session.execute(
            select(SalesCreditNote).where(
                SalesCreditNote.tenant_id == tenant_id,
                SalesCreditNote.company_id == company_id,
                SalesCreditNote.id == credit_note_id,
            )
        )
    ).scalar_one_or_none()
    if credit_note is None:
        raise AppError(
            code="credit_note_not_found",
            title="Not found",
            status_code=404,
            detail="The sales credit note does not exist in the selected company.",
        )
    lines = (
        (
            await session.execute(
                select(SalesCreditNoteLine)
                .where(
                    SalesCreditNoteLine.tenant_id == tenant_id,
                    SalesCreditNoteLine.credit_note_id == credit_note.id,
                )
                .order_by(SalesCreditNoteLine.line_number)
            )
        )
        .scalars()
        .all()
    )
    return credit_note, tuple(lines)


async def list_credit_notes(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[SalesCreditNote]:
    """List credit notes newest first."""
    return list(
        (
            await session.execute(
                select(SalesCreditNote)
                .where(
                    SalesCreditNote.tenant_id == tenant_id,
                    SalesCreditNote.company_id == company_id,
                )
                .order_by(SalesCreditNote.credit_note_date.desc(), SalesCreditNote.number.desc())
            )
        )
        .scalars()
        .all()
    )


def _translate_credit_note(error: DBAPIError) -> AppError:
    message = str(error.orig).lower()
    if "uq_sales_credit_note_tenant_company_number" in message:
        return conflict("duplicate_document_number", "This credit note number already exists.")
    if "uq_journal_entry_source" in message:
        return conflict("source_duplicate", "This credit note is already posted.")
    if "uq_stock_move_document_item_type" in message:
        return conflict("stock_move_duplicate", "This credit note already moved stock.")
    return AppError(
        code="credit_note_rejected",
        title="Conflict",
        status_code=409,
        detail="The credit note could not be saved because it conflicts with existing data.",
    )


async def record_credit_note_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    action: str,
    credit_note: SalesCreditNote,
) -> None:
    """Record the credit-note lifecycle event."""
    from app.modules.identity.audit import record_audit

    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type="sales_credit_note",
        entity_id=credit_note.id,
        detail={
            "number": credit_note.number,
            "total_minor": credit_note.total_minor,
            "currency": credit_note.currency,
        },
    )
