"""Tenant isolation proven with raw SQL under the application role (integration)."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.modules.identity.models import Company
from app.shared.ids import new_id
from app.shared.unit_of_work import UnitOfWork

pytestmark = pytest.mark.integration

TENANT_SQL = "INSERT INTO tenant (id, name, slug) VALUES (:id, :name, :slug)"
COMPANY_SQL = (
    "INSERT INTO company (id, tenant_id, code, name_ar, base_currency) "
    "VALUES (:id, :tenant_id, :code, :name_ar, 'USD')"
)
SET_TENANT_SQL = "SELECT set_config('app.tenant_id', :tenant_id, true)"
SELECT_COMPANIES_SQL = "SELECT id FROM company WHERE id IN (:company_a, :company_b)"


async def _seed_two_tenants(connection: AsyncConnection) -> tuple[UUID, UUID, UUID, UUID]:
    tenant_a, tenant_b = new_id(), new_id()
    company_a, company_b = new_id(), new_id()
    await connection.execute(
        text(TENANT_SQL),
        {"id": str(tenant_a), "name": "Tenant A", "slug": f"rls-a-{tenant_a.hex}"},
    )
    await connection.execute(
        text(TENANT_SQL),
        {"id": str(tenant_b), "name": "Tenant B", "slug": f"rls-b-{tenant_b.hex}"},
    )
    await connection.execute(
        text(COMPANY_SQL),
        {
            "id": str(company_a),
            "tenant_id": str(tenant_a),
            "code": "A-1",
            "name_ar": "شركة ألف",
        },
    )
    await connection.execute(
        text(COMPANY_SQL),
        {
            "id": str(company_b),
            "tenant_id": str(tenant_b),
            "code": "B-1",
            "name_ar": "شركة باء",
        },
    )
    return tenant_a, tenant_b, company_a, company_b


async def test_rls_limits_reads_to_the_bound_tenant(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            tenant_a, tenant_b, company_a, company_b = await _seed_two_tenants(connection)
            await connection.execute(text("SET ROLE almahmoud_app"))

            await connection.execute(text(SET_TENANT_SQL), {"tenant_id": str(tenant_a)})
            visible_a = (
                (
                    await connection.execute(
                        text(SELECT_COMPANIES_SQL),
                        {"company_a": str(company_a), "company_b": str(company_b)},
                    )
                )
                .scalars()
                .all()
            )
            assert visible_a == [company_a]

            await connection.execute(text(SET_TENANT_SQL), {"tenant_id": str(tenant_b)})
            visible_b = (
                (
                    await connection.execute(
                        text(SELECT_COMPANIES_SQL),
                        {"company_a": str(company_a), "company_b": str(company_b)},
                    )
                )
                .scalars()
                .all()
            )
            assert visible_b == [company_b]

            # Fail closed: no tenant in the transaction means no rows at all.
            await connection.execute(text(SET_TENANT_SQL), {"tenant_id": ""})
            visible_none = (
                (
                    await connection.execute(
                        text(SELECT_COMPANIES_SQL),
                        {"company_a": str(company_a), "company_b": str(company_b)},
                    )
                )
                .scalars()
                .all()
            )
            assert visible_none == []
        finally:
            await transaction.rollback()


async def test_unit_of_work_runs_tenant_transactions_as_the_restricted_role(
    engine: AsyncEngine,
) -> None:
    """The request-path unit of work must not keep the owner's privileges."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    unit_of_work = UnitOfWork(factory, set_role="almahmoud_app")

    async with engine.begin() as connection:
        tenant_a, tenant_b, company_a, _ = await _seed_two_tenants(connection)
    try:

        async def inspect(session: AsyncSession) -> tuple[str, list[UUID]]:
            role = (await session.execute(text("SELECT current_user"))).scalar_one()
            visible = (
                (await session.execute(select(Company.id).where(Company.tenant_id == tenant_a)))
                .scalars()
                .all()
            )
            return role, list(visible)

        role, visible = await unit_of_work.run(inspect, tenant_id=tenant_a)
        assert role == "almahmoud_app"
        assert visible == [company_a]

        async def write_other_tenant(session: AsyncSession) -> None:
            await session.execute(
                text(COMPANY_SQL),
                {
                    "id": str(new_id()),
                    "tenant_id": str(tenant_b),
                    "code": "B-2",
                    "name_ar": "شركة باء الثانية",
                },
            )

        with pytest.raises(DBAPIError):
            await unit_of_work.run(write_other_tenant, tenant_id=tenant_a)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM company WHERE tenant_id IN (:a, :b)"),
                {"a": str(tenant_a), "b": str(tenant_b)},
            )
            await connection.execute(
                text("DELETE FROM tenant WHERE id IN (:a, :b)"),
                {"a": str(tenant_a), "b": str(tenant_b)},
            )


async def test_rls_rejects_a_write_for_another_tenant(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            tenant_a, tenant_b, _, _ = await _seed_two_tenants(connection)
            await connection.execute(text("SET ROLE almahmoud_app"))
            await connection.execute(text(SET_TENANT_SQL), {"tenant_id": str(tenant_a)})

            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(COMPANY_SQL),
                    {
                        "id": str(new_id()),
                        "tenant_id": str(tenant_b),
                        "code": "B-2",
                        "name_ar": "شركة باء الثانية",
                    },
                )
        finally:
            await transaction.rollback()
