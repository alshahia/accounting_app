"""Release evidence for provisioning, roles and tamper-evident records (AC-10..AC-13).

The fresh-install test runs against its own empty database migrated with Alembic,
so "needs setup" is a real statement about an installation that has never seen
the demo seed or a manual SQL step.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.main import create_app
from app.shared.ids import new_id
from tests.conftest import TEST_DATABASE_URL
from tests.integration.conftest import (
    SeededIdentity,
    SeededLedger,
    delete_identity,
    seed_identity,
    seed_ledger,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_TOKEN = "release-gate-bootstrap-token-0123456789abcdef"
ADMIN_PASSWORD = "Release-Gate-Admin-2026"
NEW_USER_PASSWORD = "Release-Gate-User-2026"
LOGIN_URL = "/api/v1/auth/login"
ENTRIES_URL = "/api/v1/ledger/journal-entries"
PERIODS_URL = "/api/v1/ledger/periods"


def _database_url(name: str) -> str:
    base = TEST_DATABASE_URL.rsplit("/", 1)[0]
    return f"{base}/{name}"


async def _create_empty_database() -> str:
    """Create a database and migrate it to head; the caller drops it afterwards."""

    name = f"almahmoud_setup_{new_id().hex[:10]}"
    maintenance = create_async_engine(
        _database_url("postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with maintenance.connect() as connection:
            await connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        await maintenance.dispose()

    url = _database_url(name)
    environment = os.environ.copy()
    environment["MAHMOUD_DATABASE_URL"] = url
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")
    return url


async def _drop_database(url: str) -> None:
    name = url.rsplit("/", 1)[-1]
    maintenance = create_async_engine(
        _database_url("postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with maintenance.connect() as connection:
            await connection.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            await connection.execute(sa.text(f'DROP DATABASE "{name}"'))
    finally:
        await maintenance.dispose()


def _decoded_sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


async def test_ac10_fresh_install_provisions_tenant_company_and_users() -> None:
    url = await _create_empty_database()
    slug = f"fresh-{new_id().hex[-8:]}"
    try:
        settings = Settings(
            environment="test",
            database_url=SecretStr(url),
            provision_database_url=SecretStr(url),
            bootstrap_token=SecretStr(BOOTSTRAP_TOKEN),
            log_level="WARNING",
            jwt_secret=SecretStr("test-secret-key-for-unit-and-integration-tests"),
            cookie_secure=False,
            max_failed_logins=5,
            lockout_minutes=1,
        )
        application = create_app(settings)
        async with application.router.lifespan_context(application):
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                status = await client.get("/api/v1/setup/status")
                assert status.status_code == 200, status.text
                assert status.json()["needs_setup"] is True
                assert status.json()["bootstrap_configured"] is True

                body = {
                    "tenant_name": "Fresh Company",
                    "tenant_slug": slug,
                    "company_code": "MAIN",
                    "company_name_ar": "الشركة الرئيسية",
                    "company_name_en": "Main Company",
                    "base_currency": "USD",
                    "admin_email": f"admin-{slug}@example.com",
                    "admin_full_name": "Release Admin",
                    "admin_password": ADMIN_PASSWORD,
                    "require_mfa": False,
                    "fiscal_year": 2026,
                }

                without_token = await client.post("/api/v1/setup", json=body)
                assert without_token.status_code == 403, without_token.text
                assert without_token.json()["code"] == "setup_token_invalid"

                created = await client.post(
                    "/api/v1/setup",
                    json=body,
                    headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
                )
                assert created.status_code == 201, created.text
                provisioned = created.json()
                assert UUID(provisioned["tenant_id"]).version == 7
                company_id = provisioned["company_id"]
                admin_email = provisioned["admin_email"]

                closed = await client.post(
                    "/api/v1/setup",
                    json={**body, "tenant_slug": f"{slug}-second"},
                    headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
                )
                assert closed.status_code == 409, closed.text
                assert closed.json()["code"] == "setup_completed"
                assert (await client.get("/api/v1/setup/status")).json()["needs_setup"] is False

                login = await client.post(
                    LOGIN_URL,
                    json={
                        "email": admin_email,
                        "password": ADMIN_PASSWORD,
                        "tenant": slug,
                    },
                )
                assert login.status_code == 200, login.text
                admin_headers = {
                    "Authorization": f"Bearer {login.json()['access_token']}",
                    "X-Company-Id": company_id,
                }

                roles = await client.get("/api/v1/admin/roles", headers=admin_headers)
                assert roles.status_code == 200, roles.text
                role_codes = {role["code"] for role in roles.json()}
                assert {"tenant_admin", "company_admin", "accountant"}.issubset(role_codes)
                tenant_admin = next(role for role in roles.json() if role["code"] == "tenant_admin")
                assert tenant_admin["permission_count"] > 0

                users = await client.get("/api/v1/admin/users", headers=admin_headers)
                assert users.status_code == 200, users.text
                assert [user["email"] for user in users.json()] == [admin_email]

                invite = await client.post(
                    "/api/v1/admin/users",
                    json={
                        "email": f"accountant-{slug}@example.com",
                        "full_name": "Release Accountant",
                        "company_id": company_id,
                        "role_codes": ["accountant"],
                    },
                    headers=admin_headers,
                )
                assert invite.status_code == 201, invite.text
                invitation = invite.json()
                assert invitation["invitation_token"]
                assert invitation["user"]["email"] == f"accountant-{slug}@example.com"

                accepted = await client.post(
                    "/api/v1/auth/accept-invitation",
                    json={"token": invitation["invitation_token"], "password": NEW_USER_PASSWORD},
                )
                assert accepted.status_code == 204, accepted.text
                # The link is single-use: its epoch no longer matches, and the
                # error says the invitation was already used rather than expired.
                replay = await client.post(
                    "/api/v1/auth/accept-invitation",
                    json={"token": invitation["invitation_token"], "password": NEW_USER_PASSWORD},
                )
                assert replay.status_code == 409, replay.text
                assert replay.json()["code"] == "credential_used"

                accountant_login = await client.post(
                    LOGIN_URL,
                    json={
                        "email": f"accountant-{slug}@example.com",
                        "password": NEW_USER_PASSWORD,
                        "tenant": slug,
                    },
                )
                assert accountant_login.status_code == 200, accountant_login.text
                accountant_headers = {
                    "Authorization": f"Bearer {accountant_login.json()['access_token']}",
                    "X-Company-Id": company_id,
                }
                me = await client.get("/api/v1/auth/me", headers=accountant_headers)
                assert me.status_code == 200, me.text

                denied = await client.get("/api/v1/admin/users", headers=accountant_headers)
                assert denied.status_code == 403, denied.text
                assert denied.json()["code"] == "permission_denied"

                second_company = await client.post(
                    "/api/v1/admin/companies",
                    json={
                        "code": "SECOND",
                        "name_ar": "الشركة الثانية",
                        "base_currency": "USD",
                        "fiscal_year": 2026,
                    },
                    headers=admin_headers,
                )
                assert second_company.status_code == 201, second_company.text
                assert second_company.json()["code"] == "SECOND"
    finally:
        await _drop_database(url)


@pytest.fixture
async def ledger(engine: AsyncEngine) -> AsyncIterator[SeededLedger]:
    """A committed tenant with a chart, an open January and a closed February."""
    seeded = await seed_identity(
        engine,
        tenant_slug=f"release-{new_id().hex[-12:]}",
        email=f"release-{new_id().hex[-12:]}@example.com",
        password="correct horse battery staple",
    )
    try:
        yield await seed_ledger(engine, seeded)
    finally:
        await delete_identity(engine, seeded.tenant_id)


def _payload(ledger: SeededLedger, *, amount: int = 1000) -> dict[str, object]:
    return {
        "period_id": str(ledger.period_id),
        "entry_date": date(2026, 1, 15).isoformat(),
        "post": True,
        "memo": "record integrity",
        "lines": [
            {"account_id": str(ledger.account_ids["1000"]), "debit_minor": amount},
            {"account_id": str(ledger.account_ids["4000"]), "credit_minor": amount},
        ],
    }


async def _headers(
    client: AsyncClient,
    identity: SeededIdentity,
    company_id: UUID,
    *,
    password: str | None = None,
) -> dict[str, str]:
    response = await client.post(
        LOGIN_URL,
        json={"email": identity.email, "password": password or identity.password},
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Company-Id": str(company_id),
    }


async def test_ac11_application_role_cannot_delete_audit_rows(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(sa.text("SET ROLE almahmoud_app"))
            with pytest.raises(DBAPIError) as excinfo:
                await connection.execute(sa.text("DELETE FROM audit_log"))
        finally:
            await transaction.rollback()
    assert _decoded_sqlstate(excinfo.value) == "42501"


async def test_ac12_posted_lines_are_immutable_and_reversal_is_the_only_way(
    client: AsyncClient,
    engine: AsyncEngine,
    ledger: SeededLedger,
) -> None:
    headers = await _headers(client, ledger.identity, ledger.identity.company_a)
    created = await client.post(ENTRIES_URL, json=_payload(ledger), headers=headers)
    assert created.status_code == 201, created.text
    entry_id = created.json()["id"]

    parameters = {"tenant_id": str(ledger.identity.tenant_id), "entry_id": entry_id}
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(DBAPIError) as update_error:
                await connection.execute(
                    sa.text(
                        "UPDATE journal_line SET debit_minor = debit_minor + 1 "
                        "WHERE tenant_id = :tenant_id AND entry_id = :entry_id"
                    ),
                    parameters,
                )
        finally:
            await transaction.rollback()
    assert _decoded_sqlstate(update_error.value) == "23514"

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(DBAPIError) as delete_error:
                await connection.execute(
                    sa.text(
                        "DELETE FROM journal_line "
                        "WHERE tenant_id = :tenant_id AND entry_id = :entry_id"
                    ),
                    parameters,
                )
        finally:
            await transaction.rollback()
    assert _decoded_sqlstate(delete_error.value) == "23514"

    reversal = await client.post(f"{ENTRIES_URL}/{entry_id}/reverse", json={}, headers=headers)
    assert reversal.status_code == 201, reversal.text
    assert reversal.json()["reverses_entry_id"] == entry_id

    original = await client.get(f"{ENTRIES_URL}/{entry_id}", headers=headers)
    assert original.status_code == 200, original.text
    assert original.json()["status"] == "posted"
    assert original.json()["total_debit_minor"] == 1000


async def test_ac13_concurrent_period_closes_give_one_winner_and_one_conflict(
    client: AsyncClient,
    ledger: SeededLedger,
) -> None:
    headers = await _headers(client, ledger.identity, ledger.identity.company_a)
    url = f"{PERIODS_URL}/{ledger.period_id}/close"
    responses: list[Response] = await asyncio.gather(
        client.post(url, json={"reason": "month end (a)"}, headers=headers),
        client.post(url, json={"reason": "month end (b)"}, headers=headers),
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 409], [response.text for response in responses]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["code"] == "period_already_closed"
