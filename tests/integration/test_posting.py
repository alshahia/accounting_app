"""End-to-end posting tests against PostgreSQL (phase 0, day 6).

The tests exercise the real ASGI app and the real database: manual journal
creation as draft and posted, idempotent retries, reversal, period close/reopen,
permission denials, and 20 parallel postings that must not lose an update. The
concurrency test drives the posting service directly with its own unit of work,
which is the same transaction path the API uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.modules.ledger.models import JournalEntry, JournalLine
from app.modules.ledger.service import (
    EntryResult,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.shared.ids import new_id
from app.shared.unit_of_work import UnitOfWork
from tests.integration.conftest import (
    SeededIdentity,
    SeededLedger,
    delete_identity,
    seed_identity,
    seed_ledger,
)

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
ENTRIES_URL = "/api/v1/ledger/journal-entries"
PERIODS_URL = "/api/v1/ledger/periods"
ACCOUNTS_URL = "/api/v1/ledger/accounts"
ENTRY_DATE = date(2026, 1, 15)


@pytest.fixture
async def ledger(engine: AsyncEngine, identity: SeededIdentity) -> SeededLedger:
    """The identity fixture plus a chart, an open January and a closed February."""
    return await seed_ledger(engine, identity)


@pytest.fixture
async def admin_ledger(engine: AsyncEngine) -> AsyncIterator[SeededLedger]:
    """A committed company-admin identity with its own chart, for settings writes."""
    seeded = await seed_identity(
        engine,
        tenant_slug=f"admin-{new_id().hex[-12:]}",
        email=f"admin-{new_id().hex[-12:]}@example.com",
        password="admin password 123",
        role_code="company_admin",
    )
    ledger = await seed_ledger(engine, seeded)
    try:
        yield ledger
    finally:
        await delete_identity(engine, seeded.tenant_id)


@pytest.fixture
async def sales_identity(engine: AsyncEngine) -> AsyncIterator[SeededIdentity]:
    """A committed sales-role user for permission-denial tests."""
    seeded = await seed_identity(
        engine,
        tenant_slug=f"sales-{new_id().hex[-12:]}",
        email=f"sales-{new_id().hex[-12:]}@example.com",
        password="sales password 123",
        role_code="sales",
    )
    try:
        yield seeded
    finally:
        await delete_identity(engine, seeded.tenant_id)


async def _headers(
    client: AsyncClient,
    identity: SeededIdentity,
    company_id: UUID,
) -> dict[str, str]:
    response = await client.post(
        LOGIN_URL,
        json={"email": identity.email, "password": identity.password},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}", "X-Company-Id": str(company_id)}


def _payload(
    ledger: SeededLedger,
    *,
    post: bool = True,
    amount: int = 1000,
    memo: str = "manual journal",
    period_id: UUID | None = None,
    entry_date: date = ENTRY_DATE,
) -> dict[str, object]:
    account_ids = ledger.account_ids
    return {
        "period_id": str(period_id or ledger.period_id),
        "entry_date": entry_date.isoformat(),
        "post": post,
        "memo": memo,
        "lines": [
            {"account_id": str(account_ids["1000"]), "debit_minor": amount},
            {"account_id": str(account_ids["4000"]), "credit_minor": amount},
        ],
    }


async def test_draft_and_posted_manual_journals(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    headers = await _headers(client, ledger.identity, ledger.identity.company_a)

    draft = await client.post(ENTRIES_URL, json=_payload(ledger, post=False), headers=headers)
    assert draft.status_code == 201, draft.text
    draft_body = draft.json()
    assert draft_body["status"] == "draft"
    assert draft_body["posted_at"] is None
    assert draft_body["number"].startswith("JE-2026-")
    assert draft_body["total_debit_minor"] == 1000
    assert draft_body["total_credit_minor"] == 1000
    assert len(draft_body["lines"]) == 2

    posted = await client.post(
        ENTRIES_URL, json=_payload(ledger, post=True, amount=2500), headers=headers
    )
    assert posted.status_code == 201, posted.text
    posted_body = posted.json()
    assert posted_body["status"] == "posted"
    assert posted_body["posted_at"] is not None
    assert posted_body["number"] != draft_body["number"]

    fetched = await client.get(f"{ENTRIES_URL}/{posted_body['id']}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["total_debit_minor"] == 2500

    listed = await client.get(ENTRIES_URL, headers=headers)
    assert listed.status_code == 200, listed.text
    assert {item["id"] for item in listed.json()} == {draft_body["id"], posted_body["id"]}

    accounts = await client.get(ACCOUNTS_URL, headers=headers)
    assert accounts.status_code == 200, accounts.text
    assert {account["code"] for account in accounts.json()} >= {"1000", "4000"}


async def test_draft_can_be_edited_posted_and_deleted(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    headers = await _headers(client, ledger.identity, ledger.identity.company_a)
    created = await client.post(ENTRIES_URL, json=_payload(ledger, post=False), headers=headers)
    assert created.status_code == 201, created.text
    draft = created.json()

    edit = _payload(ledger, amount=3300, memo="edited draft")
    edit.pop("post")
    updated = await client.put(f"{ENTRIES_URL}/{draft['id']}", json=edit, headers=headers)
    assert updated.status_code == 200, updated.text
    updated_body = updated.json()
    assert updated_body["memo"] == "edited draft"
    assert updated_body["total_debit_minor"] == 3300
    assert updated_body["status"] == "draft"

    posted = await client.post(f"{ENTRIES_URL}/{draft['id']}/post", headers=headers)
    assert posted.status_code == 200, posted.text
    posted_body = posted.json()
    assert posted_body["status"] == "posted"
    assert posted_body["posted_at"] is not None
    assert posted_body["number"] == draft["number"]

    locked = await client.put(f"{ENTRIES_URL}/{draft['id']}", json=edit, headers=headers)
    assert locked.status_code == 409, locked.text
    assert locked.json()["code"] == "entry_not_editable"

    protected = await client.delete(f"{ENTRIES_URL}/{draft['id']}", headers=headers)
    assert protected.status_code == 409, protected.text
    assert protected.json()["code"] == "entry_not_editable"

    second = await client.post(ENTRIES_URL, json=_payload(ledger, post=False), headers=headers)
    assert second.status_code == 201, second.text
    deleted = await client.delete(f"{ENTRIES_URL}/{second.json()['id']}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    missing = await client.get(f"{ENTRIES_URL}/{second.json()['id']}", headers=headers)
    assert missing.status_code == 404, missing.text


async def test_unbalanced_and_invalid_entries_are_rejected(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    headers = await _headers(client, ledger.identity, ledger.identity.company_a)

    unbalanced = _payload(ledger)
    unbalanced["lines"] = [
        {"account_id": str(ledger.account_ids["1000"]), "debit_minor": 100},
        {"account_id": str(ledger.account_ids["4000"]), "credit_minor": 90},
    ]
    response = await client.post(ENTRIES_URL, json=unbalanced, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "journal_unbalanced"

    both_sides = _payload(ledger)
    both_sides["lines"] = [
        {"account_id": str(ledger.account_ids["1000"]), "debit_minor": 100, "credit_minor": 100},
        {"account_id": str(ledger.account_ids["4000"]), "credit_minor": 100},
    ]
    response = await client.post(ENTRIES_URL, json=both_sides, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "journal_lines_invalid"

    one_line = _payload(ledger)
    one_line["lines"] = [{"account_id": str(ledger.account_ids["1000"]), "debit_minor": 100}]
    response = await client.post(ENTRIES_URL, json=one_line, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"

    missing_account = _payload(ledger)
    missing_account["lines"] = [
        {"account_id": str(new_id()), "debit_minor": 100},
        {"account_id": str(ledger.account_ids["4000"]), "credit_minor": 100},
    ]
    response = await client.post(ENTRIES_URL, json=missing_account, headers=headers)
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "account_not_found"


async def test_idempotency_key_returns_the_same_entry(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
) -> None:
    headers = await _headers(client, ledger.identity, ledger.identity.company_a)
    idempotency_key = new_id().hex
    first_headers = {**headers, "Idempotency-Key": idempotency_key}
    payload = _payload(ledger, memo="idempotent post")

    first = await client.post(ENTRIES_URL, json=payload, headers=first_headers)
    assert first.status_code == 201, first.text
    second = await client.post(ENTRIES_URL, json=payload, headers=first_headers)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["idempotent_replay"] is True

    changed = dict(payload)
    changed["memo"] = "different request"
    conflict = await client.post(ENTRIES_URL, json=changed, headers=first_headers)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "idempotency_conflict"

    async with engine.connect() as connection:
        count = await connection.scalar(
            select(func.count())
            .select_from(JournalEntry)
            .where(JournalEntry.tenant_id == ledger.identity.tenant_id)
        )
    assert count == 1


async def test_reversal_is_linked_and_the_original_stays_posted(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    headers = await _headers(client, ledger.identity, ledger.identity.company_a)
    created = await client.post(ENTRIES_URL, json=_payload(ledger, amount=700), headers=headers)
    assert created.status_code == 201, created.text
    original = created.json()

    reversal = await client.post(
        f"{ENTRIES_URL}/{original['id']}/reverse",
        json={"memo": "wrong account"},
        headers=headers,
    )
    assert reversal.status_code == 201, reversal.text
    body = reversal.json()
    assert body["reverses_entry_id"] == original["id"]
    assert body["status"] == "posted"
    assert body["memo"] == "wrong account"
    reversed_lines = {
        (line["account_id"], line["debit_minor"], line["credit_minor"]) for line in body["lines"]
    }
    assert (str(ledger.account_ids["4000"]), 700, 0) in reversed_lines
    assert (str(ledger.account_ids["1000"]), 0, 700) in reversed_lines

    original_after = await client.get(f"{ENTRIES_URL}/{original['id']}", headers=headers)
    assert original_after.json()["status"] == "posted"
    assert original_after.json()["total_debit_minor"] == 700

    replay = await client.post(
        f"{ENTRIES_URL}/{original['id']}/reverse",
        json={},
        headers=headers,
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["code"] == "entry_already_reversed"


async def test_period_close_blocks_postings_and_reopen_restores_them(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    headers = await _headers(client, ledger.identity, ledger.identity.company_a)
    first = await client.post(ENTRIES_URL, json=_payload(ledger), headers=headers)
    assert first.status_code == 201, first.text

    close_url = f"{PERIODS_URL}/{ledger.period_id}/close"
    closed = await client.post(close_url, json={"reason": "month end"}, headers=headers)
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"

    blocked = await client.post(ENTRIES_URL, json=_payload(ledger), headers=headers)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "period_closed"

    reopen_url = f"{PERIODS_URL}/{ledger.period_id}/reopen"
    reopened = await client.post(reopen_url, json={"reason": "late correction"}, headers=headers)
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "open"

    draft = await client.post(ENTRIES_URL, json=_payload(ledger, post=False), headers=headers)
    assert draft.status_code == 201, draft.text
    with_drafts = await client.post(close_url, json={"reason": "month end"}, headers=headers)
    assert with_drafts.status_code == 409, with_drafts.text
    assert with_drafts.json()["code"] == "period_has_drafts"


async def test_sales_role_cannot_post_a_journal(
    client: AsyncClient,
    sales_identity: SeededIdentity,
) -> None:
    headers = await _headers(client, sales_identity, sales_identity.company_a)
    response = await client.post(
        ENTRIES_URL,
        json={
            "period_id": str(new_id()),
            "entry_date": ENTRY_DATE.isoformat(),
            "lines": [
                {"account_id": str(new_id()), "debit_minor": 10},
                {"account_id": str(new_id()), "credit_minor": 10},
            ],
        },
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "permission_denied"


async def test_twenty_parallel_postings_stay_balanced(
    engine: AsyncEngine,
    ledger: SeededLedger,
) -> None:
    identity = ledger.identity
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    unit_of_work = UnitOfWork(session_factory)
    amount = 500
    postings = 20

    async def post(index: int) -> EntryResult:
        return await create_journal_entry(
            unit_of_work,
            tenant_id=identity.tenant_id,
            actor_user_id=identity.user_id,
            request=JournalEntryInput(
                company_id=identity.company_a,
                period_id=ledger.period_id,
                entry_date=ENTRY_DATE,
                memo=f"parallel posting {index}",
                post=True,
                lines=(
                    JournalLineInput(account_id=ledger.account_ids["1000"], debit_minor=amount),
                    JournalLineInput(account_id=ledger.account_ids["4000"], credit_minor=amount),
                ),
            ),
        )

    results = await asyncio.gather(*(post(index) for index in range(postings)))
    assert len({result.entry.number for result in results}) == postings
    assert len({result.entry.id for result in results}) == postings

    async with engine.connect() as connection:
        entry_count = await connection.scalar(
            select(func.count())
            .select_from(JournalEntry)
            .where(
                JournalEntry.tenant_id == identity.tenant_id,
                JournalEntry.company_id == identity.company_a,
            )
        )
        debit_total = await connection.scalar(
            select(func.coalesce(func.sum(JournalLine.debit_minor), 0))
            .select_from(JournalLine)
            .where(JournalLine.tenant_id == identity.tenant_id)
        )
        credit_total = await connection.scalar(
            select(func.coalesce(func.sum(JournalLine.credit_minor), 0))
            .select_from(JournalLine)
            .where(JournalLine.tenant_id == identity.tenant_id)
        )
        cash_balance = await connection.scalar(
            text(
                """
                SELECT COALESCE(SUM(l.debit_minor - l.credit_minor), 0)
                  FROM journal_line l
                 WHERE l.tenant_id = :tenant_id
                   AND l.account_id = :account_id
                """
            ),
            {"tenant_id": str(identity.tenant_id), "account_id": str(ledger.account_ids["1000"])},
        )
    assert entry_count == postings
    assert debit_total == postings * amount
    assert credit_total == postings * amount
    assert cash_balance == postings * amount


async def test_chart_account_create_update_and_duplicate(
    client: AsyncClient,
    admin_ledger: SeededLedger,
) -> None:
    ledger = admin_ledger
    headers = await _headers(client, ledger.identity, ledger.identity.company_a)
    payload = {
        "code": "1610",
        "name_ar": "أثاث ومعدات",
        "name_en": "Furniture and equipment",
        "account_type": "asset",
    }
    created = await client.post(ACCOUNTS_URL, json=payload, headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["code"] == "1610"
    assert body["is_postable"] is True
    assert body["is_active"] is True

    duplicate = await client.post(ACCOUNTS_URL, json=payload, headers=headers)
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["code"] == "account_code_conflict"

    invalid_type = await client.post(
        ACCOUNTS_URL,
        json={**payload, "code": "1611", "account_type": "suspense"},
        headers=headers,
    )
    assert invalid_type.status_code == 422, invalid_type.text

    updated = await client.patch(
        f"{ACCOUNTS_URL}/{body['id']}",
        json={"name_en": "Furniture", "is_active": False, "is_postable": False},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    updated_body = updated.json()
    assert updated_body["name_en"] == "Furniture"
    assert updated_body["name_ar"] == "أثاث ومعدات"
    assert updated_body["is_active"] is False
    assert updated_body["is_postable"] is False

    listed = await client.get(ACCOUNTS_URL, headers=headers)
    assert listed.status_code == 200, listed.text
    assert any(account["code"] == "1610" for account in listed.json())

    missing = await client.patch(
        f"{ACCOUNTS_URL}/{new_id()}",
        json={"name_en": "Nope"},
        headers=headers,
    )
    assert missing.status_code == 404, missing.text
    assert missing.json()["code"] == "account_not_found"


async def test_chart_account_create_requires_settings_permission(
    client: AsyncClient,
    sales_identity: SeededIdentity,
) -> None:
    headers = await _headers(client, sales_identity, sales_identity.company_a)
    response = await client.post(
        ACCOUNTS_URL,
        json={"code": "1620", "name_ar": "حساب اختبار", "account_type": "asset"},
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "permission_denied"
