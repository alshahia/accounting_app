"""CSV import for master data (phase 1): customers, suppliers and items.

The import is all-or-nothing: every row is validated first, then all rows are
inserted in one transaction. A bad row, a duplicate code inside the file or a
code that already exists in the company aborts the whole import with a
problem+json error naming the row, so a half-imported list cannot happen.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import Customer, Item, Supplier
from app.modules.identity.audit import record_audit
from app.shared.errors import AppError
from app.shared.unit_of_work import UnitOfWork

MAX_IMPORT_ROWS = 5000


@dataclass(frozen=True)
class ImportResult:
    """How many records an import created."""

    entity: str
    created: int


def _bad_row(row_number: int, detail: str) -> AppError:
    return AppError(
        code="import_invalid_row",
        title="Unprocessable entity",
        status_code=422,
        detail=f"Row {row_number}: {detail}",
    )


def parse_csv(content: bytes, *, required: tuple[str, ...]) -> list[dict[str, str]]:
    """Decode UTF-8 CSV, require the header fields and return stripped rows."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise AppError(
            code="import_encoding_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="The file is not valid UTF-8 text.",
        ) from error
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise _bad_row(1, "the header row is missing.")
    header = [name.strip() for name in reader.fieldnames]
    missing = [name for name in required if name not in header]
    if missing:
        raise _bad_row(1, f"missing required columns: {', '.join(missing)}.")
    rows: list[dict[str, str]] = []
    for index, raw in enumerate(reader, start=2):
        if index - 1 > MAX_IMPORT_ROWS:
            raise _bad_row(index - 1, f"the file has more than {MAX_IMPORT_ROWS} rows.")
        row = {
            key.strip(): (raw.get(key) or "").strip() for key in header if raw.get(key) is not None
        }
        if not any(row.values()):
            continue
        rows.append(row)
    if not rows:
        raise _bad_row(2, "the file has no data rows.")
    return rows


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float):
        return format(Decimal(str(value)).normalize(), "f")
    return str(value).strip()


def parse_xlsx(content: bytes, *, required: tuple[str, ...]) -> list[dict[str, str]]:
    """Read the first worksheet of an Excel workbook as string rows."""
    from openpyxl import load_workbook  # imported lazily so CSV-only callers stay light

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as error:
        raise AppError(
            code="import_file_invalid",
            title="Unprocessable entity",
            status_code=422,
            detail="The file is not a readable Excel workbook.",
        ) from error
    try:
        sheet = workbook.worksheets[0]
        rows = sheet.iter_rows(values_only=True)
        header_row = next(rows, None)
        if header_row is None:
            raise _bad_row(1, "the header row is missing.")
        header = [_stringify(name) for name in header_row]
        missing = [name for name in required if name not in header]
        if missing:
            raise _bad_row(1, f"missing required columns: {', '.join(missing)}.")
        parsed: list[dict[str, str]] = []
        for index, values in enumerate(rows, start=2):
            if index - 1 > MAX_IMPORT_ROWS:
                raise _bad_row(index - 1, f"the file has more than {MAX_IMPORT_ROWS} rows.")
            row = {
                header[position]: _stringify(values[position])
                for position in range(min(len(header), len(values)))
            }
            if not any(row.values()):
                continue
            parsed.append(row)
        if not parsed:
            raise _bad_row(2, "the file has no data rows.")
        return parsed
    finally:
        workbook.close()


def _require(row: dict[str, str], field: str, row_number: int) -> str:
    value = row.get(field, "")
    if not value:
        raise _bad_row(row_number, f"'{field}' is required.")
    return value


def _optional(row: dict[str, str], field: str) -> str | None:
    value = row.get(field, "")
    return value or None


async def import_customers(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    rows: list[dict[str, str]],
) -> ImportResult:
    """Insert every customer row after validating codes and required fields."""

    async def operation(session: AsyncSession) -> ImportResult:
        seen: set[str] = set()
        records: list[Customer] = []
        for offset, row in enumerate(rows, start=2):
            code = _require(row, "code", offset)
            name_ar = _require(row, "name_ar", offset)
            if code in seen:
                raise _bad_row(offset, f"duplicate code '{code}' in the file.")
            seen.add(code)
            records.append(
                Customer(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=code,
                    name_ar=name_ar,
                    name_en=_optional(row, "name_en"),
                    tax_number=_optional(row, "tax_number"),
                    created_by=actor_user_id,
                    updated_by=actor_user_id,
                )
            )
        existing = set(
            (
                await session.execute(
                    select(Customer.code).where(
                        Customer.tenant_id == tenant_id,
                        Customer.company_id == company_id,
                        Customer.code.in_(seen),
                    )
                )
            )
            .scalars()
            .all()
        )
        if existing:
            raise AppError(
                code="import_duplicate_code",
                title="Conflict",
                status_code=409,
                detail=f"These customer codes already exist: {', '.join(sorted(existing))}.",
            )
        session.add_all(records)
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="catalog.customers.imported",
            entity_type="customer",
            detail={"created": len(records)},
        )
        return ImportResult(entity="customer", created=len(records))

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError:
        raise AppError(
            code="import_conflict",
            title="Conflict",
            status_code=409,
            detail="A row conflicts with existing data; nothing was imported.",
        ) from None


async def import_suppliers(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    rows: list[dict[str, str]],
) -> ImportResult:
    """Insert every supplier row after validating codes and required fields."""

    async def operation(session: AsyncSession) -> ImportResult:
        seen: set[str] = set()
        records: list[Supplier] = []
        for offset, row in enumerate(rows, start=2):
            code = _require(row, "code", offset)
            name_ar = _require(row, "name_ar", offset)
            if code in seen:
                raise _bad_row(offset, f"duplicate code '{code}' in the file.")
            seen.add(code)
            records.append(
                Supplier(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=code,
                    name_ar=name_ar,
                    name_en=_optional(row, "name_en"),
                    tax_number=_optional(row, "tax_number"),
                    created_by=actor_user_id,
                    updated_by=actor_user_id,
                )
            )
        existing = set(
            (
                await session.execute(
                    select(Supplier.code).where(
                        Supplier.tenant_id == tenant_id,
                        Supplier.company_id == company_id,
                        Supplier.code.in_(seen),
                    )
                )
            )
            .scalars()
            .all()
        )
        if existing:
            raise AppError(
                code="import_duplicate_code",
                title="Conflict",
                status_code=409,
                detail=f"These supplier codes already exist: {', '.join(sorted(existing))}.",
            )
        session.add_all(records)
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="catalog.suppliers.imported",
            entity_type="supplier",
            detail={"created": len(records)},
        )
        return ImportResult(entity="supplier", created=len(records))

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError:
        raise AppError(
            code="import_conflict",
            title="Conflict",
            status_code=409,
            detail="A row conflicts with existing data; nothing was imported.",
        ) from None


def _optional_decimal(row: dict[str, str], field: str, row_number: int) -> Decimal:
    value = row.get(field, "")
    if not value:
        return Decimal("0")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise _bad_row(row_number, f"'{field}' is not a number.") from error
    if number < 0:
        raise _bad_row(row_number, f"'{field}' must not be negative.")
    return number


async def import_items(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    rows: list[dict[str, str]],
) -> ImportResult:
    """Insert every item row; quantity and value default to zero."""

    async def operation(session: AsyncSession) -> ImportResult:
        seen: set[str] = set()
        records: list[Item] = []
        for offset, row in enumerate(rows, start=2):
            code = _require(row, "code", offset)
            name_ar = _require(row, "name_ar", offset)
            if code in seen:
                raise _bad_row(offset, f"duplicate code '{code}' in the file.")
            seen.add(code)
            quantity = _optional_decimal(row, "quantity_on_hand", offset)
            value = _optional_decimal(row, "stock_value_minor", offset)
            if value != value.to_integral_value():
                raise _bad_row(offset, "'stock_value_minor' must be an integer.")
            records.append(
                Item(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    code=code,
                    name_ar=name_ar,
                    name_en=_optional(row, "name_en"),
                    unit_of_measure=_optional(row, "unit_of_measure") or "piece",
                    quantity_on_hand=quantity,
                    stock_value_minor=int(value),
                    created_by=actor_user_id,
                    updated_by=actor_user_id,
                )
            )
        existing = set(
            (
                await session.execute(
                    select(Item.code).where(
                        Item.tenant_id == tenant_id,
                        Item.company_id == company_id,
                        Item.code.in_(seen),
                    )
                )
            )
            .scalars()
            .all()
        )
        if existing:
            raise AppError(
                code="import_duplicate_code",
                title="Conflict",
                status_code=409,
                detail=f"These item codes already exist: {', '.join(sorted(existing))}.",
            )
        session.add_all(records)
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="catalog.items.imported",
            entity_type="item",
            detail={"created": len(records)},
        )
        return ImportResult(entity="item", created=len(records))

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError:
        raise AppError(
            code="import_conflict",
            title="Conflict",
            status_code=409,
            detail="A row conflicts with existing data; nothing was imported.",
        ) from None
