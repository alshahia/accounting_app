"""Purchase bill creation and posting (phase 0, day 7).

A posted bill receives stocked lines at the line's base-currency cost, debits
inventory and input VAT, and credits accounts payable, all in one transaction.
The stock receipt value is exactly the base line amount posted to inventory, so
inventory GL and item values never drift apart. Duplicate bill numbers are
refused and foreign-currency bills convert at their own exchange rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import Item, Supplier, TaxCode
from app.modules.catalog.tax import rates_on as tax_rates_on
from app.modules.inventory.service import receive_stock
from app.modules.ledger.mapping import resolve_posting_accounts
from app.modules.ledger.models import AccountingPeriod
from app.modules.ledger.service import (
    EntryResult,
    JournalLineInput,
    post_entry_in_session,
)
from app.modules.purchasing.models import PurchaseBill, PurchaseBillLine
from app.shared.errors import AppError
from app.shared.numbering import allocate_document_number
from app.shared.unit_of_work import UnitOfWork

NUMBER_SCOPE = "purchase_bill"
NUMBER_PREFIX = "PB"


@dataclass(frozen=True)
class PurchaseLineInput:
    """One bill line in the document currency."""

    description: str
    quantity: Decimal
    unit_price_minor: int
    item_id: UUID | None = None
    tax_code_id: UUID | None = None


@dataclass(frozen=True)
class PurchaseBillInput:
    """Everything needed to create a draft or posted supplier bill."""

    company_id: UUID
    supplier_id: UUID
    bill_date: date
    lines: tuple[PurchaseLineInput, ...]
    due_date: date | None = None
    currency: str = "IQD"
    exchange_rate: Decimal = Decimal("1")
    memo: str | None = None
    number: str | None = None
    post: bool = False


@dataclass(frozen=True)
class PurchaseBillResult:
    """The bill, its lines and the posting entry (if any)."""

    bill: PurchaseBill
    lines: tuple[PurchaseBillLine, ...]
    journal_entry: EntryResult | None


def bad(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Unprocessable entity", status_code=422, detail=detail)


def conflict(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Conflict", status_code=409, detail=detail)


def minor(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def load_supplier(
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
        raise AppError(
            code="supplier_not_found",
            title="Not found",
            status_code=404,
            detail="The supplier does not exist in the selected company.",
        )
    if not supplier.is_active:
        raise bad("supplier_inactive", f"Supplier {supplier.code} is not active.")
    return supplier


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


def _validate_input(request: PurchaseBillInput) -> None:
    if not request.lines:
        raise bad("bill_lines_invalid", "A bill needs at least one line.")
    if len(request.lines) > 200:
        raise bad("bill_lines_invalid", "A bill can have at most 200 lines.")
    if len(request.currency) != 3 or not request.currency.isalpha():
        raise bad("currency_invalid", "The currency must be a three-letter ISO code.")
    if request.exchange_rate <= 0:
        raise bad("exchange_rate_invalid", "The exchange rate must be positive.")
    if request.number is not None and not 1 <= len(request.number) <= 40:
        raise bad("document_number_invalid", "A document number is 1-40 characters.")
    for index, line in enumerate(request.lines, start=1):
        if line.quantity <= 0:
            raise bad("bill_lines_invalid", f"Line {index} must have a positive quantity.")
        if line.unit_price_minor <= 0:
            raise bad("bill_lines_invalid", f"Line {index} must have a positive unit price.")


async def _post_bill(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    bill: PurchaseBill,
    lines: tuple[PurchaseBillLine, ...],
) -> EntryResult:
    if bill.status != "draft":
        raise conflict("bill_already_posted", "This bill is already posted.")
    period = await resolve_open_period(
        session, tenant_id=tenant_id, company_id=bill.company_id, on_date=bill.bill_date
    )
    accounts = await resolve_posting_accounts(
        session,
        tenant_id=tenant_id,
        company_id=bill.company_id,
        on_date=bill.bill_date,
        keys=("inventory", "vat_payable", "accounts_payable"),
    )
    for line in sorted(lines, key=lambda row: row.line_number):
        if line.item_id is None:
            continue
        await receive_stock(
            session,
            tenant_id=tenant_id,
            company_id=bill.company_id,
            item_id=line.item_id,
            quantity=line.quantity,
            value_minor=line.base_line_subtotal_minor,
            document_type="purchase_bill",
            document_id=bill.id,
            occurred_on=bill.bill_date,
            actor_user_id=actor_user_id,
        )
    journal_lines = [
        JournalLineInput(
            account_id=accounts["inventory"].id,
            debit_minor=bill.base_subtotal_minor,
            description=f"Bill {bill.number} inventory",
        )
    ]
    if bill.base_tax_minor > 0:
        journal_lines.append(
            JournalLineInput(
                account_id=accounts["vat_payable"].id,
                debit_minor=bill.base_tax_minor,
                description=f"Bill {bill.number} input VAT",
            )
        )
    journal_lines.append(
        JournalLineInput(
            account_id=accounts["accounts_payable"].id,
            credit_minor=bill.base_total_minor,
            description=f"Bill {bill.number} payable",
        )
    )
    journal_entry = await post_entry_in_session(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        company_id=bill.company_id,
        period_id=period.id,
        entry_date=bill.bill_date,
        memo=bill.memo or f"Purchase bill {bill.number}",
        lines=tuple(journal_lines),
        source_type="purchase_bill",
        source_id=bill.id,
    )
    bill.status = "posted"
    bill.journal_entry_id = journal_entry.entry.id
    bill.posted_at = datetime.now(UTC)
    bill.posted_by = actor_user_id
    await session.flush()
    return journal_entry


async def _load_bill(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, bill_id: UUID
) -> tuple[PurchaseBill, tuple[PurchaseBillLine, ...]]:
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
    lines = (
        (
            await session.execute(
                select(PurchaseBillLine)
                .where(
                    PurchaseBillLine.tenant_id == tenant_id,
                    PurchaseBillLine.company_id == company_id,
                    PurchaseBillLine.bill_id == bill.id,
                )
                .order_by(PurchaseBillLine.line_number)
            )
        )
        .scalars()
        .all()
    )
    return bill, tuple(lines)


async def create_purchase_bill(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: PurchaseBillInput,
) -> PurchaseBillResult:
    """Create a draft bill, optionally posting it immediately."""
    _validate_input(request)

    async def operation(session: AsyncSession) -> PurchaseBillResult:
        supplier = await load_supplier(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            supplier_id=request.supplier_id,
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
            on_date=request.bill_date,
        )
        await load_items(session, tenant_id=tenant_id, company_id=request.company_id, ids=item_ids)

        prepared: list[tuple[PurchaseLineInput, int, int, int, int]] = []
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
                period_key=f"{request.bill_date.year:04d}",
                prefix=NUMBER_PREFIX,
            )
        bill = PurchaseBill(
            tenant_id=tenant_id,
            company_id=request.company_id,
            number=number,
            supplier_id=supplier.id,
            bill_date=request.bill_date,
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
        session.add(bill)
        await session.flush()
        lines = tuple(
            PurchaseBillLine(
                tenant_id=tenant_id,
                company_id=request.company_id,
                bill_id=bill.id,
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
            journal_entry = await _post_bill(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                bill=bill,
                lines=lines,
            )
        await _record_purchasing_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="purchasing.bill.posted" if request.post else "purchasing.bill.created",
            bill=bill,
        )
        return PurchaseBillResult(bill=bill, lines=lines, journal_entry=journal_entry)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate(error) from None
    except DBAPIError as error:
        raise _translate(error) from None


async def post_purchase_bill(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    bill_id: UUID,
) -> PurchaseBillResult:
    """Post an existing draft bill."""

    async def operation(session: AsyncSession) -> PurchaseBillResult:
        bill = (
            await session.execute(
                select(PurchaseBill)
                .where(
                    PurchaseBill.tenant_id == tenant_id,
                    PurchaseBill.company_id == company_id,
                    PurchaseBill.id == bill_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if bill is None:
            raise AppError(
                code="bill_not_found",
                title="Not found",
                status_code=404,
                detail="The purchase bill does not exist in the selected company.",
            )
        lines = (
            (
                await session.execute(
                    select(PurchaseBillLine)
                    .where(
                        PurchaseBillLine.tenant_id == tenant_id,
                        PurchaseBillLine.bill_id == bill.id,
                    )
                    .order_by(PurchaseBillLine.line_number)
                )
            )
            .scalars()
            .all()
        )
        journal_entry = await _post_bill(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            bill=bill,
            lines=tuple(lines),
        )
        await _record_purchasing_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="purchasing.bill.posted",
            bill=bill,
        )
        return PurchaseBillResult(bill=bill, lines=tuple(lines), journal_entry=journal_entry)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        raise _translate(error) from None
    except DBAPIError as error:
        raise _translate(error) from None


async def get_purchase_bill(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID, bill_id: UUID
) -> tuple[PurchaseBill, tuple[PurchaseBillLine, ...]]:
    """Load one bill and its lines."""
    return await _load_bill(session, tenant_id=tenant_id, company_id=company_id, bill_id=bill_id)


async def list_purchase_bills(
    session: AsyncSession, *, tenant_id: UUID, company_id: UUID
) -> list[PurchaseBill]:
    """List bills newest first."""
    return list(
        (
            await session.execute(
                select(PurchaseBill)
                .where(
                    PurchaseBill.tenant_id == tenant_id,
                    PurchaseBill.company_id == company_id,
                )
                .order_by(PurchaseBill.bill_date.desc(), PurchaseBill.number.desc())
            )
        )
        .scalars()
        .all()
    )


def _translate(error: DBAPIError) -> AppError:
    message = str(error.orig).lower()
    if "uq_purchase_bill_tenant_company_number" in message:
        return conflict("duplicate_document_number", "This bill number already exists.")
    if "uq_journal_entry_source" in message:
        return conflict("source_duplicate", "This bill is already posted.")
    if "uq_stock_move_document_item_type" in message:
        return conflict("stock_move_duplicate", "This bill already moved stock.")
    return AppError(
        code="bill_rejected",
        title="Conflict",
        status_code=409,
        detail="The bill could not be saved because it conflicts with existing data.",
    )


async def _record_purchasing_audit(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    action: str,
    bill: PurchaseBill,
) -> None:
    from app.modules.identity.audit import record_audit

    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type="purchase_bill",
        entity_id=bill.id,
        detail={
            "number": bill.number,
            "total_minor": bill.total_minor,
            "currency": bill.currency,
        },
    )
