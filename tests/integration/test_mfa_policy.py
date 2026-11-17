"""Tenant MFA enforcement policy (analysis/21 §6)."""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.identity import totp
from app.shared.ids import new_id
from tests.integration.conftest import (
    SeededIdentity,
    delete_identity,
    seed_identity,
)

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
ME_URL = "/api/v1/auth/me"
POLICY_URL = "/api/v1/auth/tenant-settings/mfa-policy"
MEMBERS_URL = "/api/v1/admin/members"
INVOICES_URL = "/api/v1/sales/invoices"


async def _identity(engine: AsyncEngine, *, role_code: str = "company_admin") -> SeededIdentity:
    suffix = new_id().hex[-12:]
    return await seed_identity(
        engine,
        tenant_slug=f"mfa-{suffix}",
        email=f"mfa-{suffix}@example.com",
        password="mfa policy password 123",
        role_code=role_code,
    )


async def _login(
    client: AsyncClient,
    identity: SeededIdentity,
    company_id: UUID,
    *totp_code: str,
) -> dict[str, str]:
    credentials: dict[str, object] = {
        "email": identity.email,
        "password": identity.password,
    }
    if totp_code:
        credentials["totp_code"] = totp_code[0]
    response = await client.post(LOGIN_URL, json=credentials)
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Company-Id": str(company_id),
    }


async def test_sensitive_actions_need_enrolled_mfa_when_policy_is_on(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    identity = await _identity(engine)
    try:
        headers = await _login(client, identity, identity.company_a)
        me = await client.get(ME_URL, headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["tenant_mfa_policy"] == {
            "mfa_sensitive_required": False,
            "mfa_posting_required": False,
        }
        members = await client.get(MEMBERS_URL, headers=headers)
        assert members.status_code == 200, members.text

        updated = await client.put(
            POLICY_URL,
            json={"mfa_sensitive_required": True, "mfa_posting_required": False},
            headers=headers,
        )
        assert updated.status_code == 200, updated.text
        blocked = await client.get(MEMBERS_URL, headers=headers)
        assert blocked.status_code == 403, blocked.text
        assert blocked.json()["code"] == "mfa_required"

        setup = await client.post("/api/v1/auth/mfa/setup", headers=headers)
        assert setup.status_code == 200, setup.text
        secret = setup.json()["secret"]
        confirmed = await client.post(
            "/api/v1/auth/mfa/confirm",
            json={"code": totp.generate_code(secret)},
            headers=headers,
        )
        assert confirmed.status_code == 200, confirmed.text

        enrolled = await _login(client, identity, identity.company_a, totp.generate_code(secret))
        allowed = await client.get(MEMBERS_URL, headers=enrolled)
        assert allowed.status_code == 200, allowed.text
    finally:
        await delete_identity(engine, identity.tenant_id)


async def test_posting_actions_follow_the_posting_switch(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    identity = await _identity(engine)
    try:
        headers = await _login(client, identity, identity.company_a)
        enabled = await client.put(
            POLICY_URL,
            json={"mfa_sensitive_required": False, "mfa_posting_required": True},
            headers=headers,
        )
        assert enabled.status_code == 200, enabled.text

        blocked = await client.post(INVOICES_URL, json={}, headers=headers)
        assert blocked.status_code == 403, blocked.text
        assert blocked.json()["code"] == "mfa_required"

        disabled = await client.put(
            POLICY_URL,
            json={"mfa_sensitive_required": False, "mfa_posting_required": False},
            headers=headers,
        )
        assert disabled.status_code == 200, disabled.text
        validation = await client.post(INVOICES_URL, json={}, headers=headers)
        assert validation.status_code == 422, validation.text
    finally:
        await delete_identity(engine, identity.tenant_id)


async def test_only_a_tenant_administrator_can_change_the_policy(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    identity = await _identity(engine, role_code="accountant")
    try:
        headers = await _login(client, identity, identity.company_a)
        denied = await client.put(
            POLICY_URL,
            json={"mfa_sensitive_required": False, "mfa_posting_required": False},
            headers=headers,
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "permission_denied"
    finally:
        await delete_identity(engine, identity.tenant_id)
