"""Cash/bank account, receipt and payment endpoints (phase 1).

Creation and posting are separate permissions so a clerk can prepare a receipt
or payment and an accountant can post it; asking to post immediately without the
post permission is refused rather than silently downgraded. Listing and reading
use the reporting permission, since receipts are financial records.
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
from app.modules.cashbank import service
from app.modules.cashbank.service import (
    AllocationInput,
    CashBankAccountInput,
    PaymentInput,
    ReceiptInput,
)
from app.modules.catalog.models import Customer, Supplier
from app.modules.identity.service import CompanyAccess
from app.modules.purchasing.models import PurchaseBill
from app.modules.reports import render
from app.modules.sales.models import SalesInvoice
from app.shared.errors import AppError

router = APIRouter(prefix="/cashbank", tags=["cashbank"])


class CashBankAccountCreate(BaseModel):
    """A cash box or bank account to create."""

    code: Annotated[str, Field(min_length=1, max_length=30)]
    name_ar: Annotated[str, Field(min_length=1, max_length=200)]
    name_en: Annotated[str | None, Field(max_length=200)] = None
    kind: Annotated[str, Field(pattern=r"^(cash|bank)$")]
    ledger_account_id: UUID
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] = "USD"


class CashBankAccountOut(BaseModel):
    """A stored cash or bank account."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name_ar: str
    name_en: str | None
    kind: str
    ledger_account_id: UUID
    currency: str
    is_active: bool


class AllocationIn(BaseModel):
    """One invoice or bill allocation."""

    document_id: UUID
    amount_minor: Annotated[int, Field(gt=0)]


class ReceiptCreate(BaseModel):
    """A customer receipt to create as a draft or post immediately."""

    customer_id: UUID
    cash_bank_account_id: UUID
    receipt_date: date
    amount_minor: Annotated[int, Field(gt=0)]
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] = "USD"
    exchange_rate: Annotated[Decimal, Field(gt=0)] = Decimal("1")
    memo: Annotated[str | None, Field(max_length=500)] = None
    number: Annotated[str | None, Field(min_length=1, max_length=40)] = None
    post: bool = False
    withholding_tax_code_id: UUID | None = None
    withholding_base_minor: Annotated[int | None, Field(gt=0)] = None
    allocations: Annotated[list[AllocationIn], Field(max_length=200)] = []


class PaymentCreate(BaseModel):
    """A supplier payment to create as a draft or post immediately."""

    supplier_id: UUID
    cash_bank_account_id: UUID
    payment_date: date
    amount_minor: Annotated[int, Field(gt=0)]
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] = "USD"
    exchange_rate: Annotated[Decimal, Field(gt=0)] = Decimal("1")
    memo: Annotated[str | None, Field(max_length=500)] = None
    number: Annotated[str | None, Field(min_length=1, max_length=40)] = None
    post: bool = False
    withholding_tax_code_id: UUID | None = None
    withholding_base_minor: Annotated[int | None, Field(gt=0)] = None
    allocations: Annotated[list[AllocationIn], Field(max_length=200)] = []


class ReceiptAllocationOut(BaseModel):
    """One stored receipt allocation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    invoice_id: UUID
    amount_minor: int
    base_amount_minor: int
    settlement_base_minor: int


class ReceiptOut(BaseModel):
    """A receipt with its allocations."""

    id: UUID
    number: str
    customer_id: UUID
    cash_bank_account_id: UUID
    receipt_date: date
    currency: str
    exchange_rate: Decimal
    amount_minor: int
    base_amount_minor: int
    unallocated_minor: int
    withholding_tax_code_id: UUID | None
    withholding_base_minor: int
    withholding_amount_minor: int
    base_withholding_minor: int
    status: str
    memo: str | None
    journal_entry_id: UUID | None
    posted_at: datetime | None
    allocations: list[ReceiptAllocationOut]


class PaymentAllocationOut(BaseModel):
    """One stored supplier payment allocation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    bill_id: UUID
    amount_minor: int
    base_amount_minor: int
    settlement_base_minor: int


class PaymentOut(BaseModel):
    """A supplier payment with its allocations."""

    id: UUID
    number: str
    supplier_id: UUID
    cash_bank_account_id: UUID
    payment_date: date
    currency: str
    exchange_rate: Decimal
    amount_minor: int
    base_amount_minor: int
    unallocated_minor: int
    withholding_tax_code_id: UUID | None
    withholding_base_minor: int
    withholding_amount_minor: int
    base_withholding_minor: int
    status: str
    memo: str | None
    journal_entry_id: UUID | None
    posted_at: datetime | None
    allocations: list[PaymentAllocationOut]


def _receipt_body(result: service.ReceiptResult) -> ReceiptOut:
    receipt = result.receipt
    return ReceiptOut(
        id=receipt.id,
        number=receipt.number,
        customer_id=receipt.customer_id,
        cash_bank_account_id=receipt.cash_bank_account_id,
        receipt_date=receipt.receipt_date,
        currency=receipt.currency,
        exchange_rate=receipt.exchange_rate,
        amount_minor=receipt.amount_minor,
        base_amount_minor=receipt.base_amount_minor,
        unallocated_minor=receipt.unallocated_minor,
        withholding_tax_code_id=receipt.withholding_tax_code_id,
        withholding_base_minor=receipt.withholding_base_minor,
        withholding_amount_minor=receipt.withholding_amount_minor,
        base_withholding_minor=receipt.base_withholding_minor,
        status=receipt.status,
        memo=receipt.memo,
        journal_entry_id=receipt.journal_entry_id,
        posted_at=receipt.posted_at,
        allocations=[
            ReceiptAllocationOut.model_validate(allocation) for allocation in result.allocations
        ],
    )


def _payment_body(result: service.PaymentResult) -> PaymentOut:
    payment = result.payment
    return PaymentOut(
        id=payment.id,
        number=payment.number,
        supplier_id=payment.supplier_id,
        cash_bank_account_id=payment.cash_bank_account_id,
        payment_date=payment.payment_date,
        currency=payment.currency,
        exchange_rate=payment.exchange_rate,
        amount_minor=payment.amount_minor,
        base_amount_minor=payment.base_amount_minor,
        unallocated_minor=payment.unallocated_minor,
        withholding_tax_code_id=payment.withholding_tax_code_id,
        withholding_base_minor=payment.withholding_base_minor,
        withholding_amount_minor=payment.withholding_amount_minor,
        base_withholding_minor=payment.base_withholding_minor,
        status=payment.status,
        memo=payment.memo,
        journal_entry_id=payment.journal_entry_id,
        posted_at=payment.posted_at,
        allocations=[
            PaymentAllocationOut.model_validate(allocation) for allocation in result.allocations
        ],
    )


def _allocation_inputs(allocations: list[AllocationIn]) -> tuple[AllocationInput, ...]:
    return tuple(
        AllocationInput(document_id=allocation.document_id, amount_minor=allocation.amount_minor)
        for allocation in allocations
    )


@router.post(
    "/accounts",
    response_model=CashBankAccountOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a cash or bank account",
)
async def create_account(
    payload: CashBankAccountCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("cashbank.manage"))],
) -> CashBankAccountOut:
    """Create a cash box or bank account linked to a postable asset account."""
    result = await service.create_cash_bank_account(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=CashBankAccountInput(
            company_id=company.id,
            code=payload.code,
            name_ar=payload.name_ar,
            name_en=payload.name_en,
            kind=payload.kind,
            ledger_account_id=payload.ledger_account_id,
            currency=payload.currency,
        ),
    )
    return CashBankAccountOut.model_validate(result.account)


@router.get(
    "/accounts",
    response_model=list[CashBankAccountOut],
    summary="List cash and bank accounts",
)
async def read_accounts(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[CashBankAccountOut]:
    """List the selected company's cash and bank accounts."""
    accounts = await service.list_cash_bank_accounts(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    return [CashBankAccountOut.model_validate(account) for account in accounts]


@router.post(
    "/receipts",
    response_model=ReceiptOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer receipt",
)
async def create_receipt(
    payload: ReceiptCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[
        CompanyAccess, Depends(require_company_permission("cashbank.receipt.create"))
    ],
) -> ReceiptOut:
    """Create a draft receipt, or post it when allowed and requested."""
    if payload.post and "cashbank.receipt.post" not in company.permissions:
        raise AppError(
            code="permission_denied",
            title="Forbidden",
            status_code=403,
            detail="The permission 'cashbank.receipt.post' is required in the selected company.",
        )
    result = await service.create_receipt(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=ReceiptInput(
            company_id=company.id,
            customer_id=payload.customer_id,
            cash_bank_account_id=payload.cash_bank_account_id,
            receipt_date=payload.receipt_date,
            amount_minor=payload.amount_minor,
            allocations=_allocation_inputs(payload.allocations),
            currency=payload.currency.upper(),
            exchange_rate=payload.exchange_rate,
            memo=payload.memo,
            number=payload.number,
            post=payload.post,
            withholding_tax_code_id=payload.withholding_tax_code_id,
            withholding_base_minor=payload.withholding_base_minor,
        ),
    )
    return _receipt_body(result)


@router.get("/receipts", response_model=list[ReceiptOut], summary="List customer receipts")
async def read_receipts(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[ReceiptOut]:
    """List receipts newest first, each with its allocations."""
    receipts = await service.list_receipts(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    bodies: list[ReceiptOut] = []
    for receipt in receipts:
        _, allocations = await service.get_receipt(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            receipt_id=receipt.id,
        )
        bodies.append(
            _receipt_body(
                service.ReceiptResult(receipt=receipt, allocations=allocations, journal_entry=None)
            )
        )
    return bodies


@router.get("/receipts/{receipt_id}", response_model=ReceiptOut, summary="Read a receipt")
async def read_receipt(
    receipt_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> ReceiptOut:
    """Return one receipt with its allocations."""
    receipt, allocations = await service.get_receipt(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        receipt_id=receipt_id,
    )
    return _receipt_body(
        service.ReceiptResult(receipt=receipt, allocations=allocations, journal_entry=None)
    )


@router.get(
    "/receipts/{receipt_id}/pdf",
    response_class=Response,
    summary="Read a receipt as an Arabic PDF",
)
async def read_receipt_pdf(
    receipt_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> Response:
    """Return one receipt and its allocations as a printable Arabic PDF."""
    receipt, allocations = await service.get_receipt(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        receipt_id=receipt_id,
    )
    customer_name = (
        await session.scalar(
            select(Customer.name_ar).where(
                Customer.tenant_id == principal.tenant_id,
                Customer.company_id == company.id,
                Customer.id == receipt.customer_id,
            )
        )
        or "—"
    )
    numbers: dict[UUID, str] = {}
    if allocations:
        rows = await session.execute(
            select(SalesInvoice.id, SalesInvoice.number).where(
                SalesInvoice.tenant_id == principal.tenant_id,
                SalesInvoice.id.in_([allocation.invoice_id for allocation in allocations]),
            )
        )
        numbers = {row.id: row.number for row in rows}
    lines = [
        (
            f"تسديد فاتورة {numbers.get(allocation.invoice_id, '')}".strip(),
            "",
            0,
            allocation.amount_minor,
        )
        for allocation in allocations
    ]
    if receipt.unallocated_minor:
        lines.append(("دفعة غير مخصصة", "", 0, receipt.unallocated_minor))
    if not lines:
        lines = [("استلام نقدي", "", 0, receipt.amount_minor)]
    content = render.render_document_pdf(
        title_ar="سند قبض",
        company_name_ar=company.name_ar,
        number=receipt.number,
        document_date=receipt.receipt_date.isoformat(),
        party_label_ar="العميل",
        party_name=customer_name,
        currency=receipt.currency,
        status=receipt.status,
        lines=lines,
        subtotal_minor=receipt.amount_minor,
        tax_minor=receipt.withholding_amount_minor,
        total_minor=receipt.amount_minor - receipt.withholding_amount_minor,
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{receipt.number}.pdf"'},
    )


@router.post(
    "/receipts/{receipt_id}/post",
    response_model=ReceiptOut,
    summary="Post a draft receipt",
)
async def post_receipt(
    receipt_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("cashbank.receipt.post"))],
) -> ReceiptOut:
    """Post a draft receipt and settle its allocated invoices."""
    result = await service.post_receipt(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        receipt_id=receipt_id,
    )
    return _receipt_body(result)


@router.post(
    "/payments",
    response_model=PaymentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a supplier payment",
)
async def create_payment(
    payload: PaymentCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[
        CompanyAccess, Depends(require_company_permission("cashbank.payment.create"))
    ],
) -> PaymentOut:
    """Create a draft supplier payment, or post it when allowed and requested."""
    if payload.post and "cashbank.payment.post" not in company.permissions:
        raise AppError(
            code="permission_denied",
            title="Forbidden",
            status_code=403,
            detail="The permission 'cashbank.payment.post' is required in the selected company.",
        )
    result = await service.create_payment(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=PaymentInput(
            company_id=company.id,
            supplier_id=payload.supplier_id,
            cash_bank_account_id=payload.cash_bank_account_id,
            payment_date=payload.payment_date,
            amount_minor=payload.amount_minor,
            allocations=_allocation_inputs(payload.allocations),
            currency=payload.currency.upper(),
            exchange_rate=payload.exchange_rate,
            memo=payload.memo,
            number=payload.number,
            post=payload.post,
            withholding_tax_code_id=payload.withholding_tax_code_id,
            withholding_base_minor=payload.withholding_base_minor,
        ),
    )
    return _payment_body(result)


@router.get("/payments", response_model=list[PaymentOut], summary="List supplier payments")
async def read_payments(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[PaymentOut]:
    """List supplier payments newest first, each with its allocations."""
    payments = await service.list_payments(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    bodies: list[PaymentOut] = []
    for payment in payments:
        _, allocations = await service.get_payment(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            payment_id=payment.id,
        )
        bodies.append(
            _payment_body(
                service.PaymentResult(payment=payment, allocations=allocations, journal_entry=None)
            )
        )
    return bodies


@router.get("/payments/{payment_id}", response_model=PaymentOut, summary="Read a payment")
async def read_payment(
    payment_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> PaymentOut:
    """Return one supplier payment with its allocations."""
    payment, allocations = await service.get_payment(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        payment_id=payment_id,
    )
    return _payment_body(
        service.PaymentResult(payment=payment, allocations=allocations, journal_entry=None)
    )


@router.get(
    "/payments/{payment_id}/pdf",
    response_class=Response,
    summary="Read a supplier payment as an Arabic PDF",
)
async def read_payment_pdf(
    payment_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> Response:
    """Return one supplier payment and its allocations as a printable Arabic PDF."""
    payment, allocations = await service.get_payment(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        payment_id=payment_id,
    )
    supplier_name = (
        await session.scalar(
            select(Supplier.name_ar).where(
                Supplier.tenant_id == principal.tenant_id,
                Supplier.company_id == company.id,
                Supplier.id == payment.supplier_id,
            )
        )
        or "—"
    )
    numbers: dict[UUID, str] = {}
    if allocations:
        rows = await session.execute(
            select(PurchaseBill.id, PurchaseBill.number).where(
                PurchaseBill.tenant_id == principal.tenant_id,
                PurchaseBill.id.in_([allocation.bill_id for allocation in allocations]),
            )
        )
        numbers = {row.id: row.number for row in rows}
    lines = [
        (
            f"تسديد فاتورة شراء {numbers.get(allocation.bill_id, '')}".strip(),
            "",
            0,
            allocation.amount_minor,
        )
        for allocation in allocations
    ]
    if payment.unallocated_minor:
        lines.append(("دفعة مقدمة غير مخصصة", "", 0, payment.unallocated_minor))
    if not lines:
        lines = [("دفعة نقدية", "", 0, payment.amount_minor)]
    content = render.render_document_pdf(
        title_ar="سند دفع",
        company_name_ar=company.name_ar,
        number=payment.number,
        document_date=payment.payment_date.isoformat(),
        party_label_ar="المورد",
        party_name=supplier_name,
        currency=payment.currency,
        status=payment.status,
        lines=lines,
        subtotal_minor=payment.amount_minor,
        tax_minor=payment.withholding_amount_minor,
        total_minor=payment.amount_minor - payment.withholding_amount_minor,
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{payment.number}.pdf"'},
    )


@router.post(
    "/payments/{payment_id}/post",
    response_model=PaymentOut,
    summary="Post a draft supplier payment",
)
async def post_payment(
    payment_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("cashbank.payment.post"))],
) -> PaymentOut:
    """Post a draft payment and settle its allocated bills."""
    result = await service.post_payment(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        payment_id=payment_id,
    )
    return _payment_body(result)
