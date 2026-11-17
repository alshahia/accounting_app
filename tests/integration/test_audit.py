"""Audit-trail content tests: actor, time, entity and filters (phase 0, day 4)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.modules.identity.audit import record_audit
from app.shared.ids import new_id
from tests.integration.conftest import SeededIdentity, delete_identity, seed_identity

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
EVENTS_URL = "/api/v1/audit/events"
HISTORY_URL = "/api/v1/audit/login-history"


async def _login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post(LOGIN_URL, json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def test_login_history_is_self_scoped_with_actor_and_time(
    client: AsyncClient,
    identity: SeededIdentity,
) -> None:
    token = await _login(client, identity.email, identity.password)
    response = await client.get(HISTORY_URL, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items, "login must be audited"
    first = items[0]
    assert first["action"] == "auth.login_succeeded"
    assert first["actor_user_id"] == str(identity.user_id)
    assert first["created_at"]
    assert first["request_id"]
    assert first["entity_type"] is None


async def test_tenant_audit_trail_records_entity_and_filters(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    token = new_id().hex[-12:]
    seeded = await seed_identity(
        engine,
        tenant_slug=f"audit-{token}",
        email=f"auditor-{token}@example.com",
        password="audit password 123",
        role_code="auditor",
    )
    entity_id = new_id()
    try:
        access = await _login(client, seeded.email, seeded.password)

        # Write a non-authentication event with an entity, as a future domain
        # service will; the actor is the seeded user.
        async with engine.begin() as connection:
            session = AsyncSession(bind=connection, expire_on_commit=False)
            await record_audit(
                session,
                tenant_id=seeded.tenant_id,
                actor_user_id=seeded.user_id,
                action="invoice.posted",
                entity_type="sales_invoice",
                entity_id=entity_id,
                detail={"number": "INV-0001"},
                request_id="test-request",
            )

        headers = {"Authorization": f"Bearer {access}"}
        response = await client.get(EVENTS_URL, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["items"], "the login and invoice events must be listed"

        filtered = await client.get(
            EVENTS_URL,
            params={"action": "invoice.posted", "entity_type": "sales_invoice"},
            headers=headers,
        )
        assert filtered.status_code == 200
        items = filtered.json()["items"]
        assert len(items) == 1
        event = items[0]
        assert event["action"] == "invoice.posted"
        assert event["entity_type"] == "sales_invoice"
        assert event["entity_id"] == str(entity_id)
        assert event["actor_user_id"] == str(seeded.user_id)
        assert event["detail"] == {"number": "INV-0001"}
        assert event["created_at"]
        assert event["request_id"] == "test-request"

        # Pagination is echoed and bounded.
        page = await client.get(EVENTS_URL, params={"limit": 1, "offset": 0}, headers=headers)
        assert page.status_code == 200
        assert page.json()["limit"] == 1
        assert page.json()["offset"] == 0
    finally:
        await delete_identity(engine, seeded.tenant_id)
