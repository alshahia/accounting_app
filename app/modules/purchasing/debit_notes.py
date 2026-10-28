"""Supplier debit notes: purchase returns against a posted bill (phase 1).

Posting reduces payable, takes the returned quantity out of stock at its current
weighted-average cost, reverses input VAT and books the difference between the
bill price and the stock cost to cost of sales, so the entry stays balanced and
the inventory GL keeps agreeing with the sum of item values. The returned
quantity is capped by what the bill line still has outstanding across posted
debit notes.
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
from app.modules.inventory.service import issue_stock
from app.modules.ledger.mapping import resolve_posting_accounts
from app.modules.ledger.service import (
    EntryResult,
    JournalLineInput,
    post_entry_in_session,
)
from app.modules.purchasing.models import (
    PurchaseBill,
    PurchaseBillLine,
    PurchaseDebitNote,
    PurchaseDebitNoteLine,
)
from app.modules.purchasing.service import (
    bad,
    conflict,
    load_items,
    load_supplier,
    load_tax_rates,
    minor,
    resolve_open_period,
)
from app.shared.errors import AppError
from app.shared.numbering import allocate_document_number
from app.shared.unit_of_work import UnitOfWork

NUMBER_SCOPE = "purchase_debit_note"
NUMBER_PREFIX = "DN"
POSTED_BILL_STATUSES = ("posted", "part_paid", "paid")


@dataclass(frozen=True)
class DebitNoteLineInput:
    """One debit-note line in the document currency."""

    description: str
    quantity: Decimal
    unit_price_minor: int
    item_id: UUID | None = None
    tax_code_id: UUID | None = None
    bill_line_id: UUID | None = None


@dataclass(frozen=True)
class DebitNoteInput:
    """Everything needed to create a draft or posted supplier debit note."""

    company_id: UUID
    supplier_id: UUID
    debit_note_date: date
    lines: tuple[DebitNoteLineInput, ...]
    bill_id: UUID | None = None
    currency: str = "IQD"
    exchange_rate: Decimal = Decimal("1")
    memo: str | None = None
    number: str | None = None
    post: bool = False


@dataclass(frozen=True)
class DebitNoteResult:
    """The debit note, its lines and the posting entry (if any)."""

    debit_note: PurchaseDebitNote
    lines: tuple[PurchaseDebitNoteLine, ...]
    journal_entry: EntryResult | None


def _validate_input(request: DebitNoteInput) -> None:
    if not request.lines:
        raise bad("debit_note_lines_invalid", "A debit note needs at least one line.")
    if len(request.lines) > 200:
        raise bad("debit_note_lines_invalid", "A debit note can have at most 200 lines.")
    if len(request.currency) != 3 or not request.currency.isalpha():
        raise bad("currency_invalid", "The currency must be a three-letter ISO code.")
    if request.exchange_rate <= 0:
        raise bad("exchange_rate_invalid", "The exchange rate must be positive.")
    if request.number is not None and not 1 <= len(request.number) <= 40:
        raise bad("document_number_invalid", "A document number is 1-40 characters.")
    for index, line in enumerate(request.lines, start=1):
        if line.quantity <= 0:
            raise bad("debit_note_lines_invalid", f"Line {index} must have a positive quantity.")
        if line.unit_price_minor < 0:
            raise bad(
                "debit_note_lines_invalid", f"Line {index} must have a non-negative unit price."
            )
        if line.item_id is not None and line.bill_line_id is None:
            raise bad(
                "debit_note_bill_line_required",
                f"Line {index} returns a stocked item and must name its bill line.",
            )


async def _load_bill(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, bill_id: UUID
) -> PurchaseBill:
    bill = (
        await session.execute(
            select(PurchaseBill).where(
                PurchaseBill.tenant_id == tenant_id,
                PurchaseBill.company_id == company_id,
                PurchaseBill.id == bill_id,
            )
        )
    ).scalar_one_or_none()
    if bill is None:
        raise AppError(
            code="bill_not_found",
            title="Not found",
            status_code=404,
            detail="The purchase bill does not exist in the selected company.",
        )
    if bill.status not in POSTED_BILL_STATUSES:
        raise conflict("bill_not_posted", "Debit notes apply to posted bills only.")
    return bill


async def _load_bill_lines(
    session: AsyncSession, *, tenant_id: UUID, bill_id: UUID
) -> dict[UUID, PurchaseBillLine]:
    rows = (
        (
            await session.execute(
                select(PurchaseBillLine).where(
                    PurchaseBillLine.tenant_id == tenant_id,
                    PurchaseBillLine.bill_id == bill_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return {line.id: line for line in rows}


async def _returned_quantity(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, bill_line_id: UUID
) -> Decimal:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(PurchaseDebitNoteLine.quantity), 0))
            .join(
                PurchaseDebitNote,
                (PurchaseDebitNote.tenant_id == PurchaseDebitNoteLine.tenant_id)
                & (PurchaseDebitNote.id == PurchaseDebitNoteLine.debit_note_id),
            )
            .where(
                PurchaseDebitNoteLine.tenant_id == tenant_id,
                PurchaseDebitNoteLine.company_id == company_id,
                PurchaseDebitNoteLine.bill_line_id == bill_line_id,
                PurchaseDebitNote.status == "posted",
            )
        )
    ).scalar_one()
    return Decimal(str(total)).quantize(Decimal("0.0001"))


async def _prepare_lines(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    request: DebitNoteInput,
    bill: PurchaseBill | None,
    bill_lines: dict[UUID, PurchaseBillLine],
    tax_rates: dict[UUID, int],
) -> list[tuple[DebitNoteLineInput, int, int, int, int]]:
    prepared: list[tuple[DebitNoteLineInput, int, int, int, int]] = []
    touched: set[UUID] = set()
    for index, line in enumerate(request.lines, start=1):
        bill_line = None
        if line.bill_line_id is not None:
            if bill is None:
                raise bad(
                    "debit_note_bill_required",
                    "A debit note line cannot name a bill line without a bill.",
                )
            bill_line = bill_lines.get(line.bill_line_id)
            if bill_line is None:
                raise bad(
                    "debit_note_bill_line_invalid",
                    f"Line {index} does not belong to bill {bill.number}.",
                )
            if bill_line.id in touched:
                raise bad(
                    "debit_note_bill_line_duplicate",
                    f"Bill line {bill_line.line_number} appears twice.",
                )
            touched.add(bill_line.id)
            if bill_line.item_id != line.item_id:
                raise bad(
                    "debit_note_item_mismatch",
                    f"Line {index} does not match the item of bill line {bill_line.line_number}.",
                )
            returned = await _returned_quantity(
                session,
                tenant_id=tenant_id,
                company_id=company_id,
                bill_line_id=bill_line.id,
            )
            remaining = bill_line.quantity - returned
            if line.quantity > remaining:
                raise bad(
                    "debit_note_quantity_exceeds_bill",
                    f"Line {index} returns {line.quantity}; only {remaining} remains on "
                    f"bill line {bill_line.line_number}.",
                )
        elif line.item_id is not None:
            raise bad(
                "debit_note_bill_required",
                "A stocked return needs a bill and its bill line.",
            )
        line_subtotal = minor(line.quantity * Decimal(line.unit_price_minor))
        rate = tax_rates[line.tax_code_id] if line.tax_code_id else 0
        line_tax = minor(Decimal(line_subtotal) * rate / Decimal(10000))
        base_line_subtotal = minor(Decimal(line_subtotal) * request.exchange_rate)
        base_line_tax = minor(Decimal(line_tax) * request.exchange_rate)
        prepared.append((line, line_subtotal, line_tax, base_line_subtotal, base_line_tax))
    return prepared


async def _post_debit_note(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    debit_note: PurchaseDebitNote,
    lines: tuple[PurchaseDebitNoteLine, ...],
) -> EntryResult:
    if debit_note.status != "draft":
        raise conflict("debit_note_already_posted", "This debit note is already posted.")
    period = await resolve_open_period(
        session,
        tenant_id=tenant_id,
        company_id=debit_note.company_id,
        on_date=debit_note.debit_note_date,
    )
    accounts = await resolve_posting_accounts(
        session,
        tenant_id=tenant_id,
        company_id=debit_note.company_id,
        on_date=debit_note.debit_note_date,
        keys=("accounts_payable", "inventory", "vat_payable", "cost_of_sales"),
    )
    return_qty_by_item: dict[UUID, Decimal] = {}
    inventory_credit = 0
    for line in lines:
        if line.item_id is None:
            inventory_credit += line.base_line_subtotal_minor
            continue
        return_qty_by_item[line.item_id] = (
            return_qty_by_item.get(line.item_id, Decimal("0")) + line.quantity
        )
    for item_id, return_qty in return_qty_by_item.items():
        issue = await issue_stock(
            session,
            tenant_id=tenant_id,
            company_id=debit_note.company_id,
            item_id=item_id,
            quantity=return_qty,
            document_type="purchase_debit_note",
            document_id=debit_note.id,
            occurred_on=debit_note.debit_note_date,
            actor_user_id=actor_user_id,
        )
        inventory_credit += issue.value_minor

    journal_lines = [
        JournalLineInput(
            account_id=accounts["accounts_payable"].id,
            debit_minor=debit_note.base_total_minor,
            description=f"Debit note {debit_note.number} payable relief",
        )
    ]
    if inventory_credit > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts["inventory"].id,
                credit_minor=inventory_credit,
                description=f"Debit note {debit_note.number} stock return",
            )
        )
    if debit_note.base_tax_minor > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts["vat_payable"].id,
                credit_minor=debit_note.base_tax_minor,
                description=f"Debit note {debit_note.number} input VAT reversal",
            )
        )
    adjustment = debit_note.base_subtotal_minor - inventory_credit
    if adjustment > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts["cost_of_sales"].id,
                credit_minor=adjustment,
                description=f"Debit note {debit_note.number} purchase price adjustment",
            )
        )
    elif adjustment < 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts["cost_of_sales"].id,
                debit_minor=-adjustment,
                description=f"Debit note {debit_note.number} cost adjustment",
            )
        )
    journal_entry = await post_entry_in_session(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        company_id=debit_note.company_id,
        period_id=period.id,
        entry_date=debit_note.debit_note_date,
        memo=debit_note.memo or f"Purchase debit note {debit_note.number}",
        lines=tuple(journal_lines),
        source_type="purchase_debit_note",
        source_id=debit_note.id,
    )
    debit_note.status = "posted"
    debit_note.journal_entry_id = journal_entry.entry.id
    debit_note.posted_at = datetime.now(UTC)
    debit_note.posted_by = actor_user_id
    await session.flush()
    return journal_entry


async def create_debit_note(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: DebitNoteInput,
) -> DebitNoteResult:
    """Create a draft debit note, optionally posting it immediately."""
    _validate_input(request)

    async def operation(session: AsyncSession) -> DebitNoteResult:
        supplier = await load_supplier(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            supplier_id=request.supplier_id,
        )
        bill = None
        bill_lines: dict[UUID, PurchaseBillLine] = {}
        if request.bill_id is not None:
            bill = await _load_bill(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                bill_id=request.bill_id,
            )
            if bill.supplier_id != supplier.id:
                raise bad(
                    "debit_note_supplier_mismatch",
                    "The debit note supplier is not the supplier of the bill.",
                )
            bill_lines = await _load_bill_lines(session, tenant_id=tenant_id, bill_id=bill.id)
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
            on_date=request.debit_note_date,
        )
        await load_items(session, tenant_id=tenant_id, company_id=request.company_id, ids=item_ids)
        prepared = await _prepare_lines(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            request=request,
            bill=bill,
            bill_lines=bill_lines,
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
                period_key=f"{request.debit_note_date.year:04d}",
                prefix=NUMBER_PREFIX,
            )
        debit_note = PurchaseDebitNote(
            tenant_id=tenant_id,
            company_id=request.company_id,
            number=number,
            supplier_id=supplier.id,
            bill_id=bill.id if bill is not None else None,
            debit_note_date=request.debit_note_date,
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
        session.add(debit_note)
        await session.flush()
        lines = tuple(
            PurchaseDebitNoteLine(
                tenant_id=tenant_id,
                company_id=request.company_id,
                debit_note_id=debit_note.id,
                bill_line_id=line.bill_line_id,
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
            journal_entry = await _post_debit_note(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                debit_note=debit_note,
                lines=lines,
            )
        await record_debit_note_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=(
                "purchasing.debit_note.posted" if request.post else "purchasing.debit_note.created"
            ),
            debit_note=debit_note,
        )
        return DebitNoteResult(debit_note=debit_note, lines=lines, journal_entry=journal_entry)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_debit_note(error) from None
    except DBAPIError as error:
        raise _translate_debit_note(error) from None


async def post_debit_note(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    debit_note_id: UUID,
) -> DebitNoteResult:
    """Post an existing draft debit note."""

    async def operation(session: AsyncSession) -> DebitNoteResult:
        debit_note = (
            await session.execute(
                select(PurchaseDebitNote)
                .where(
                    PurchaseDebitNote.tenant_id == tenant_id,
                    PurchaseDebitNote.company_id == company_id,
                    PurchaseDebitNote.id == debit_note_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if debit_note is None:
            raise AppError(
                code="debit_note_not_found",
                title="Not found",
                status_code=404,
                detail="The purchase debit note does not exist in the selected company.",
            )
        lines = (
            (
                await session.execute(
                    select(PurchaseDebitNoteLine)
                    .where(
                        PurchaseDebitNoteLine.tenant_id == tenant_id,
                        PurchaseDebitNoteLine.debit_note_id == debit_note.id,
                    )
                    .order_by(PurchaseDebitNoteLine.line_number)
                )
            )
            .scalars()
            .all()
        )
        journal_entry = await _post_debit_note(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            debit_note=debit_note,
            lines=tuple(lines),
        )
        await record_debit_note_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="purchasing.debit_note.posted",
            debit_note=debit_note,
        )
        return DebitNoteResult(
            debit_note=debit_note, lines=tuple(lines), journal_entry=journal_entry
        )

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate_debit_note(error) from None
    except DBAPIError as error:
        raise _translate_debit_note(error) from None


async def get_debit_note(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, debit_note_id: UUID
) -> tuple[PurchaseDebitNote, tuple[PurchaseDebitNoteLine, ...]]:
    """Load one debit note and its lines."""
    debit_note = (
        await session.execute(
            select(PurchaseDebitNote).where(
                PurchaseDebitNote.tenant_id == tenant_id,
                PurchaseDebitNote.company_id == company_id,
                PurchaseDebitNote.id == debit_note_id,
            )
        )
    ).scalar_one_or_none()
    if debit_note is None:
        raise AppError(
            code="debit_note_not_found",
            title="Not found",
            status_code=404,
            detail="The purchase debit note does not exist in the selected company.",
        )
    lines = (
        (
            await session.execute(
                select(PurchaseDebitNoteLine)
                .where(
                    PurchaseDebitNoteLine.tenant_id == tenant_id,
                    PurchaseDebitNoteLine.debit_note_id == debit_note.id,
                )
                .order_by(PurchaseDebitNoteLine.line_number)
            )
        )
        .scalars()
        .all()
    )
    return debit_note, tuple(lines)


async def list_debit_notes(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[PurchaseDebitNote]:
    """List debit notes newest first."""
    return list(
        (
            await session.execute(
                select(PurchaseDebitNote)
                .where(
                    PurchaseDebitNote.tenant_id == tenant_id,
                    PurchaseDebitNote.company_id == company_id,
                )
                .order_by(PurchaseDebitNote.debit_note_date.desc(), PurchaseDebitNote.number.desc())
            )
        )
        .scalars()
        .all()
    )


def _translate_debit_note(error: DBAPIError) -> AppError:
    message = str(error.orig).lower()
    if "uq_purchase_debit_note_tenant_company_number" in message:
        return conflict("duplicate_document_number", "This debit note number already exists.")
    if "uq_journal_entry_source" in message:
        return conflict("source_duplicate", "This debit note is already posted.")
    if "uq_stock_move_document_item_type" in message:
        return conflict("stock_move_duplicate", "This debit note already moved stock.")
    return AppError(
        code="debit_note_rejected",
        title="Conflict",
        status_code=409,
        detail="The debit note could not be saved because it conflicts with existing data.",
    )


async def record_debit_note_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    action: str,
    debit_note: PurchaseDebitNote,
) -> None:
    """Record the debit-note lifecycle event."""
    from app.modules.identity.audit import record_audit

    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type="purchase_debit_note",
        entity_id=debit_note.id,
        detail={
            "number": debit_note.number,
            "total_minor": debit_note.total_minor,
            "currency": debit_note.currency,
        },
    )
