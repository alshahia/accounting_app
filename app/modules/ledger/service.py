"""Ledger posting service (phase 0, day 6).

One command = one transaction, owned by :class:`UnitOfWork`. The service
validates what it can for a friendly problem+json error and lets PostgreSQL
enforce the invariants again (deferred debit = credit, immutability, open
period, postable accounts). Document numbers come from a locked per-company
sequence; an optional idempotency key maps a retried request to the entry its
first call created; a reversal is a new linked posting that leaves the original
entry untouched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.audit import record_audit
from app.modules.ledger.models import (
    Account,
    AccountingPeriod,
    FiscalYear,
    IdempotencyRecord,
    JournalEntry,
    JournalLine,
)
from app.modules.ledger.seeding import seed_company_chart
from app.shared.context import get_request_id
from app.shared.errors import AppError
from app.shared.unit_of_work import UnitOfWork, is_retryable_db_error

ENTRY_STATUS_DRAFT = "draft"
ENTRY_STATUS_POSTED = "posted"
SEQUENCE_SCOPE = "journal_entry"
MAX_IDEMPOTENCY_KEY_LENGTH = 200


@dataclass(frozen=True)
class JournalLineInput:
    """One side of a journal line, in integer minor currency units."""

    account_id: UUID
    debit_minor: int = 0
    credit_minor: int = 0
    description: str | None = None


@dataclass(frozen=True)
class JournalEntryInput:
    """A manual journal entry to create, as a draft or directly posted."""

    company_id: UUID
    period_id: UUID
    entry_date: date
    lines: tuple[JournalLineInput, ...]
    memo: str | None = None
    post: bool = False
    source_type: str | None = None
    source_id: UUID | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ReversalInput:
    """A request to reverse one posted journal entry."""

    company_id: UUID
    entry_id: UUID
    entry_date: date | None = None
    memo: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class EntryResult:
    """A journal entry and its lines, plus whether it was an idempotent replay."""

    entry: JournalEntry
    lines: tuple[JournalLine, ...]
    replayed: bool


@dataclass(frozen=True)
class _Reservation:
    record_id: UUID
    existing_entry_id: UUID | None


def _now() -> datetime:
    return datetime.now(UTC)


def _request_id() -> str | None:
    request_id = get_request_id()
    return None if request_id == "-" else request_id


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _entry_fingerprint(request: JournalEntryInput) -> str:
    return _fingerprint(
        {
            "kind": "journal_entry",
            "company_id": str(request.company_id),
            "period_id": str(request.period_id),
            "entry_date": request.entry_date.isoformat(),
            "memo": request.memo,
            "post": request.post,
            "source_type": request.source_type,
            "source_id": str(request.source_id) if request.source_id else None,
            "lines": [
                {
                    "account_id": str(line.account_id),
                    "debit_minor": line.debit_minor,
                    "credit_minor": line.credit_minor,
                    "description": line.description,
                }
                for line in request.lines
            ],
        }
    )


def _reversal_fingerprint(request: ReversalInput) -> str:
    return _fingerprint(
        {
            "kind": "journal_reversal",
            "company_id": str(request.company_id),
            "entry_id": str(request.entry_id),
            "entry_date": request.entry_date.isoformat() if request.entry_date else None,
            "memo": request.memo,
        }
    )


def _bad_request(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Unprocessable entity", status_code=422, detail=detail)


def _conflict(code: str, detail: str) -> AppError:
    return AppError(code=code, title="Conflict", status_code=409, detail=detail)


def _validate_lines(lines: tuple[JournalLineInput, ...]) -> tuple[int, int]:
    """Check at least two lines, one positive side per line and a zero balance."""
    if len(lines) < 2:
        raise _bad_request("journal_lines_invalid", "A journal entry needs at least two lines.")
    total_debit = 0
    total_credit = 0
    for index, line in enumerate(lines, start=1):
        if line.debit_minor < 0 or line.credit_minor < 0:
            raise _bad_request(
                "journal_lines_invalid",
                f"Line {index} has a negative amount; amounts are integer minor units.",
            )
        if (line.debit_minor > 0) == (line.credit_minor > 0):
            raise _bad_request(
                "journal_lines_invalid",
                f"Line {index} must have exactly one positive debit or credit amount.",
            )
        total_debit += line.debit_minor
        total_credit += line.credit_minor
    if total_debit != total_credit:
        raise _bad_request(
            "journal_unbalanced",
            f"Journal entry is unbalanced: debit {total_debit} <> credit {total_credit}.",
        )
    return total_debit, total_credit


async def _load_period(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    period_id: UUID,
    for_update: bool = False,
) -> AccountingPeriod:
    statement = select(AccountingPeriod).where(
        AccountingPeriod.tenant_id == tenant_id,
        AccountingPeriod.company_id == company_id,
        AccountingPeriod.id == period_id,
    )
    if for_update:
        # Close/reopen race: the second caller waits, then sees the new status
        # and answers 409 instead of both succeeding.
        statement = statement.with_for_update()
    period = (await session.execute(statement)).scalar_one_or_none()
    if period is None:
        raise AppError(
            code="period_not_found",
            title="Not found",
            status_code=404,
            detail="The accounting period does not exist in the selected company.",
        )
    return period


def _ensure_period_accepts(period: AccountingPeriod, entry_date: date, *, posting: bool) -> None:
    if entry_date < period.start_date or entry_date > period.end_date:
        raise _bad_request(
            "entry_date_outside_period",
            f"Entry date {entry_date.isoformat()} is outside the selected period.",
        )
    if posting and period.status != "open":
        raise _conflict("period_closed", "The selected accounting period is closed.")


async def _load_accounts(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    lines: tuple[JournalLineInput, ...],
) -> dict[UUID, Account]:
    ids = {line.account_id for line in lines}
    accounts = {
        account.id: account
        for account in (
            await session.execute(
                select(Account).where(
                    Account.tenant_id == tenant_id,
                    Account.company_id == company_id,
                    Account.id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    }
    missing = ids - set(accounts)
    if missing:
        raise AppError(
            code="account_not_found",
            title="Not found",
            status_code=404,
            detail="One or more accounts do not exist in the selected company.",
        )
    for account in accounts.values():
        if not account.is_postable or not account.is_active:
            raise _bad_request(
                "account_not_postable",
                f"Account {account.code} is not an active postable account.",
            )
    return accounts


async def _allocate_number(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    period: AccountingPeriod,
) -> str:
    """Atomically take the next entry number for the company and period year."""
    period_key = f"{period.start_date.year:04d}"
    next_value = await session.scalar(
        text(
            """
            INSERT INTO number_sequence (tenant_id, company_id, scope, period_key, next_value)
            VALUES (:tenant_id, :company_id, :scope, :period_key, 1)
            ON CONFLICT ON CONSTRAINT uq_number_sequence_tenant_company_scope_period
            DO UPDATE SET next_value = number_sequence.next_value + 1, updated_at = now()
            RETURNING next_value
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "company_id": str(company_id),
            "scope": SEQUENCE_SCOPE,
            "period_key": period_key,
        },
    )
    if next_value is None:
        raise _conflict("entry_number_conflict", "Could not allocate a journal entry number.")
    return f"JE-{period_key}-{int(next_value):06d}"


async def _reserve_idempotency(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    key: str,
    fingerprint: str,
) -> _Reservation:
    if not 1 <= len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        raise _bad_request(
            "idempotency_key_invalid",
            f"The Idempotency-Key header must be 1-{MAX_IDEMPOTENCY_KEY_LENGTH} characters.",
        )
    inserted_id = await session.scalar(
        text(
            """
            INSERT INTO idempotency_record
                (tenant_id, company_id, key, request_fingerprint, status)
            VALUES (:tenant_id, :company_id, :key, :fingerprint, 'in_progress')
            ON CONFLICT ON CONSTRAINT uq_idempotency_record_tenant_company_key DO NOTHING
            RETURNING id
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "company_id": str(company_id),
            "key": key,
            "fingerprint": fingerprint,
        },
    )
    if inserted_id is not None:
        return _Reservation(record_id=inserted_id, existing_entry_id=None)
    record = (
        await session.execute(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.tenant_id == tenant_id,
                IdempotencyRecord.company_id == company_id,
                IdempotencyRecord.key == key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        raise _conflict(
            "idempotency_in_progress",
            "A request with this idempotency key is still in progress; retry shortly.",
        )
    if record.request_fingerprint != fingerprint:
        raise _conflict(
            "idempotency_conflict",
            "This idempotency key was already used for a different request.",
        )
    if record.entry_id is None:
        raise _conflict(
            "idempotency_in_progress",
            "A request with this idempotency key is still in progress; retry shortly.",
        )
    return _Reservation(record_id=record.id, existing_entry_id=record.entry_id)


async def _complete_idempotency(
    session: AsyncSession,
    *,
    reservation: _Reservation,
    entry_id: UUID,
) -> None:
    await session.execute(
        text(
            """
            UPDATE idempotency_record
               SET entry_id = :entry_id, status = 'completed', updated_at = now()
             WHERE id = :record_id
            """
        ),
        {"entry_id": str(entry_id), "record_id": str(reservation.record_id)},
    )


async def _load_entry(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    entry_id: UUID,
) -> JournalEntry:
    entry = (
        await session.execute(
            select(JournalEntry).where(
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.company_id == company_id,
                JournalEntry.id == entry_id,
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise AppError(
            code="entry_not_found",
            title="Not found",
            status_code=404,
            detail="The journal entry does not exist in the selected company.",
        )
    return entry


async def _load_lines(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    entry_id: UUID,
) -> tuple[JournalLine, ...]:
    lines = (
        await session.execute(
            select(JournalLine)
            .where(
                JournalLine.tenant_id == tenant_id,
                JournalLine.company_id == company_id,
                JournalLine.entry_id == entry_id,
            )
            .order_by(JournalLine.line_no)
        )
    ).scalars()
    return tuple(lines.all())


async def _load_result(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    entry_id: UUID,
    replayed: bool,
) -> EntryResult:
    entry = await _load_entry(
        session, tenant_id=tenant_id, company_id=company_id, entry_id=entry_id
    )
    lines = await _load_lines(
        session, tenant_id=tenant_id, company_id=company_id, entry_id=entry.id
    )
    return EntryResult(entry=entry, lines=lines, replayed=replayed)


async def load_accounts_by_code(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    codes: tuple[str, ...],
) -> dict[str, Account]:
    """Load a company's postable accounts keyed by chart code (day 7 modules)."""
    accounts = {
        account.code: account
        for account in (
            await session.execute(
                select(Account).where(
                    Account.tenant_id == tenant_id,
                    Account.company_id == company_id,
                    Account.code.in_(codes),
                )
            )
        )
        .scalars()
        .all()
    }
    missing = [code for code in codes if code not in accounts]
    if missing:
        raise _conflict(
            "chart_incomplete",
            f"The company chart is missing account codes: {', '.join(missing)}.",
        )
    for code in codes:
        account = accounts[code]
        if not account.is_postable or not account.is_active:
            raise _bad_request(
                "account_not_postable", f"Account {code} is not an active postable account."
            )
    return accounts


async def post_entry_in_session(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    period_id: UUID,
    entry_date: date,
    memo: str | None,
    lines: tuple[JournalLineInput, ...],
    source_type: str,
    source_id: UUID,
) -> EntryResult:
    """Post a journal entry inside an existing transaction (document services).

    The caller owns the transaction and supplies the period; validation, number
    allocation, lines and audit are the same as :func:`create_journal_entry`.
    """
    total_debit, _ = _validate_lines(lines)
    period = await _load_period(
        session, tenant_id=tenant_id, company_id=company_id, period_id=period_id
    )
    _ensure_period_accepts(period, entry_date, posting=True)
    await _load_accounts(session, tenant_id=tenant_id, company_id=company_id, lines=lines)
    number = await _allocate_number(
        session, tenant_id=tenant_id, company_id=company_id, period=period
    )
    entry = JournalEntry(
        tenant_id=tenant_id,
        company_id=company_id,
        period_id=period.id,
        number=number,
        entry_date=entry_date,
        memo=memo,
        status=ENTRY_STATUS_POSTED,
        source_type=source_type,
        source_id=source_id,
        posted_at=_now(),
        posted_by=actor_user_id,
        created_by=actor_user_id,
        updated_by=actor_user_id,
    )
    session.add(entry)
    await session.flush()
    entry_lines = tuple(
        JournalLine(
            tenant_id=tenant_id,
            company_id=company_id,
            entry_id=entry.id,
            line_no=index,
            account_id=line.account_id,
            debit_minor=line.debit_minor,
            credit_minor=line.credit_minor,
            description=line.description,
        )
        for index, line in enumerate(lines, start=1)
    )
    session.add_all(entry_lines)
    await session.flush()
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="ledger.entry.posted",
        entity_type="journal_entry",
        entity_id=entry.id,
        detail={"number": number, "total_minor": total_debit, "lines": len(entry_lines)},
        request_id=_request_id(),
    )
    return EntryResult(entry=entry, lines=entry_lines, replayed=False)


def _translate_db_error(error: DBAPIError) -> AppError | None:
    """Map a database invariant violation to a client-safe problem+json error."""
    if is_retryable_db_error(error):
        return AppError(
            code="database_busy",
            title="Service unavailable",
            status_code=503,
            detail="The posting conflicted with concurrent work; retry the request.",
        )
    message = str(error.orig).lower()
    if "uq_journal_entry_source" in message:
        return _conflict("source_duplicate", "This source document is already posted.")
    if "uq_journal_entry_reversal" in message:
        return _conflict("entry_already_reversed", "This journal entry is already reversed.")
    if "uq_journal_entry_tenant_company_number" in message:
        return _conflict("entry_number_conflict", "The allocated entry number already exists.")
    if "is closed" in message:
        return _conflict("period_closed", "The selected accounting period is closed.")
    if "outside the selected period" in message:
        return _bad_request("entry_date_outside_period", "The entry date is outside its period.")
    if "not a postable" in message:
        return _bad_request("account_not_postable", "An account is not an active postable account.")
    if "at least two lines" in message:
        return _bad_request("journal_lines_invalid", "A journal entry needs at least two lines.")
    if "unbalanced" in message:
        return _bad_request("journal_unbalanced", "The journal entry is unbalanced.")
    return None


def _raise_translated(error: DBAPIError) -> None:
    translated = _translate_db_error(error)
    if translated is None:
        raise error
    raise translated from error


async def create_journal_entry(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: JournalEntryInput,
) -> EntryResult:
    """Create a draft or posted journal entry in one retryable transaction."""
    fingerprint = _entry_fingerprint(request)

    async def operation(session: AsyncSession) -> EntryResult:
        reservation: _Reservation | None = None
        if request.idempotency_key is not None:
            reservation = await _reserve_idempotency(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                key=request.idempotency_key,
                fingerprint=fingerprint,
            )
            if reservation.existing_entry_id is not None:
                return await _load_result(
                    session,
                    tenant_id=tenant_id,
                    company_id=request.company_id,
                    entry_id=reservation.existing_entry_id,
                    replayed=True,
                )
        total_debit, _ = _validate_lines(request.lines)
        period = await _load_period(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            period_id=request.period_id,
        )
        _ensure_period_accepts(period, request.entry_date, posting=request.post)
        await _load_accounts(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            lines=request.lines,
        )
        number = await _allocate_number(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            period=period,
        )
        status = ENTRY_STATUS_POSTED if request.post else ENTRY_STATUS_DRAFT
        now = _now()
        entry = JournalEntry(
            tenant_id=tenant_id,
            company_id=request.company_id,
            period_id=period.id,
            number=number,
            entry_date=request.entry_date,
            memo=request.memo,
            status=status,
            source_type=request.source_type,
            source_id=request.source_id,
            posted_at=now if request.post else None,
            posted_by=actor_user_id if request.post else None,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(entry)
        await session.flush()
        lines = tuple(
            JournalLine(
                tenant_id=tenant_id,
                company_id=request.company_id,
                entry_id=entry.id,
                line_no=index,
                account_id=line.account_id,
                debit_minor=line.debit_minor,
                credit_minor=line.credit_minor,
                description=line.description,
            )
            for index, line in enumerate(request.lines, start=1)
        )
        session.add_all(lines)
        await session.flush()
        if reservation is not None:
            await _complete_idempotency(session, reservation=reservation, entry_id=entry.id)
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="ledger.entry.posted" if request.post else "ledger.entry.created",
            entity_type="journal_entry",
            entity_id=entry.id,
            detail={"number": number, "total_minor": total_debit, "lines": len(lines)},
            request_id=_request_id(),
        )
        return EntryResult(entry=entry, lines=lines, replayed=False)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        _raise_translated(error)
    except DBAPIError as error:
        _raise_translated(error)
    raise AssertionError("unreachable")


async def update_journal_entry(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    entry_id: UUID,
    request: JournalEntryInput,
) -> EntryResult:
    """Replace a draft entry's header and lines; a posted entry never changes."""
    company_id = request.company_id

    async def operation(session: AsyncSession) -> EntryResult:
        entry = await _load_entry(
            session, tenant_id=tenant_id, company_id=company_id, entry_id=entry_id
        )
        if entry.status != ENTRY_STATUS_DRAFT:
            raise _conflict("entry_not_editable", "Only a draft journal entry can be edited.")
        total_debit, _ = _validate_lines(request.lines)
        period = await _load_period(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            period_id=request.period_id,
        )
        _ensure_period_accepts(period, request.entry_date, posting=False)
        await _load_accounts(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            lines=request.lines,
        )
        entry.period_id = period.id
        entry.entry_date = request.entry_date
        entry.memo = request.memo
        entry.updated_by = actor_user_id
        await session.execute(
            delete(JournalLine).where(
                JournalLine.tenant_id == tenant_id,
                JournalLine.company_id == company_id,
                JournalLine.entry_id == entry.id,
            )
        )
        lines = tuple(
            JournalLine(
                tenant_id=tenant_id,
                company_id=company_id,
                entry_id=entry.id,
                line_no=index,
                account_id=line.account_id,
                debit_minor=line.debit_minor,
                credit_minor=line.credit_minor,
                description=line.description,
            )
            for index, line in enumerate(request.lines, start=1)
        )
        session.add_all(lines)
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="ledger.entry.updated",
            entity_type="journal_entry",
            entity_id=entry.id,
            detail={"number": entry.number, "total_minor": total_debit, "lines": len(lines)},
            request_id=_request_id(),
        )
        return EntryResult(entry=entry, lines=lines, replayed=False)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        _raise_translated(error)
    except DBAPIError as error:
        _raise_translated(error)
    raise AssertionError("unreachable")


async def post_journal_entry(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    entry_id: UUID,
) -> EntryResult:
    """Post an existing draft after re-checking the open period at the database."""

    async def operation(session: AsyncSession) -> EntryResult:
        entry = await _load_entry(
            session, tenant_id=tenant_id, company_id=company_id, entry_id=entry_id
        )
        if entry.status == ENTRY_STATUS_POSTED:
            raise _conflict("entry_already_posted", "This journal entry is already posted.")
        period = await _load_period(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            period_id=entry.period_id,
        )
        _ensure_period_accepts(period, entry.entry_date, posting=True)
        lines = await _load_lines(
            session, tenant_id=tenant_id, company_id=company_id, entry_id=entry.id
        )
        total_debit, _ = _validate_lines(
            tuple(
                JournalLineInput(
                    account_id=line.account_id,
                    debit_minor=line.debit_minor,
                    credit_minor=line.credit_minor,
                    description=line.description,
                )
                for line in lines
            )
        )
        entry.status = ENTRY_STATUS_POSTED
        entry.posted_at = _now()
        entry.posted_by = actor_user_id
        entry.updated_by = actor_user_id
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="ledger.entry.posted",
            entity_type="journal_entry",
            entity_id=entry.id,
            detail={"number": entry.number, "total_minor": total_debit, "lines": len(lines)},
            request_id=_request_id(),
        )
        return EntryResult(entry=entry, lines=lines, replayed=False)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        _raise_translated(error)
    except DBAPIError as error:
        _raise_translated(error)
    raise AssertionError("unreachable")


async def delete_journal_entry(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    entry_id: UUID,
) -> None:
    """Delete a draft entry and its lines; posted entries are protected."""

    async def operation(session: AsyncSession) -> None:
        entry = await _load_entry(
            session, tenant_id=tenant_id, company_id=company_id, entry_id=entry_id
        )
        if entry.status != ENTRY_STATUS_DRAFT:
            raise _conflict("entry_not_editable", "Only a draft journal entry can be deleted.")
        number = entry.number
        await session.execute(
            delete(JournalLine).where(
                JournalLine.tenant_id == tenant_id,
                JournalLine.company_id == company_id,
                JournalLine.entry_id == entry.id,
            )
        )
        await session.delete(entry)
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="ledger.entry.deleted",
            entity_type="journal_entry",
            entity_id=entry.id,
            detail={"number": number},
            request_id=_request_id(),
        )

    try:
        await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        _raise_translated(error)
    except DBAPIError as error:
        _raise_translated(error)


async def reverse_journal_entry(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: ReversalInput,
) -> EntryResult:
    """Post a new entry that reverses a posted entry; the original never changes."""
    fingerprint = _reversal_fingerprint(request)

    async def operation(session: AsyncSession) -> EntryResult:
        reservation: _Reservation | None = None
        if request.idempotency_key is not None:
            reservation = await _reserve_idempotency(
                session,
                tenant_id=tenant_id,
                company_id=request.company_id,
                key=request.idempotency_key,
                fingerprint=fingerprint,
            )
            if reservation.existing_entry_id is not None:
                return await _load_result(
                    session,
                    tenant_id=tenant_id,
                    company_id=request.company_id,
                    entry_id=reservation.existing_entry_id,
                    replayed=True,
                )
        original = await _load_entry(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            entry_id=request.entry_id,
        )
        if original.status != ENTRY_STATUS_POSTED:
            raise _conflict(
                "entry_not_posted",
                "Only a posted journal entry can be reversed.",
            )
        existing = (
            await session.execute(
                select(JournalEntry.id).where(
                    JournalEntry.tenant_id == tenant_id,
                    JournalEntry.company_id == request.company_id,
                    JournalEntry.reverses_entry_id == original.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise _conflict("entry_already_reversed", "This journal entry is already reversed.")
        period = await _load_period(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            period_id=original.period_id,
        )
        entry_date = request.entry_date or original.entry_date
        _ensure_period_accepts(period, entry_date, posting=True)
        original_lines = await _load_lines(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            entry_id=original.id,
        )
        reversed_lines = tuple(
            JournalLineInput(
                account_id=line.account_id,
                debit_minor=line.credit_minor,
                credit_minor=line.debit_minor,
                description=line.description,
            )
            for line in original_lines
        )
        number = await _allocate_number(
            session,
            tenant_id=tenant_id,
            company_id=request.company_id,
            period=period,
        )
        now = _now()
        entry = JournalEntry(
            tenant_id=tenant_id,
            company_id=request.company_id,
            period_id=period.id,
            number=number,
            entry_date=entry_date,
            memo=request.memo or f"Reversal of {original.number}",
            status=ENTRY_STATUS_POSTED,
            reverses_entry_id=original.id,
            posted_at=now,
            posted_by=actor_user_id,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(entry)
        await session.flush()
        lines = tuple(
            JournalLine(
                tenant_id=tenant_id,
                company_id=request.company_id,
                entry_id=entry.id,
                line_no=index,
                account_id=line.account_id,
                debit_minor=line.debit_minor,
                credit_minor=line.credit_minor,
                description=line.description,
            )
            for index, line in enumerate(reversed_lines, start=1)
        )
        session.add_all(lines)
        await session.flush()
        if reservation is not None:
            await _complete_idempotency(session, reservation=reservation, entry_id=entry.id)
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="ledger.entry.reversed",
            entity_type="journal_entry",
            entity_id=entry.id,
            detail={"number": number, "reverses": original.number},
            request_id=_request_id(),
        )
        return EntryResult(entry=entry, lines=lines, replayed=False)

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError as error:
        _raise_translated(error)
    except DBAPIError as error:
        _raise_translated(error)
    raise AssertionError("unreachable")


async def close_period(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    period_id: UUID,
    reason: str,
) -> AccountingPeriod:
    """Close a period after refusing when it still holds draft entries."""

    async def operation(session: AsyncSession) -> AccountingPeriod:
        period = await _load_period(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            period_id=period_id,
            for_update=True,
        )
        if period.status == "closed":
            raise _conflict("period_already_closed", "The accounting period is already closed.")
        drafts = await session.scalar(
            select(func.count())
            .select_from(JournalEntry)
            .where(
                JournalEntry.tenant_id == tenant_id,
                JournalEntry.company_id == company_id,
                JournalEntry.period_id == period.id,
                JournalEntry.status == ENTRY_STATUS_DRAFT,
            )
        )
        if drafts:
            raise _conflict(
                "period_has_drafts",
                f"The period still has {int(drafts)} draft journal entries.",
            )
        period.status = "closed"
        period.closed_at = _now()
        period.closed_by = actor_user_id
        period.updated_by = actor_user_id
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="ledger.period.closed",
            entity_type="accounting_period",
            entity_id=period.id,
            detail={"reason": reason, "number": period.number},
            request_id=_request_id(),
        )
        return period

    return await unit_of_work.run(operation, tenant_id=tenant_id)


async def reopen_period(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    period_id: UUID,
    reason: str,
) -> AccountingPeriod:
    """Reopen a closed period and record the reason in the audit trail."""

    async def operation(session: AsyncSession) -> AccountingPeriod:
        period = await _load_period(
            session,
            tenant_id=tenant_id,
            company_id=company_id,
            period_id=period_id,
            for_update=True,
        )
        if period.status != "closed":
            raise _conflict("period_already_open", "The accounting period is already open.")
        period.status = "open"
        period.closed_at = None
        period.closed_by = None
        period.updated_by = actor_user_id
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="ledger.period.reopened",
            entity_type="accounting_period",
            entity_id=period.id,
            detail={"reason": reason, "number": period.number},
            request_id=_request_id(),
        )
        return period

    return await unit_of_work.run(operation, tenant_id=tenant_id)


async def seed_chart(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> list[Account]:
    """Insert the default chart of accounts when the company has none."""

    async def operation(session: AsyncSession) -> list[Account]:
        accounts = await seed_company_chart(session, tenant_id=tenant_id, company_id=company_id)
        return sorted(accounts.values(), key=lambda account: account.code)

    return await unit_of_work.run(operation, tenant_id=tenant_id)


async def get_entry(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    entry_id: UUID,
) -> EntryResult:
    """Read one entry with its lines."""
    return await _load_result(
        session,
        tenant_id=tenant_id,
        company_id=company_id,
        entry_id=entry_id,
        replayed=False,
    )


async def list_entries(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
    period_id: UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[JournalEntry]:
    """List entries newest first with optional period/status filters."""
    statement = select(JournalEntry).where(
        JournalEntry.tenant_id == tenant_id,
        JournalEntry.company_id == company_id,
    )
    if period_id is not None:
        statement = statement.where(JournalEntry.period_id == period_id)
    if status is not None:
        statement = statement.where(JournalEntry.status == status)
    statement = (
        statement.order_by(JournalEntry.created_at.desc(), JournalEntry.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(statement)).scalars().all())


async def list_accounts(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> list[Account]:
    """Return the company's chart ordered by account code."""
    statement = (
        select(Account)
        .where(Account.tenant_id == tenant_id, Account.company_id == company_id)
        .order_by(Account.code)
    )
    return list((await session.execute(statement)).scalars().all())


async def list_periods(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> list[AccountingPeriod]:
    """Return the company's accounting periods in date order."""
    statement = (
        select(AccountingPeriod)
        .where(
            AccountingPeriod.tenant_id == tenant_id,
            AccountingPeriod.company_id == company_id,
        )
        .order_by(AccountingPeriod.start_date)
    )
    return list((await session.execute(statement)).scalars().all())


async def list_fiscal_years(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    company_id: UUID,
) -> list[FiscalYear]:
    """Return the company's fiscal years in date order."""
    statement = (
        select(FiscalYear)
        .where(FiscalYear.tenant_id == tenant_id, FiscalYear.company_id == company_id)
        .order_by(FiscalYear.start_date)
    )
    return list((await session.execute(statement)).scalars().all())


ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "expense")


def _validate_account_type(account_type: str) -> str:
    normalized = account_type.strip().lower()
    if normalized not in ACCOUNT_TYPES:
        raise _bad_request(
            "account_type_invalid",
            "account_type must be one of asset, liability, equity, revenue, expense.",
        )
    return normalized


@dataclass(frozen=True)
class AccountInput:
    """A chart account to create for one company."""

    company_id: UUID
    code: str
    name_ar: str
    name_en: str | None = None
    account_type: str = "asset"
    is_postable: bool = True
    is_active: bool = True


async def create_account(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    request: AccountInput,
) -> Account:
    """Create one chart account; a duplicate code is refused, never merged."""
    code = request.code.strip()
    name_ar = request.name_ar.strip()
    if code == "" or len(code) > 20:
        raise _bad_request("account_code_invalid", "The account code must be 1-20 characters.")
    if name_ar == "":
        raise _bad_request("account_name_invalid", "The Arabic account name is required.")
    account_type = _validate_account_type(request.account_type)

    async def operation(session: AsyncSession) -> Account:
        existing = await session.scalar(
            select(Account.id).where(
                Account.tenant_id == tenant_id,
                Account.company_id == request.company_id,
                Account.code == code,
            )
        )
        if existing is not None:
            raise _conflict("account_code_conflict", f"Account code {code} already exists.")
        account = Account(
            tenant_id=tenant_id,
            company_id=request.company_id,
            code=code,
            name_ar=name_ar,
            name_en=request.name_en.strip() if request.name_en else None,
            account_type=account_type,
            is_postable=request.is_postable,
            is_active=request.is_active,
            created_by=actor_user_id,
            updated_by=actor_user_id,
        )
        session.add(account)
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="ledger.account.created",
            entity_type="account",
            entity_id=account.id,
            detail={"code": code, "account_type": account_type},
            request_id=_request_id(),
        )
        return account

    try:
        return await unit_of_work.run(operation, tenant_id=tenant_id)
    except IntegrityError:
        raise _conflict("account_code_conflict", f"Account code {code} already exists.") from None


async def update_account(
    unit_of_work: UnitOfWork,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    company_id: UUID,
    account_id: UUID,
    name_ar: str | None,
    name_en: str | None,
    is_postable: bool | None,
    is_active: bool | None,
) -> Account:
    """Rename an account or toggle its postable/active flags."""

    async def operation(session: AsyncSession) -> Account:
        account = await session.scalar(
            select(Account)
            .where(
                Account.tenant_id == tenant_id,
                Account.company_id == company_id,
                Account.id == account_id,
            )
            .with_for_update()
        )
        if account is None:
            raise AppError(
                code="account_not_found",
                title="Not found",
                status_code=404,
                detail="The account does not exist in the selected company.",
            )
        if name_ar is not None:
            if name_ar.strip() == "":
                raise _bad_request("account_name_invalid", "The Arabic account name is required.")
            account.name_ar = name_ar.strip()
        if name_en is not None:
            account.name_en = name_en.strip() or None
        if is_postable is not None:
            account.is_postable = is_postable
        if is_active is not None:
            account.is_active = is_active
        account.updated_by = actor_user_id
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="ledger.account.updated",
            entity_type="account",
            entity_id=account.id,
            detail={
                "code": account.code,
                "is_postable": account.is_postable,
                "is_active": account.is_active,
            },
            request_id=_request_id(),
        )
        return account

    return await unit_of_work.run(operation, tenant_id=tenant_id)
