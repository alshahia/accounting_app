"""CSV import endpoints for master data (phase 1).

The browser uploads a UTF-8 CSV file; the server validates every row and imports
all rows in one transaction. A bad row or duplicate code aborts the import.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel

from app.api.deps import PrincipalDep, UnitOfWorkDep, require_company_permission
from app.modules.catalog import imports
from app.modules.identity.service import CompanyAccess
from app.modules.ledger import opening_balances
from app.shared.errors import AppError

router = APIRouter(prefix="/imports", tags=["imports"])

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class ImportOut(BaseModel):
    """The number of records one import created."""

    entity: str
    created: int


class OpeningBalanceLineOut(BaseModel):
    """One account row in an opening-balance validation report."""

    account_code: str
    account_name_ar: str
    account_name_en: str | None
    debit_minor: int
    credit_minor: int
    description: str | None


class OpeningBalanceOut(BaseModel):
    """The validation report of an opening-balance import."""

    valid: bool
    total_debit_minor: int
    total_credit_minor: int
    line_count: int
    lines: list[OpeningBalanceLineOut]
    entry_id: UUID | None
    entry_number: str | None


_UPLOAD_CHUNK_BYTES = 64 * 1024


async def _read_upload(file: UploadFile) -> bytes:
    """Read the upload in chunks and refuse as soon as the limit is crossed.

    Reading the whole body first would let a client make the process hold an
    arbitrarily large file in memory before the size check runs.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise AppError(
                code="import_file_too_large",
                title="Unprocessable entity",
                status_code=422,
                detail="The CSV file is larger than 5 MB.",
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise AppError(
            code="import_file_empty",
            title="Unprocessable entity",
            status_code=422,
            detail="The uploaded file is empty.",
        )
    return content


def _tabular(
    file_name: str | None, content: bytes, *, required: tuple[str, ...]
) -> list[dict[str, str]]:
    """Parse a CSV or Excel upload into the same header-keyed rows."""
    suffix = (file_name or "").lower().rsplit(".", 1)[-1]
    if suffix in {"xlsx", "xlsm"}:
        return imports.parse_xlsx(content, required=required)
    return imports.parse_csv(content, required=required)


@router.post("/customers", response_model=ImportOut, summary="Import customers from CSV or Excel")
async def import_customers(
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("settings.manage"))],
    file: Annotated[UploadFile, File(description="UTF-8 CSV with code,name_ar,name_en,tax_number")],
) -> ImportOut:
    """Import customers from a UTF-8 CSV file, all or nothing."""
    rows = _tabular(file.filename, await _read_upload(file), required=("code", "name_ar"))
    result = await imports.import_customers(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        rows=rows,
    )
    return ImportOut(entity=result.entity, created=result.created)


@router.post("/suppliers", response_model=ImportOut, summary="Import suppliers from CSV or Excel")
async def import_suppliers(
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("settings.manage"))],
    file: Annotated[UploadFile, File(description="UTF-8 CSV with code,name_ar,name_en,tax_number")],
) -> ImportOut:
    """Import suppliers from a UTF-8 CSV file, all or nothing."""
    rows = _tabular(file.filename, await _read_upload(file), required=("code", "name_ar"))
    result = await imports.import_suppliers(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        rows=rows,
    )
    return ImportOut(entity=result.entity, created=result.created)


@router.post(
    "/opening-balances",
    response_model=OpeningBalanceOut,
    summary="Validate or import opening balances from CSV or Excel",
)
async def import_opening_balances(
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("ledger.journal.create"))],
    period_id: Annotated[UUID, Form(description="Accounting period receiving the entry")],
    entry_date: Annotated[date, Form(description="Opening entry date inside the period")],
    file: Annotated[
        UploadFile,
        File(description="UTF-8 CSV with account_code,debit,credit,description"),
    ],
    post: Annotated[bool, Form(description="Post the entry instead of keeping it draft")] = True,
    dry_run: Annotated[bool, Form(description="Return the report without writing")] = False,
) -> OpeningBalanceOut:
    """Validate and optionally post one balanced opening journal entry."""
    if post and not dry_run and "ledger.journal.post" not in company.permissions:
        raise AppError(
            code="permission_denied",
            title="Forbidden",
            status_code=403,
            detail="The permission 'ledger.journal.post' is required in the selected company.",
        )
    rows = _tabular(file.filename, await _read_upload(file), required=("account_code",))
    report = await opening_balances.import_opening_balances(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        period_id=period_id,
        entry_date=entry_date,
        rows=rows,
        post=post,
        dry_run=dry_run,
    )
    return OpeningBalanceOut(
        valid=report.valid,
        total_debit_minor=report.total_debit_minor,
        total_credit_minor=report.total_credit_minor,
        line_count=report.line_count,
        lines=[
            OpeningBalanceLineOut(
                account_code=line.account_code,
                account_name_ar=line.account_name_ar,
                account_name_en=line.account_name_en,
                debit_minor=line.debit_minor,
                credit_minor=line.credit_minor,
                description=line.description,
            )
            for line in report.lines
        ],
        entry_id=report.entry_id,
        entry_number=report.entry_number,
    )


@router.post("/items", response_model=ImportOut, summary="Import items from CSV")
async def import_items(
    unit_of_work: UnitOfWorkDep,
    principal: PrincipalDep,
    company: Annotated[CompanyAccess, Depends(require_company_permission("inventory.manage"))],
    file: Annotated[
        UploadFile,
        File(description="UTF-8 CSV with code,name_ar,name_en,unit_of_measure,quantity_on_hand"),
    ],
) -> ImportOut:
    """Import stock items from a UTF-8 CSV file, all or nothing."""
    rows = _tabular(file.filename, await _read_upload(file), required=("code", "name_ar"))
    result = await imports.import_items(
        unit_of_work,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        company_id=company.id,
        rows=rows,
    )
    return ImportOut(entity=result.entity, created=result.created)
