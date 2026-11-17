"""Sales invoice endpoints (phase 0, day 7).

Creation accepts an optional explicit invoice number; when omitted the service
allocates the next SI-<year>-NNNNNN number under a row lock. A duplicate number
is a 409. Posting permission is required when the request asks to post
immediately, so a user with create-only rights can still save a draft.
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
from app.modules.catalog.models import Customer
from app.modules.identity.service import CompanyAccess
from app.modules.reports import render
from app.modules.sales import credit_notes, service
from app.modules.sales.service import SalesInvoiceInput, SalesLineInput
from app.shared.errors import AppError

router = APIRouter(prefix="/sales", tags=["sales"])


class SalesLineIn(BaseModel):
    """One invoice line in the document currency."""

    description: Annotated[str, Field(min_length=1, max_length=500)]
    quantity: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
    unit_price_minor: Annotated[int, Field(gt=0)]
    item_id: UUID | None = None
    tax_code_id: UUID | None = None


class SalesInvoiceCreate(BaseModel):
    """A customer invoice to create as a draft or post immediately."""

    customer_id: UUID
    invoice_date: date
    due_date: date | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] = "IQD"
    exchange_rate: Annotated[Decimal, Field(gt=0)] = Decimal("1")
    memo: Annotated[str | None, Field(max_length=500)] = None
    number: Annotated[str | None, Field(min_length=1, max_length=40)] = None
    post: bool = False
    lines: Annotated[list[SalesLineIn], Field(min_length=1, max_length=200)]


class SalesLineOut(BaseModel):
    """One stored invoice line."""

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


class SalesInvoiceOut(BaseModel):
    """An invoice header with totals and its lines."""

    id: UUID
    number: str
    customer_id: UUID
    invoice_date: date
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
    cogs_entry_id: UUID | None
    posted_at: datetime | None
    lines: list[SalesLineOut]


def _body(result: service.SalesInvoiceResult) -> SalesInvoiceOut:
    invoice = result.invoice
    return SalesInvoiceOut(
        id=invoice.id,
        number=invoice.number,
        customer_id=invoice.customer_id,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        currency=invoice.currency,
        exchange_rate=invoice.exchange_rate,
        status=invoice.status,
        memo=invoice.memo,
        subtotal_minor=invoice.subtotal_minor,
        tax_minor=invoice.tax_minor,
        total_minor=invoice.total_minor,
        base_subtotal_minor=invoice.base_subtotal_minor,
        base_tax_minor=invoice.base_tax_minor,
        base_total_minor=invoice.base_total_minor,
        journal_entry_id=invoice.journal_entry_id,
        cogs_entry_id=invoice.cogs_entry_id,
        posted_at=invoice.posted_at,
        lines=[SalesLineOut.model_validate(line) for line in result.lines],
    )


@router.post(
    "/invoices",
    response_model=SalesInvoiceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer invoice",
)
async def create_invoice(
    payload: SalesInvoiceCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("sales.invoice.create"))],
) -> SalesInvoiceOut:
    """Create a draft invoice, or post it when the payload asks and the user may post."""
    if payload.post and "sales.invoice.post" not in company.permissions:
        raise AppError(
            code="permission_denied",
            title="Forbidden",
            status_code=403,
            detail="The permission 'sales.invoice.post' is required in the selected company.",
        )
    result = await service.create_sales_invoice(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=SalesInvoiceInput(
            company_id=company.id,
            customer_id=payload.customer_id,
            invoice_date=payload.invoice_date,
            due_date=payload.due_date,
            currency=payload.currency.upper(),
            exchange_rate=payload.exchange_rate,
            memo=payload.memo,
            number=payload.number,
            post=payload.post,
            lines=tuple(
                SalesLineInput(
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


@router.get("/invoices", response_model=list[SalesInvoiceOut], summary="List invoices")
async def read_invoices(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[SalesInvoiceOut]:
    """List invoices newest first, each with its lines."""
    invoices = await service.list_sales_invoices(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    bodies: list[SalesInvoiceOut] = []
    for invoice in invoices:
        _, lines = await service.get_sales_invoice(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            invoice_id=invoice.id,
        )
        bodies.append(
            _body(
                service.SalesInvoiceResult(
                    invoice=invoice, lines=lines, journal_entry=None, cogs_entry=None
                )
            )
        )
    return bodies


@router.get("/invoices/{invoice_id}", response_model=SalesInvoiceOut, summary="Read an invoice")
async def read_invoice(
    invoice_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> SalesInvoiceOut:
    """Return one invoice with its lines."""
    invoice, lines = await service.get_sales_invoice(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        invoice_id=invoice_id,
    )
    return _body(
        service.SalesInvoiceResult(
            invoice=invoice, lines=lines, journal_entry=None, cogs_entry=None
        )
    )


@router.get(
    "/invoices/{invoice_id}/pdf",
    response_class=Response,
    summary="Read an invoice as an Arabic PDF",
)
async def read_invoice_pdf(
    invoice_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> Response:
    """Return one invoice and its lines as a printable Arabic PDF."""
    invoice, lines = await service.get_sales_invoice(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        invoice_id=invoice_id,
    )
    customer_name = (
        await session.scalar(
            select(Customer.name_ar).where(
                Customer.tenant_id == principal.tenant_id,
                Customer.company_id == company.id,
                Customer.id == invoice.customer_id,
            )
        )
        or "—"
    )
    content = render.render_document_pdf(
        title_ar="فاتورة مبيعات",
        company_name_ar=company.name_ar,
        number=invoice.number,
        document_date=invoice.invoice_date.isoformat(),
        party_label_ar="العميل",
        party_name=customer_name,
        currency=invoice.currency,
        status=invoice.status,
        lines=[
            (
                line.description,
                format(line.quantity.normalize(), "f"),
                line.unit_price_minor,
                line.line_total_minor,
            )
            for line in lines
        ],
        subtotal_minor=invoice.subtotal_minor,
        tax_minor=invoice.tax_minor,
        total_minor=invoice.total_minor,
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{invoice.number}.pdf"',
        },
    )


@router.post(
    "/invoices/{invoice_id}/post",
    response_model=SalesInvoiceOut,
    summary="Post a draft invoice",
)
async def post_invoice(
    invoice_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("sales.invoice.post"))],
) -> SalesInvoiceOut:
    """Post a draft invoice and create its journal and cost-of-sales entries."""
    result = await service.post_sales_invoice(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        invoice_id=invoice_id,
    )
    return _body(result)


class CreditNoteLineIn(BaseModel):
    """One credit-note line in the document currency."""

    description: Annotated[str, Field(min_length=1, max_length=500)]
    quantity: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
    unit_price_minor: Annotated[int, Field(ge=0)]
    item_id: UUID | None = None
    tax_code_id: UUID | None = None
    invoice_line_id: UUID | None = None


class CreditNoteCreate(BaseModel):
    """A customer credit note to create as a draft or post immediately."""

    customer_id: UUID
    invoice_id: UUID | None = None
    credit_note_date: date
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] = "IQD"
    exchange_rate: Annotated[Decimal, Field(gt=0)] = Decimal("1")
    memo: Annotated[str | None, Field(max_length=500)] = None
    number: Annotated[str | None, Field(min_length=1, max_length=40)] = None
    post: bool = False
    lines: Annotated[list[CreditNoteLineIn], Field(min_length=1, max_length=200)]


class CreditNoteLineOut(BaseModel):
    """One stored credit-note line."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_number: int
    invoice_line_id: UUID | None
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


class CreditNoteOut(BaseModel):
    """A credit note header with totals and its lines."""

    id: UUID
    number: str
    customer_id: UUID
    invoice_id: UUID | None
    credit_note_date: date
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
    lines: list[CreditNoteLineOut]


def _credit_note_body(result: credit_notes.CreditNoteResult) -> CreditNoteOut:
    note = result.credit_note
    return CreditNoteOut(
        id=note.id,
        number=note.number,
        customer_id=note.customer_id,
        invoice_id=note.invoice_id,
        credit_note_date=note.credit_note_date,
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
        lines=[CreditNoteLineOut.model_validate(line) for line in result.lines],
    )


@router.post(
    "/credit-notes",
    response_model=CreditNoteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer credit note",
)
async def create_credit_note(
    payload: CreditNoteCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[
        CompanyAccess, Depends(require_company_permission("sales.credit_note.create"))
    ],
) -> CreditNoteOut:
    """Create a draft credit note, or post it when the payload asks and the user may post."""
    if payload.post and "sales.credit_note.post" not in company.permissions:
        raise AppError(
            code="permission_denied",
            title="Forbidden",
            status_code=403,
            detail="The permission 'sales.credit_note.post' is required in the selected company.",
        )
    result = await credit_notes.create_credit_note(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=credit_notes.CreditNoteInput(
            company_id=company.id,
            customer_id=payload.customer_id,
            invoice_id=payload.invoice_id,
            credit_note_date=payload.credit_note_date,
            currency=payload.currency.upper(),
            exchange_rate=payload.exchange_rate,
            memo=payload.memo,
            number=payload.number,
            post=payload.post,
            lines=tuple(
                credit_notes.CreditNoteLineInput(
                    description=line.description,
                    quantity=line.quantity,
                    unit_price_minor=line.unit_price_minor,
                    item_id=line.item_id,
                    tax_code_id=line.tax_code_id,
                    invoice_line_id=line.invoice_line_id,
                )
                for line in payload.lines
            ),
        ),
    )
    return _credit_note_body(result)


@router.get(
    "/credit-notes", response_model=list[CreditNoteOut], summary="List customer credit notes"
)
async def read_credit_notes(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[CreditNoteOut]:
    """List credit notes newest first, each with its lines."""
    notes = await credit_notes.list_credit_notes(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    bodies: list[CreditNoteOut] = []
    for note in notes:
        _, lines = await credit_notes.get_credit_note(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            credit_note_id=note.id,
        )
        bodies.append(
            _credit_note_body(
                credit_notes.CreditNoteResult(credit_note=note, lines=lines, journal_entry=None)
            )
        )
    return bodies


@router.get(
    "/credit-notes/{credit_note_id}",
    response_model=CreditNoteOut,
    summary="Read a customer credit note",
)
async def read_credit_note(
    credit_note_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> CreditNoteOut:
    """Return one credit note with its lines."""
    note, lines = await credit_notes.get_credit_note(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        credit_note_id=credit_note_id,
    )
    return _credit_note_body(
        credit_notes.CreditNoteResult(credit_note=note, lines=lines, journal_entry=None)
    )


@router.post(
    "/credit-notes/{credit_note_id}/post",
    response_model=CreditNoteOut,
    summary="Post a draft customer credit note",
)
async def post_credit_note(
    credit_note_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[
        CompanyAccess, Depends(require_company_permission("sales.credit_note.post"))
    ],
) -> CreditNoteOut:
    """Post a draft credit note and create its journal entry and stock return."""
    result = await credit_notes.post_credit_note(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        credit_note_id=credit_note_id,
    )
    return _credit_note_body(result)
