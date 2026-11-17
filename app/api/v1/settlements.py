"""Cash transfer, cheque and bank reconciliation endpoints (phase 1).

Cheques are separate from receipts and payments because their value moves over
several days: a received cheque is held under collection until deposited and
then cleared or bounced; an issued cheque stays a payable until it clears. The
statements endpoints import bank lines and match them to already-posted
documents without editing those documents.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import PrincipalDep, TenantSessionDep, UnitOfWorkDep, require_company_permission
from app.modules.cashbank import service
from app.modules.cashbank.service import (
    AllocationInput,
    BankStatementLineInput,
    CashTransferInput,
    ChequeInput,
)
from app.modules.identity.service import CompanyAccess
from app.shared.errors import AppError

router = APIRouter(prefix="/cashbank", tags=["cashbank"])


class TransferCreate(BaseModel):
    """A movement between two cash or bank accounts, posted immediately."""

    transfer_date: date
    from_account_id: UUID
    to_account_id: UUID
    amount_minor: Annotated[int, Field(gt=0)]
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] = "USD"
    exchange_rate: Annotated[Decimal, Field(gt=0)] = Decimal("1")
    memo: Annotated[str | None, Field(max_length=500)] = None
    number: Annotated[str | None, Field(min_length=1, max_length=40)] = None


class TransferOut(BaseModel):
    """A posted cash or bank transfer."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    number: str
    transfer_date: date
    from_account_id: UUID
    to_account_id: UUID
    currency: str
    exchange_rate: Decimal
    amount_minor: int
    base_amount_minor: int
    status: str
    memo: str | None
    journal_entry_id: UUID | None
    posted_at: datetime | None


class TransferDetailOut(TransferOut):
    """A posted transfer with the names of both cash or bank accounts."""

    from_account_name_ar: str
    from_account_name_en: str | None
    to_account_name_ar: str
    to_account_name_en: str | None


class ChequeCreate(BaseModel):
    """A received or issued cheque with optional invoice or bill allocations."""

    direction: Annotated[str, Field(pattern=r"^(received|issued)$")]
    number: Annotated[str, Field(min_length=1, max_length=40)]
    cash_bank_account_id: UUID
    issue_date: date
    amount_minor: Annotated[int, Field(gt=0)]
    customer_id: UUID | None = None
    supplier_id: UUID | None = None
    due_date: date | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] = "USD"
    exchange_rate: Annotated[Decimal, Field(gt=0)] = Decimal("1")
    bank_name: Annotated[str | None, Field(max_length=200)] = None
    party_name: Annotated[str | None, Field(max_length=200)] = None
    memo: Annotated[str | None, Field(max_length=500)] = None
    post: bool = False
    allocations: Annotated[list[service.AllocationInput], Field(max_length=200)] = []


class ChequeAllocationOut(BaseModel):
    """One stored cheque allocation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    invoice_id: UUID | None
    bill_id: UUID | None
    amount_minor: int
    base_amount_minor: int
    settlement_base_minor: int
    is_reversed: bool


class ChequeEventOut(BaseModel):
    """One stored cheque lifecycle event."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    event_date: date
    journal_entry_id: UUID | None
    notes: str | None


class ChequeOut(BaseModel):
    """A cheque with its allocations and lifecycle events."""

    id: UUID
    direction: str
    number: str
    customer_id: UUID | None
    supplier_id: UUID | None
    cash_bank_account_id: UUID
    bank_name: str | None
    party_name: str | None
    issue_date: date
    due_date: date
    currency: str
    exchange_rate: Decimal
    amount_minor: int
    base_amount_minor: int
    unallocated_minor: int
    status: str
    memo: str | None
    allocations: list[ChequeAllocationOut]
    events: list[ChequeEventOut]


class ChequeEventRequest(BaseModel):
    """A lifecycle event date and optional note."""

    event_date: date
    notes: Annotated[str | None, Field(max_length=500)] = None


class BankStatementLineIn(BaseModel):
    """One bank statement line; positive means money into the bank."""

    statement_date: date
    description: Annotated[str, Field(min_length=1, max_length=500)]
    amount_minor: int
    reference: Annotated[str | None, Field(max_length=100)] = None


class BankStatementImport(BaseModel):
    """A batch of statement lines for one cash or bank account."""

    cash_bank_account_id: UUID
    currency: Annotated[str, Field(pattern=r"^[A-Za-z]{3}$")] = "USD"
    lines: Annotated[list[BankStatementLineIn], Field(min_length=1, max_length=5000)]


class BankStatementLineOut(BaseModel):
    """A stored bank statement line."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    cash_bank_account_id: UUID
    statement_date: date
    description: str
    reference: str | None
    amount_minor: int
    currency: str
    status: str
    matched_source_type: str | None
    matched_source_id: UUID | None
    matched_at: datetime | None


class BankStatementMatch(BaseModel):
    """The posted document a statement line corresponds to."""

    source_type: Annotated[
        str, Field(pattern=r"^(receipt|supplier_payment|cheque_event|cash_transfer)$")
    ]
    source_id: UUID


def _cheque_body(result: service.ChequeResult) -> ChequeOut:
    cheque = result.cheque
    return ChequeOut(
        id=cheque.id,
        direction=cheque.direction,
        number=cheque.number,
        customer_id=cheque.customer_id,
        supplier_id=cheque.supplier_id,
        cash_bank_account_id=cheque.cash_bank_account_id,
        bank_name=cheque.bank_name,
        party_name=cheque.party_name,
        issue_date=cheque.issue_date,
        due_date=cheque.due_date,
        currency=cheque.currency,
        exchange_rate=cheque.exchange_rate,
        amount_minor=cheque.amount_minor,
        base_amount_minor=cheque.base_amount_minor,
        unallocated_minor=cheque.unallocated_minor,
        status=cheque.status,
        memo=cheque.memo,
        allocations=[
            ChequeAllocationOut.model_validate(allocation) for allocation in result.allocations
        ],
        events=[ChequeEventOut.model_validate(event) for event in result.events],
    )


def _require(permission: str, company: CompanyAccess) -> None:
    if permission not in company.permissions:
        raise AppError(
            code="permission_denied",
            title="Forbidden",
            status_code=403,
            detail=f"The permission '{permission}' is required in the selected company.",
        )


@router.post(
    "/transfers",
    response_model=TransferOut,
    status_code=status.HTTP_201_CREATED,
    summary="Post a cash or bank transfer",
)
async def create_transfer(
    payload: TransferCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[
        CompanyAccess, Depends(require_company_permission("cashbank.transfer.post"))
    ],
) -> TransferOut:
    """Move money between two cash or bank accounts and post the entry."""
    _require("cashbank.transfer.create", company)
    result = await service.create_cash_transfer(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=CashTransferInput(
            company_id=company.id,
            transfer_date=payload.transfer_date,
            from_account_id=payload.from_account_id,
            to_account_id=payload.to_account_id,
            amount_minor=payload.amount_minor,
            currency=payload.currency.upper(),
            exchange_rate=payload.exchange_rate,
            memo=payload.memo,
            number=payload.number,
        ),
    )
    return TransferOut.model_validate(result.transfer)


@router.get("/transfers", response_model=list[TransferOut], summary="List cash and bank transfers")
async def read_transfers(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> list[TransferOut]:
    """List transfers newest first."""
    transfers = await service.list_cash_transfers(
        session, tenant_id=principal.tenant_id, company_id=company.id
    )
    return [TransferOut.model_validate(transfer) for transfer in transfers]


@router.get(
    "/transfers/{transfer_id}",
    response_model=TransferDetailOut,
    summary="Read one cash or bank transfer",
)
async def read_transfer(
    transfer_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> TransferDetailOut:
    """Return one transfer with its account names for report drill-through."""
    transfer, source, destination = await service.get_cash_transfer(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        transfer_id=transfer_id,
    )
    return TransferDetailOut(
        **TransferOut.model_validate(transfer).model_dump(),
        from_account_name_ar=source.name_ar,
        from_account_name_en=source.name_en,
        to_account_name_ar=destination.name_ar,
        to_account_name_en=destination.name_en,
    )


@router.post(
    "/cheques",
    response_model=ChequeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a received or issued cheque",
)
async def create_cheque(
    payload: ChequeCreate,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[
        CompanyAccess, Depends(require_company_permission("cashbank.cheque.create"))
    ],
) -> ChequeOut:
    """Create a cheque, posting its first lifecycle entry when allowed."""
    if payload.post:
        _require("cashbank.cheque.post", company)
    result = await service.create_cheque(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        request=ChequeInput(
            company_id=company.id,
            direction=payload.direction,
            number=payload.number,
            cash_bank_account_id=payload.cash_bank_account_id,
            issue_date=payload.issue_date,
            amount_minor=payload.amount_minor,
            customer_id=payload.customer_id,
            supplier_id=payload.supplier_id,
            due_date=payload.due_date,
            currency=payload.currency.upper(),
            exchange_rate=payload.exchange_rate,
            bank_name=payload.bank_name,
            party_name=payload.party_name,
            memo=payload.memo,
            allocations=tuple(
                AllocationInput(document_id=row.document_id, amount_minor=row.amount_minor)
                for row in payload.allocations
            ),
            post=payload.post,
        ),
    )
    return _cheque_body(result)


@router.get("/cheques", response_model=list[ChequeOut], summary="List cheques")
async def read_cheques(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    direction: str | None = None,
) -> list[ChequeOut]:
    """List cheques newest first, optionally filtered by direction."""
    cheques = await service.list_cheques(
        session, tenant_id=principal.tenant_id, company_id=company.id, direction=direction
    )
    bodies: list[ChequeOut] = []
    for cheque in cheques:
        result = await service.get_cheque(
            session,
            tenant_id=principal.tenant_id,
            company_id=company.id,
            cheque_id=cheque.id,
        )
        bodies.append(_cheque_body(result))
    return bodies


@router.get("/cheques/{cheque_id}", response_model=ChequeOut, summary="Read a cheque")
async def read_cheque(
    cheque_id: UUID,
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
) -> ChequeOut:
    """Return one cheque with its allocations and events."""
    result = await service.get_cheque(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        cheque_id=cheque_id,
    )
    return _cheque_body(result)


@router.post("/cheques/{cheque_id}/deposit", response_model=ChequeOut, summary="Deposit a cheque")
async def deposit_cheque(
    cheque_id: UUID,
    payload: ChequeEventRequest,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("cashbank.cheque.post"))],
) -> ChequeOut:
    """Move a received cheque from hand to the bank."""
    result = await service.deposit_cheque(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        cheque_id=cheque_id,
        event_date=payload.event_date,
        notes=payload.notes,
    )
    return _cheque_body(result)


@router.post("/cheques/{cheque_id}/clear", response_model=ChequeOut, summary="Clear a cheque")
async def clear_cheque(
    cheque_id: UUID,
    payload: ChequeEventRequest,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("cashbank.cheque.post"))],
) -> ChequeOut:
    """Mark a deposited or issued cheque as cleared by the bank."""
    result = await service.clear_cheque(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        cheque_id=cheque_id,
        event_date=payload.event_date,
        notes=payload.notes,
    )
    return _cheque_body(result)


@router.post("/cheques/{cheque_id}/bounce", response_model=ChequeOut, summary="Bounce a cheque")
async def bounce_cheque(
    cheque_id: UUID,
    payload: ChequeEventRequest,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("cashbank.cheque.post"))],
) -> ChequeOut:
    """Record a bounced cheque and restore the receivable or payable."""
    result = await service.bounce_cheque(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        cheque_id=cheque_id,
        event_date=payload.event_date,
        notes=payload.notes,
    )
    return _cheque_body(result)


@router.post("/cheques/{cheque_id}/cancel", response_model=ChequeOut, summary="Cancel a cheque")
async def cancel_cheque(
    cheque_id: UUID,
    payload: ChequeEventRequest,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("cashbank.cheque.post"))],
) -> ChequeOut:
    """Cancel a cheque that has not been deposited or cleared."""
    result = await service.cancel_cheque(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        cheque_id=cheque_id,
        event_date=payload.event_date,
        notes=payload.notes,
    )
    return _cheque_body(result)


@router.post(
    "/bank-statements",
    response_model=list[BankStatementLineOut],
    status_code=status.HTTP_201_CREATED,
    summary="Import bank statement lines",
)
async def import_bank_statement(
    payload: BankStatementImport,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("cashbank.reconcile"))],
) -> list[BankStatementLineOut]:
    """Import unmatched statement lines, all or nothing."""
    lines = await service.import_bank_statement_lines(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        cash_bank_account_id=payload.cash_bank_account_id,
        currency=payload.currency.upper(),
        lines=tuple(
            BankStatementLineInput(
                statement_date=row.statement_date,
                description=row.description,
                amount_minor=row.amount_minor,
                reference=row.reference,
            )
            for row in payload.lines
        ),
    )
    return [BankStatementLineOut.model_validate(line) for line in lines]


@router.get(
    "/bank-statements",
    response_model=list[BankStatementLineOut],
    summary="List bank statement lines",
)
async def read_bank_statement_lines(
    session: TenantSessionDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("report.view"))],
    cash_bank_account_id: UUID | None = None,
    status_filter: str | None = None,
) -> list[BankStatementLineOut]:
    """List statement lines newest first, optionally filtered."""
    lines = await service.list_bank_statement_lines(
        session,
        tenant_id=principal.tenant_id,
        company_id=company.id,
        cash_bank_account_id=cash_bank_account_id,
        status=status_filter,
    )
    return [BankStatementLineOut.model_validate(line) for line in lines]


@router.post(
    "/bank-statements/{line_id}/match",
    response_model=BankStatementLineOut,
    summary="Match a bank statement line",
)
async def match_bank_statement_line(
    line_id: UUID,
    payload: BankStatementMatch,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("cashbank.reconcile"))],
) -> BankStatementLineOut:
    """Match a statement line to the posted document that moved the money."""
    line = await service.match_bank_statement_line(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        line_id=line_id,
        source_type=payload.source_type,
        source_id=payload.source_id,
    )
    return BankStatementLineOut.model_validate(line)


@router.post(
    "/bank-statements/{line_id}/unmatch",
    response_model=BankStatementLineOut,
    summary="Unmatch a bank statement line",
)
async def unmatch_bank_statement_line(
    line_id: UUID,
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("cashbank.reconcile"))],
) -> BankStatementLineOut:
    """Undo a match without touching the posted documents."""
    line = await service.unmatch_bank_statement_line(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        line_id=line_id,
    )
    return BankStatementLineOut.model_validate(line)
