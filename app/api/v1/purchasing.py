"""Purchase bill endpoints (phase 0, day 7).

Bills mirror invoices: drafts or immediate posting, optional explicit number,
generated PB-<year>-NNNNNN otherwise, balanced journal entry and stock receipt
in the same transaction.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import PrincipalDep, TenantSessionDep, UnitOfWorkDep, require_company_permission
from app.modules.catalog.models import Supplier
from app.modules.identity.service import CompanyAccess
from app.modules.purchasing import debit_notes, service
from app.modules.purchasing.service import PurchaseBillInput, PurchaseLineInput
from app.modules.reports import render
from app.shared.errors import AppError

router = APIRouter(prefix="/purchasing", tags=["purchasing"])


class PurchaseLineIn(BaseModel):
    """One bill line in the document currency."""

    description: Annotated[str, Field(min_length=1, max_length=500)]
    quantity: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
    unit_price_minor: Annotated[int, Field(gt=0)]
    item_id: UUID | None = None
    tax_code_id: UUID | None = None


class PurchaseBillCreate(BaseModel):
    """A supplier bill to create as a draft or post immediately."""

    supplier_id: UUID
    bill_date: date
    due_date: date | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] = "IQD"
    exchange_rate: Annotated[Decimal, Field(gt=0)] = Decimal("1")
    memo: Annotated[str | None, Field(max_length=500)] = None
    number: Annotated[str | None, Field(min_length=1, max_length=40)] = None
    post: bool = False
    lines: Annotated[list[PurchaseLineIn], Field(min_length=1, max_length=200)]


class PurchaseLineOut(BaseModel):
    """One stored bill line."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_number: int
    item_id: UUID | None
    tax_code_id: UUID | None
    description: str
    quantity: Decimal
    unit_price_minor: int
    line_subtotal_minor: int
    line_tax_minor: int
    line_total_minor: int
    base_line_subtotal_minor: int
    base_line_tax_minor: int


class PurchaseBillOut(BaseModel):
    """A bill header with totals and its lines."""

    id: UUID
    number: str
    supplier_id: UUID
    bill_date: date
    due_date: date | None
    currency: str
    exchange_rate: Decimal
    status: str
    memo: str | None
    subtotal_minor: int
    tax_minor: int
    total_minor: int
    base_subtotal_minor: int
    base_tax_minor: int
    base_total_minor: int
    journal_entry_id: UUID | None
    posted_at: datetime | None
    lines: list[PurchaseLineOut]


def _body(result: service.PurchaseBillResult) -> PurchaseBillOut:
    bill = result.bill
    return PurchaseBillOut(
        id=bill.id,
        number=bill.number,
        supplier_id=bill.supplier_id,
        bill_date=bill.bill_date,
        due_date=bill.due_date,
        currency=bill.currency,
        exchange_rate=bill.exchange_rate,
        status=bill.status,
        memo=bill.memo,
        subtotal_minor=bill.subtotal_minor,
        tax_minor=bill.tax_minor,
        total_minor=bill.total_minor,
        base_subtotal_minor=bill.base_subtotal_minor,
        base_tax_minor=bill.base_tax_minor,
        base_total_minor=bill.base_total_minor,
        journal_entry_id=bill.journal_entry_id,
        posted_at=bill.posted_at,
        lines=[PurchaseLineOut.model_validate(line) for line in result.lines],
    )


@router.post(
    "/bills",
    response_model=PurchaseBillOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a supplier bill",
)
async def create_bill(
    payload: PurchaseBillCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[
        CompanyAccess, Depends(require_company_permission("purchasing.bill.create"))
    ],
) -> PurchaseBillOut:
    """Create a draft bill, or post it when the payload asks and the user may post."""
    if payload.post and "purchasing.bill.post" not in company.permissions:
        raise AppError(
            code="permission_denied",
            title="Forbidden",
            status_code=403,
            detail="The permission 'purchasing.bill.post' is required in the selected company.",
        )
    result = await service.create_purchase_bill(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=PurchaseBillInput(
            company_id=company.id,
            supplier_id=payload.supplier_id,
            bill_date=payload.bill_date,
            due_date=payload.due_date,
            currency=payload.currency.upper(),
            exchange_rate=payload.exchange_rate,
            memo=payload.memo,
            number=payload.number,
            post=payload.post,
            lines=tuple(
                PurchaseLineInput(
                    description=line.description,
                    quantity=line.quantity,
                    unit_price_minor=line.unit_price_minor,
                    item_id=line.item_id,
                    tax_code_id=line.tax_code_id,
                )
                for line in payload.lines
            ),
        ),
    )
    return _body(result)


@router.get("/bills", response_model=list[PurchaseBillOut], summary="List bills")
async def read_bills(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[PurchaseBillOut]:
    """List bills newest first, each with its lines."""
    bills = await service.list_purchase_bills(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    bodies: list[PurchaseBillOut] = []
    for bill in bills:
        _, lines = await service.get_purchase_bill(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            bill_id=bill.id,
        )
        bodies.append(_body(service.PurchaseBillResult(bill=bill, lines=lines, journal_entry=None)))
    return bodies


@router.get("/bills/{bill_id}", response_model=PurchaseBillOut, summary="Read a bill")
async def read_bill(
    bill_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> PurchaseBillOut:
    """Return one bill with its lines."""
    bill, lines = await service.get_purchase_bill(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        bill_id=bill_id,
    )
    return _body(service.PurchaseBillResult(bill=bill, lines=lines, journal_entry=None))


@router.get(
    "/bills/{bill_id}/pdf",
    response_class=Response,
    summary="Read a bill as an Arabic PDF",
)
async def read_bill_pdf(
    bill_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> Response:
    """Return one purchase bill and its lines as a printable Arabic PDF."""
    bill, lines = await service.get_purchase_bill(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        bill_id=bill_id,
    )
    supplier_name = (
        await session.scalar(
            select(Supplier.name_ar).where(
                Supplier.tenant_id == principal.tenant_id,
                Supplier.company_id == company.id,
                Supplier.id == bill.supplier_id,
            )
        )
        or "—"
    )
    content = render.render_document_pdf(
        title_ar="فاتورة شراء",
        company_name_ar=company.name_ar,
        number=bill.number,
        document_date=bill.bill_date.isoformat(),
        party_label_ar="المورد",
        party_name=supplier_name,
        currency=bill.currency,
        status=bill.status,
        lines=[
            (
                line.description,
                format(line.quantity.normalize(), "f"),
                line.unit_price_minor,
                line.line_total_minor,
            )
            for line in lines
        ],
        subtotal_minor=bill.subtotal_minor,
        tax_minor=bill.tax_minor,
        total_minor=bill.total_minor,
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{bill.number}.pdf"',
        },
    )


@router.post(
    "/bills/{bill_id}/post",
    response_model=PurchaseBillOut,
    summary="Post a draft bill",
)
async def post_bill(
    bill_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("purchasing.bill.post"))],
) -> PurchaseBillOut:
    """Post a draft bill, receiving stock and creating its journal entry."""
    result = await service.post_purchase_bill(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        bill_id=bill_id,
    )
    return _body(result)


class DebitNoteLineIn(BaseModel):
    """One debit-note line in the document currency."""

    description: Annotated[str, Field(min_length=1, max_length=500)]
    quantity: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
    unit_price_minor: Annotated[int, Field(ge=0)]
    item_id: UUID | None = None
    tax_code_id: UUID | None = None
    bill_line_id: UUID | None = None


class DebitNoteCreate(BaseModel):
    """A supplier debit note to create as a draft or post immediately."""

    supplier_id: UUID
    bill_id: UUID | None = None
    debit_note_date: date
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] = "IQD"
    exchange_rate: Annotated[Decimal, Field(gt=0)] = Decimal("1")
    memo: Annotated[str | None, Field(max_length=500)] = None
    number: Annotated[str | None, Field(min_length=1, max_length=40)] = None
    post: bool = False
    lines: Annotated[list[DebitNoteLineIn], Field(min_length=1, max_length=200)]


class DebitNoteLineOut(BaseModel):
    """One stored debit-note line."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_number: int
    bill_line_id: UUID | None
    item_id: UUID | None
    tax_code_id: UUID | None
    description: str
    quantity: Decimal
    unit_price_minor: int
    line_subtotal_minor: int
    line_tax_minor: int
    line_total_minor: int
    base_line_subtotal_minor: int
    base_line_tax_minor: int


class DebitNoteOut(BaseModel):
    """A debit note header with totals and its lines."""

    id: UUID
    number: str
    supplier_id: UUID
    bill_id: UUID | None
    debit_note_date: date
    currency: str
    exchange_rate: Decimal
    status: str
    memo: str | None
    subtotal_minor: int
    tax_minor: int
    total_minor: int
    base_subtotal_minor: int
    base_tax_minor: int
    base_total_minor: int
    journal_entry_id: UUID | None
    posted_at: datetime | None
    lines: list[DebitNoteLineOut]


def _debit_note_body(result: debit_notes.DebitNoteResult) -> DebitNoteOut:
    note = result.debit_note
    return DebitNoteOut(
        id=note.id,
        number=note.number,
        supplier_id=note.supplier_id,
        bill_id=note.bill_id,
        debit_note_date=note.debit_note_date,
        currency=note.currency,
        exchange_rate=note.exchange_rate,
        status=note.status,
        memo=note.memo,
        subtotal_minor=note.subtotal_minor,
        tax_minor=note.tax_minor,
        total_minor=note.total_minor,
        base_subtotal_minor=note.base_subtotal_minor,
        base_tax_minor=note.base_tax_minor,
        base_total_minor=note.base_total_minor,
        journal_entry_id=note.journal_entry_id,
        posted_at=note.posted_at,
        lines=[DebitNoteLineOut.model_validate(line) for line in result.lines],
    )


@router.post(
    "/debit-notes",
    response_model=DebitNoteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a supplier debit note",
)
async def create_debit_note(
    payload: DebitNoteCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[
        CompanyAccess, Depends(require_company_permission("purchasing.debit_note.create"))
    ],
) -> DebitNoteOut:
    """Create a draft debit note, or post it when the payload asks and the user may post."""
    if payload.post and "purchasing.debit_note.post" not in company.permissions:
        raise AppError(
            code="permission_denied",
            title="Forbidden",
            status_code=403,
            detail=(
                "The permission 'purchasing.debit_note.post' is required in the selected company."
            ),
        )
    result = await debit_notes.create_debit_note(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=debit_notes.DebitNoteInput(
            company_id=company.id,
            supplier_id=payload.supplier_id,
            bill_id=payload.bill_id,
            debit_note_date=payload.debit_note_date,
            currency=payload.currency.upper(),
            exchange_rate=payload.exchange_rate,
            memo=payload.memo,
            number=payload.number,
            post=payload.post,
            lines=tuple(
                debit_notes.DebitNoteLineInput(
                    description=line.description,
                    quantity=line.quantity,
                    unit_price_minor=line.unit_price_minor,
                    item_id=line.item_id,
                    tax_code_id=line.tax_code_id,
                    bill_line_id=line.bill_line_id,
                )
                for line in payload.lines
            ),
        ),
    )
    return _debit_note_body(result)


@router.get("/debit-notes", response_model=list[DebitNoteOut], summary="List supplier debit notes")
async def read_debit_notes(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[DebitNoteOut]:
    """List debit notes newest first, each with its lines."""
    notes = await debit_notes.list_debit_notes(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    bodies: list[DebitNoteOut] = []
    for note in notes:
        _, lines = await debit_notes.get_debit_note(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            debit_note_id=note.id,
        )
        bodies.append(
            _debit_note_body(
                debit_notes.DebitNoteResult(debit_note=note, lines=lines, journal_entry=None)
            )
        )
    return bodies


@router.get(
    "/debit-notes/{debit_note_id}",
    response_model=DebitNoteOut,
    summary="Read a supplier debit note",
)
async def read_debit_note(
    debit_note_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> DebitNoteOut:
    """Return one debit note with its lines."""
    note, lines = await debit_notes.get_debit_note(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        debit_note_id=debit_note_id,
    )
    return _debit_note_body(
        debit_notes.DebitNoteResult(debit_note=note, lines=lines, journal_entry=None)
    )


@router.post(
    "/debit-notes/{debit_note_id}/post",
    response_model=DebitNoteOut,
    summary="Post a draft supplier debit note",
)
async def post_debit_note(
    debit_note_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[
        CompanyAccess, Depends(require_company_permission("purchasing.debit_note.post"))
    ],
) -> DebitNoteOut:
    """Post a draft debit note and create its journal entry and stock return."""
    result = await debit_notes.post_debit_note(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        debit_note_id=debit_note_id,
    )
    return _debit_note_body(result)
