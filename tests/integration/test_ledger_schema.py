"""Database-level ledger invariants (phase 0, day 5).

Direct SQL/ORM writes prove the invariants hold without the service layer:
debit = credit at commit, at least two lines, posted entries immutable, only
postable accounts of the same company, a closed period rejects postings, the
entry date must fall inside its period, and a source document can produce only
one journal entry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.modules.ledger.models import (
    Account,
    AccountingPeriod,
    FiscalYear,
    JournalEntry,
    JournalLine,
)
from app.modules.ledger.seeding import seed_company_chart
from app.shared.ids import new_id
from tests.integration.conftest import SeededIdentity, delete_identity, seed_identity

pytestmark = pytest.mark.integration

YEAR_START = date(2026, 1, 1)
YEAR_END = date(2026, 12, 31)
JAN_START = date(2026, 1, 1)
JAN_END = date(2026, 1, 31)
FEB_START = date(2026, 2, 1)
FEB_END = date(2026, 2, 28)


class Ledger:
    """Committed company A ledger for one test."""

    def __init__(
        self,
        identity: SeededIdentity,
        period_id: UUID,
        closed_period_id: UUID,
        chart: dict[str, Account],
        other_company_account: Account,
    ) -> None:
        self.identity = identity
        self.period_id = period_id
        self.closed_period_id = closed_period_id
        self.chart = chart
        self.other_company_account = other_company_account


def _session(connection: Any) -> AsyncSession:
    return AsyncSession(bind=connection, expire_on_commit=False)


async def _seed_ledger(engine: AsyncEngine, identity: SeededIdentity) -> dict[str, Any]:
    async with engine.begin() as connection:
        session = _session(connection)
        chart = await seed_company_chart(
            session,
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
        )
        chart_b = await seed_company_chart(
            session,
            tenant_id=identity.tenant_id,
            company_id=identity.company_b,
        )
        year = FiscalYear(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            code="FY2026",
            name_ar="السنة 2026",
            start_date=YEAR_START,
            end_date=YEAR_END,
        )
        session.add(year)
        await session.flush()
        open_period = AccountingPeriod(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            fiscal_year_id=year.id,
            number=1,
            start_date=JAN_START,
            end_date=JAN_END,
        )
        closed_period = AccountingPeriod(
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            fiscal_year_id=year.id,
            number=2,
            start_date=FEB_START,
            end_date=FEB_END,
            status="closed",
        )
        session.add_all([open_period, closed_period])
        await session.flush()
        return {
            "period_id": open_period.id,
            "closed_period_id": closed_period.id,
            "chart": chart,
            "other_company_account": chart_b["1000"],
        }


@pytest.fixture
async def ledger(engine: AsyncEngine) -> AsyncIterator[Ledger]:
    identity = await seed_identity(
        engine,
        tenant_slug=f"ledger-{new_id().hex[-12:]}",
        email=f"accountant-{new_id().hex[-12:]}@example.com",
        password="ledger password 123",
        role_code="accountant",
    )
    try:
        seeded = await _seed_ledger(engine, identity)
        yield Ledger(
            identity,
            seeded["period_id"],
            seeded["closed_period_id"],
            seeded["chart"],
            seeded["other_company_account"],
        )
    finally:
        await delete_identity(engine, identity.tenant_id)


def _entry(
    ledger: Ledger,
    *,
    number: str,
    period_id: UUID | None = None,
    entry_date: date = date(2026, 1, 15),
    status: str = "draft",
    source_type: str | None = None,
    source_id: UUID | None = None,
) -> JournalEntry:
    identity = ledger.identity
    return JournalEntry(
        tenant_id=identity.tenant_id,
        company_id=identity.company_a,
        period_id=period_id or ledger.period_id,
        number=number,
        entry_date=entry_date,
        memo=f"test entry {number}",
        status=status,
        source_type=source_type,
        source_id=source_id,
        posted_at=datetime.now(UTC) if status == "posted" else None,
        posted_by=identity.user_id if status == "posted" else None,
    )


def _line(
    ledger: Ledger,
    entry: JournalEntry,
    *,
    line_no: int,
    account: Account,
    debit: int = 0,
    credit: int = 0,
) -> JournalLine:
    identity = ledger.identity
    return JournalLine(
        tenant_id=identity.tenant_id,
        company_id=identity.company_a,
        entry_id=entry.id,
        line_no=line_no,
        account_id=account.id,
        debit_minor=debit,
        credit_minor=credit,
    )


async def _add_balanced_entry(
    session: AsyncSession,
    ledger: Ledger,
    *,
    number: str,
    amount: int = 1000,
    status: str = "draft",
    period_id: UUID | None = None,
    entry_date: date = date(2026, 1, 15),
    source_type: str | None = None,
    source_id: UUID | None = None,
) -> JournalEntry:
    entry = _entry(
        ledger,
        number=number,
        period_id=period_id,
        entry_date=entry_date,
        status=status,
        source_type=source_type,
        source_id=source_id,
    )
    session.add(entry)
    await session.flush()
    session.add_all(
        [
            _line(ledger, entry, line_no=1, account=ledger.chart["1000"], debit=amount),
            _line(ledger, entry, line_no=2, account=ledger.chart["4000"], credit=amount),
        ]
    )
    await session.flush()
    return entry


async def _expect_db_error(scenario: Callable[[], Awaitable[None]]) -> str:
    with pytest.raises(DBAPIError) as failure:
        await scenario()
    return str(failure.value).lower()


async def _totals(engine: AsyncEngine, entry_id: UUID) -> tuple[int, int, int]:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    func.coalesce(func.sum(JournalLine.debit_minor), 0),
                    func.coalesce(func.sum(JournalLine.credit_minor), 0),
                    func.count(),
                ).where(JournalLine.entry_id == entry_id)
            )
        ).one()
    return int(row[0]), int(row[1]), int(row[2])


async def test_balanced_entry_commits(engine: AsyncEngine, ledger: Ledger) -> None:
    async with engine.begin() as connection:
        session = _session(connection)
        entry = await _add_balanced_entry(session, ledger, number="JE-0001", amount=125000)
        entry_id = entry.id
    debit, credit, lines = await _totals(engine, entry_id)
    assert (debit, credit, lines) == (125000, 125000, 2)


async def test_unbalanced_entry_fails_at_commit(engine: AsyncEngine, ledger: Ledger) -> None:
    async def scenario() -> None:
        async with engine.begin() as connection:
            session = _session(connection)
            entry = _entry(ledger, number="JE-0002")
            session.add(entry)
            await session.flush()
            session.add_all(
                [
                    _line(ledger, entry, line_no=1, account=ledger.chart["1100"], debit=100),
                    _line(ledger, entry, line_no=2, account=ledger.chart["4000"], credit=90),
                ]
            )
            await session.flush()

    assert "unbalanced" in await _expect_db_error(scenario)


async def test_single_line_entry_fails_at_commit(engine: AsyncEngine, ledger: Ledger) -> None:
    async def scenario() -> None:
        async with engine.begin() as connection:
            session = _session(connection)
            entry = _entry(ledger, number="JE-0003")
            session.add(entry)
            await session.flush()
            session.add(_line(ledger, entry, line_no=1, account=ledger.chart["1000"], debit=500))
            await session.flush()

    assert "two lines" in await _expect_db_error(scenario)


async def test_posted_entry_is_immutable(engine: AsyncEngine, ledger: Ledger) -> None:
    async with engine.begin() as connection:
        session = _session(connection)
        entry = await _add_balanced_entry(session, ledger, number="JE-0004", status="posted")
        entry_id = entry.id

    async def update_scenario() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE journal_entry SET memo = 'changed' WHERE id = :id"),
                {"id": entry_id},
            )

    assert "immutable" in await _expect_db_error(update_scenario)

    async def delete_scenario() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM journal_entry WHERE id = :id"), {"id": entry_id}
            )

    assert "immutable" in await _expect_db_error(delete_scenario)


async def test_draft_entry_can_be_updated(engine: AsyncEngine, ledger: Ledger) -> None:
    async with engine.begin() as connection:
        session = _session(connection)
        draft = await _add_balanced_entry(session, ledger, number="JE-0005")
        draft.memo = "edited while draft"
        await session.flush()
        draft_id = draft.id
    async with engine.connect() as connection:
        memo = await connection.scalar(select(JournalEntry.memo).where(JournalEntry.id == draft_id))
    assert memo == "edited while draft"


async def test_closed_period_rejects_posting(engine: AsyncEngine, ledger: Ledger) -> None:
    async def scenario() -> None:
        async with engine.begin() as connection:
            session = _session(connection)
            await _add_balanced_entry(
                session,
                ledger,
                number="JE-0006",
                status="posted",
                period_id=ledger.closed_period_id,
                entry_date=date(2026, 2, 10),
            )

    assert "closed" in await _expect_db_error(scenario)


async def test_entry_date_must_fall_inside_period(engine: AsyncEngine, ledger: Ledger) -> None:
    async def scenario() -> None:
        async with engine.begin() as connection:
            session = _session(connection)
            entry = _entry(ledger, number="JE-0007", entry_date=date(2026, 3, 1))
            session.add(entry)
            await session.flush()

    assert "outside the selected period" in await _expect_db_error(scenario)


async def test_non_postable_account_is_rejected(engine: AsyncEngine, ledger: Ledger) -> None:
    async with engine.begin() as connection:
        session = _session(connection)
        heading = Account(
            tenant_id=ledger.identity.tenant_id,
            company_id=ledger.identity.company_a,
            code="1999",
            name_ar="حساب تجميعي",
            account_type="asset",
            is_postable=False,
        )
        session.add(heading)
        await session.flush()

    async def scenario() -> None:
        async with engine.begin() as connection:
            session = _session(connection)
            entry = _entry(ledger, number="JE-0008")
            session.add(entry)
            await session.flush()
            session.add_all(
                [
                    _line(ledger, entry, line_no=1, account=heading, debit=100),
                    _line(ledger, entry, line_no=2, account=ledger.chart["4000"], credit=100),
                ]
            )
            await session.flush()

    assert "not a postable" in await _expect_db_error(scenario)


async def test_account_of_another_company_is_rejected(
    engine: AsyncEngine,
    ledger: Ledger,
) -> None:
    async def scenario() -> None:
        async with engine.begin() as connection:
            session = _session(connection)
            entry = _entry(ledger, number="JE-0009")
            session.add(entry)
            await session.flush()
            session.add_all(
                [
                    _line(ledger, entry, line_no=1, account=ledger.other_company_account, debit=10),
                    _line(ledger, entry, line_no=2, account=ledger.chart["4000"], credit=10),
                ]
            )
            await session.flush()

    await _expect_db_error(scenario)


async def test_source_document_is_unique(engine: AsyncEngine, ledger: Ledger) -> None:
    source_id = new_id()

    async def scenario() -> None:
        async with engine.begin() as connection:
            session = _session(connection)
            await _add_balanced_entry(
                session,
                ledger,
                number="JE-0010",
                source_type="sales_invoice",
                source_id=source_id,
            )
            await _add_balanced_entry(
                session,
                ledger,
                number="JE-0011",
                source_type="sales_invoice",
                source_id=source_id,
            )

    await _expect_db_error(scenario)


async def test_line_requires_exactly_one_side(engine: AsyncEngine, ledger: Ledger) -> None:
    async def scenario() -> None:
        async with engine.begin() as connection:
            session = _session(connection)
            entry = _entry(ledger, number="JE-0012")
            session.add(entry)
            await session.flush()
            session.add_all(
                [
                    _line(
                        ledger, entry, line_no=1, account=ledger.chart["1000"], debit=50, credit=50
                    ),
                    _line(ledger, entry, line_no=2, account=ledger.chart["4000"], credit=100),
                ]
            )
            await session.flush()

    await _expect_db_error(scenario)
