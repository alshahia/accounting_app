"""Permission-matrix and company data-scope tests (phase 0, day 4).

The matrix seeds one user per default role and checks that the audit trail is
readable only by roles holding `audit.view`, that the company-scoped admin
endpoint enforces `admin.user.manage` per selected company, and that a company
the caller does not belong to is a 403 — not a probe of another tenant's rows.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.shared.ids import new_id
from tests.integration.conftest import SeededIdentity, delete_identity, seed_identity

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
AUDIT_URL = "/api/v1/audit/events"
MEMBERS_URL = "/api/v1/admin/members"

# Which default roles may read the tenant-wide audit trail (audit.view).
AUDIT_MATRIX = {
    "tenant_admin": 200,
    "auditor": 200,
    "accountant": 403,
    "sales": 403,
    "purchasing": 403,
}

# Which default roles may administer company members (admin.user.manage).
MEMBERS_MATRIX = {
    "tenant_admin": 200,
    "company_admin": 200,
    "auditor": 403,
    "accountant": 403,
    "sales": 403,
    "purchasing": 403,
}


async def _seed(engine: AsyncEngine, role_code: str) -> SeededIdentity:
    token = new_id().hex[-12:]
    return await seed_identity(
        engine,
        tenant_slug=f"perm-{role_code}-{token}",
        email=f"{role_code}-{token}@example.com",
        password="matrix password 123",
        role_code=role_code,
    )


async def _login(client: AsyncClient, identity: SeededIdentity) -> dict[str, str]:
    response = await client.post(
        LOGIN_URL,
        json={"email": identity.email, "password": identity.password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_audit_trail_permission_matrix(client: AsyncClient, engine: AsyncEngine) -> None:
    for role_code, expected in AUDIT_MATRIX.items():
        seeded = await _seed(engine, role_code)
        try:
            response = await client.get(AUDIT_URL, headers=await _login(client, seeded))
            assert response.status_code == expected, (role_code, response.text)
            if expected == 403:
                assert response.json()["code"] == "permission_denied"
        finally:
            await delete_identity(engine, seeded.tenant_id)


async def test_admin_members_permission_matrix(client: AsyncClient, engine: AsyncEngine) -> None:
    for role_code, expected in MEMBERS_MATRIX.items():
        seeded = await _seed(engine, role_code)
        try:
            response = await client.get(
                MEMBERS_URL,
                headers={
                    **(await _login(client, seeded)),
                    "X-Company-Id": str(seeded.company_a),
                },
            )
            assert response.status_code == expected, (role_code, response.text)
            if expected == 403:
                assert response.json()["code"] == "permission_denied"
        finally:
            await delete_identity(engine, seeded.tenant_id)


async def test_admin_members_company_scope(client: AsyncClient, engine: AsyncEngine) -> None:
    seeded = await _seed(engine, "tenant_admin")
    try:
        headers = await _login(client, seeded)

        missing = await client.get(MEMBERS_URL, headers=headers)
        assert missing.status_code == 400
        assert missing.json()["code"] == "company_required"

        invalid = await client.get(MEMBERS_URL, headers={**headers, "X-Company-Id": "nope"})
        assert invalid.status_code == 400
        assert invalid.json()["code"] == "invalid_company"

        own = await client.get(
            MEMBERS_URL,
            headers={**headers, "X-Company-Id": str(seeded.company_a)},
        )
        assert own.status_code == 200, own.text
        members = own.json()
        assert [member["email"] for member in members] == [seeded.email]
        assert members[0]["user_id"] == str(seeded.user_id)
        assert members[0]["role_codes"] == ["tenant_admin"]

        # Company B exists in the same tenant but the user is not a member.
        foreign = await client.get(
            MEMBERS_URL,
            headers={**headers, "X-Company-Id": str(seeded.company_b)},
        )
        assert foreign.status_code == 403
        assert foreign.json()["code"] == "company_not_allowed"

        # A valid UUID that belongs to no company is the same denial.
        unknown = await client.get(
            MEMBERS_URL,
            headers={**headers, "X-Company-Id": str(new_id())},
        )
        assert unknown.status_code == 403
        assert unknown.json()["code"] == "company_not_allowed"
    finally:
        await delete_identity(engine, seeded.tenant_id)


async def test_protected_endpoints_require_login(client: AsyncClient) -> None:
    for url in (AUDIT_URL, MEMBERS_URL, "/api/v1/audit/login-history"):
        response = await client.get(url)
        assert response.status_code == 401, url
        assert response.json()["code"] == "not_authenticated"
