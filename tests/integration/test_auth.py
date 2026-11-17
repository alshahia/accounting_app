"""End-to-end authentication tests against PostgreSQL (phase 0, day 3).

The tests exercise the real ASGI app, the real database and real Argon2 hashing:
login, tenant disambiguation, lockout, /me company scoping, refresh rotation,
refresh-token replay detection and logout revocation.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.identity.models import AuditLog
from app.shared.ids import new_id
from tests.integration.conftest import SeededIdentity, delete_identity, seed_identity

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/auth/me"
COOKIE_PATH = "/api/v1/auth"


def _credentials(identity: SeededIdentity, tenant: str | None = None) -> dict[str, str]:
    payload = {"email": identity.email, "password": identity.password}
    if tenant is not None:
        payload["tenant"] = tenant
    return payload


def _csrf(client: AsyncClient) -> str:
    token = client.cookies.get("csrf_token")
    assert token is not None, "the CSRF cookie is missing"
    return token


def _replace_cookie(client: AsyncClient, name: str, value: str) -> None:
    client.cookies.delete(name, path=COOKIE_PATH)
    client.cookies.set(name, value, path=COOKIE_PATH)


async def _login(
    client: AsyncClient,
    identity: SeededIdentity,
    tenant: str | None = None,
) -> Response:
    return await client.post(LOGIN_URL, json=_credentials(identity, tenant))


async def _audit_counts(engine: AsyncEngine, identity: SeededIdentity) -> dict[str, int]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(AuditLog.action, func.count())
                .where(AuditLog.tenant_id == identity.tenant_id)
                .group_by(AuditLog.action)
            )
        ).all()
    return {str(action): int(count) for action, count in rows}


async def test_login_me_and_company_scope(
    client: AsyncClient,
    identity: SeededIdentity,
) -> None:
    response = await _login(client, identity)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == identity.email
    assert body["user"]["tenant_id"] == str(identity.tenant_id)
    assert client.cookies.get("refresh_token")
    assert client.cookies.get("csrf_token")

    me = await client.get(
        ME_URL,
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200, me.text
    payload = me.json()
    assert payload["user"]["id"] == str(identity.user_id)
    companies = payload["companies"]
    assert [company["code"] for company in companies] == ["A"]
    company = companies[0]
    assert company["id"] == str(identity.company_a)
    assert company["id"] != str(identity.company_b)
    assert company["role_codes"] == ["accountant"]
    assert "ledger.journal.post" in company["permissions"]
    assert "admin.user.manage" not in company["permissions"]


async def test_me_requires_a_valid_access_token(client: AsyncClient) -> None:
    missing = await client.get(ME_URL)
    assert missing.status_code == 401
    assert missing.json()["code"] == "not_authenticated"

    invalid = await client.get(ME_URL, headers={"Authorization": "Bearer not-a-token"})
    assert invalid.status_code == 401
    assert invalid.json()["code"] == "not_authenticated"


async def test_unknown_email_is_distinguished_from_a_wrong_password(
    client: AsyncClient,
    identity: SeededIdentity,
) -> None:
    # The installer's helpdesk needs to tell "no such account" from "wrong
    # password"; the response time stays equal because the dummy hash is still
    # verified for an unknown address.
    response = await client.post(
        LOGIN_URL,
        json={"email": "nobody@example.com", "password": identity.password},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "unknown_email"


async def test_repeated_failures_lock_the_account(
    client: AsyncClient,
    identity: SeededIdentity,
    engine: AsyncEngine,
) -> None:
    for _ in range(3):
        response = await client.post(
            LOGIN_URL,
            json={"email": identity.email, "password": "definitely wrong"},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_credentials"

    locked = await _login(client, identity)
    assert locked.status_code == 429
    assert locked.json()["code"] == "account_locked"
    assert client.cookies.get("refresh_token") is None

    counts = await _audit_counts(engine, identity)
    assert counts.get("auth.login_failed") == 3
    assert counts.get("auth.login_locked") == 1


async def test_ambiguous_email_requires_tenant(
    client: AsyncClient,
    identity: SeededIdentity,
    engine: AsyncEngine,
) -> None:
    other = await seed_identity(
        engine,
        tenant_slug=f"other-{new_id().hex[-12:]}",
        email=identity.email,
        password=identity.password,
    )
    try:
        ambiguous = await _login(client, identity)
        assert ambiguous.status_code == 400
        assert ambiguous.json()["code"] == "tenant_required"

        scoped = await _login(client, identity, tenant=identity.tenant_slug)
        assert scoped.status_code == 200, scoped.text
        assert scoped.json()["user"]["tenant_id"] == str(identity.tenant_id)

        # The same email is a separate account in the other tenant.
        other_login = await _login(client, identity, tenant=other.tenant_slug)
        assert other_login.status_code == 200, other_login.text
        assert other_login.json()["user"]["tenant_id"] == str(other.tenant_id)

        unknown_tenant = await client.post(
            LOGIN_URL,
            json={
                "email": identity.email,
                "password": identity.password,
                "tenant": "no-such-tenant",
            },
        )
        assert unknown_tenant.status_code == 401
        assert unknown_tenant.json()["code"] == "unknown_email"
    finally:
        await delete_identity(engine, other.tenant_id)


async def test_refresh_rotates_and_replay_revokes_the_family(
    client: AsyncClient,
    identity: SeededIdentity,
    engine: AsyncEngine,
) -> None:
    first = await _login(client, identity)
    assert first.status_code == 200, first.text
    old_refresh = client.cookies.get("refresh_token")
    assert old_refresh is not None

    rotated = await client.post(REFRESH_URL, headers={"X-CSRF-Token": _csrf(client)})
    assert rotated.status_code == 200, rotated.text
    new_refresh = client.cookies.get("refresh_token")
    assert new_refresh is not None
    assert new_refresh != old_refresh

    # Replaying the old token must fail and revoke the whole family.
    _replace_cookie(client, "refresh_token", old_refresh)
    replay = await client.post(REFRESH_URL, headers={"X-CSRF-Token": _csrf(client)})
    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_refresh_token"

    # Even the newest token of the family is now revoked.
    _replace_cookie(client, "refresh_token", new_refresh)
    after_replay = await client.post(REFRESH_URL, headers={"X-CSRF-Token": _csrf(client)})
    assert after_replay.status_code == 401
    assert after_replay.json()["code"] == "invalid_refresh_token"

    counts = await _audit_counts(engine, identity)
    assert counts.get("auth.refresh_rotated") == 1
    # Both replays hit a revoked row and are audited as reuse.
    assert counts.get("auth.refresh_reuse") == 2


async def test_refresh_requires_a_matching_csrf_token(
    client: AsyncClient,
    identity: SeededIdentity,
) -> None:
    assert (await _login(client, identity)).status_code == 200

    missing = await client.post(REFRESH_URL)
    assert missing.status_code == 403
    assert missing.json()["code"] == "csrf_failed"

    mismatch = await client.post(
        REFRESH_URL,
        headers={"X-CSRF-Token": "not-the-cookie-value"},
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["code"] == "csrf_failed"


async def test_logout_revokes_the_refresh_session(
    client: AsyncClient,
    identity: SeededIdentity,
    engine: AsyncEngine,
) -> None:
    assert (await _login(client, identity)).status_code == 200
    refresh_token = client.cookies.get("refresh_token")
    assert refresh_token is not None

    logout = await client.post(LOGOUT_URL, headers={"X-CSRF-Token": _csrf(client)})
    assert logout.status_code == 204
    assert client.cookies.get("refresh_token") is None
    assert client.cookies.get("csrf_token") is None

    # The server-side row is revoked, so replaying the token fails even when a
    # client supplies a self-consistent CSRF pair.
    _replace_cookie(client, "refresh_token", refresh_token)
    _replace_cookie(client, "csrf_token", "self-consistent")
    replay = await client.post(REFRESH_URL, headers={"X-CSRF-Token": "self-consistent"})
    assert replay.status_code == 401

    counts = await _audit_counts(engine, identity)
    assert counts.get("auth.logout") == 1


async def _access_token(client: AsyncClient, identity: SeededIdentity) -> str:
    response = await _login(client, identity)
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    assert isinstance(token, str)
    return token


async def test_mfa_enrolment_requires_codes_at_login(
    client: AsyncClient,
    identity: SeededIdentity,
) -> None:
    from app.modules.identity import totp

    token = await _access_token(client, identity)
    headers = {"Authorization": f"Bearer {token}"}

    setup = await client.post("/api/v1/auth/mfa/setup", headers=headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert setup.json()["provisioning_uri"].startswith("otpauth://totp/")

    confirmed = await client.post(
        "/api/v1/auth/mfa/confirm",
        headers=headers,
        json={"code": totp.generate_code(secret)},
    )
    assert confirmed.status_code == 200, confirmed.text
    recovery_codes = confirmed.json()["codes"]
    assert len(recovery_codes) == 10
    assert len(set(recovery_codes)) == 10

    conflict = await client.post("/api/v1/auth/mfa/setup", headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "totp_already_enabled"

    client.cookies.clear()
    missing = await _login(client, identity)
    assert missing.status_code == 401, missing.text
    assert missing.json()["code"] == "totp_required"
    assert client.cookies.get("refresh_token") is None

    wrong = await client.post(
        LOGIN_URL,
        json={**_credentials(identity), "totp_code": "000000"},
    )
    assert wrong.status_code == 401, wrong.text
    assert wrong.json()["code"] == "totp_invalid"

    code = totp.generate_code(secret)
    accepted = await client.post(
        LOGIN_URL,
        json={**_credentials(identity), "totp_code": code},
    )
    assert accepted.status_code == 200, accepted.text
    assert client.cookies.get("refresh_token")

    replay = await client.post(
        LOGIN_URL,
        json={**_credentials(identity), "totp_code": code},
    )
    assert replay.status_code == 401, replay.text
    assert replay.json()["code"] == "totp_invalid"


async def test_recovery_codes_are_single_use_and_rotatable(
    client: AsyncClient,
    identity: SeededIdentity,
) -> None:
    from app.modules.identity import totp

    token = await _access_token(client, identity)
    headers = {"Authorization": f"Bearer {token}"}
    setup = await client.post("/api/v1/auth/mfa/setup", headers=headers)
    secret = setup.json()["secret"]
    confirmed = await client.post(
        "/api/v1/auth/mfa/confirm",
        headers=headers,
        json={"code": totp.generate_code(secret)},
    )
    assert confirmed.status_code == 200, confirmed.text
    codes = confirmed.json()["codes"]
    assert len(codes) == 10
    assert len(set(codes)) == 10

    client.cookies.clear()
    accepted = await client.post(
        LOGIN_URL,
        json={**_credentials(identity), "recovery_code": f"{codes[0][:4]}-{codes[0][4:]}"},
    )
    assert accepted.status_code == 200, accepted.text
    assert client.cookies.get("refresh_token")

    replay = await client.post(
        LOGIN_URL,
        json={**_credentials(identity), "recovery_code": codes[0]},
    )
    assert replay.status_code == 401, replay.text
    assert replay.json()["code"] == "totp_invalid"

    client.cookies.clear()
    second_login = await client.post(
        LOGIN_URL,
        json={**_credentials(identity), "recovery_code": codes[1]},
    )
    assert second_login.status_code == 200, second_login.text
    fresh_token = second_login.json()["access_token"]
    rotated = await client.post(
        "/api/v1/auth/mfa/recovery-codes",
        headers={"Authorization": f"Bearer {fresh_token}"},
        json={"password": identity.password, "code": totp.generate_code(secret)},
    )
    assert rotated.status_code == 200, rotated.text
    replacement = rotated.json()["codes"]
    assert len(replacement) == 10
    assert set(replacement).isdisjoint(codes)

    client.cookies.clear()
    stale = await client.post(
        LOGIN_URL,
        json={**_credentials(identity), "recovery_code": codes[2]},
    )
    assert stale.status_code == 401, stale.text
    current = await client.post(
        LOGIN_URL,
        json={**_credentials(identity), "recovery_code": replacement[0]},
    )
    assert current.status_code == 200, current.text


async def test_mfa_can_be_disabled_with_password_and_code(
    client: AsyncClient,
    identity: SeededIdentity,
) -> None:
    from app.modules.identity import totp

    token = await _access_token(client, identity)
    headers = {"Authorization": f"Bearer {token}"}
    setup = await client.post("/api/v1/auth/mfa/setup", headers=headers)
    secret = setup.json()["secret"]
    code = totp.generate_code(secret)
    assert (
        await client.post("/api/v1/auth/mfa/confirm", headers=headers, json={"code": code})
    ).status_code == 200

    wrong = await client.post(
        "/api/v1/auth/mfa/disable",
        headers=headers,
        json={"password": "wrong password", "code": code},
    )
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "invalid_credentials"

    disabled = await client.post(
        "/api/v1/auth/mfa/disable",
        headers=headers,
        json={"password": identity.password, "code": code},
    )
    assert disabled.status_code == 204, disabled.text

    client.cookies.clear()
    plain = await _login(client, identity)
    assert plain.status_code == 200, plain.text


async def test_sessions_can_be_listed_and_revoked(
    client: AsyncClient,
    identity: SeededIdentity,
) -> None:
    token = await _access_token(client, identity)
    headers = {"Authorization": f"Bearer {token}"}

    listed = await client.get("/api/v1/auth/sessions", headers=headers)
    assert listed.status_code == 200, listed.text
    sessions = listed.json()
    assert len(sessions) == 1
    session_id = sessions[0]["id"]
    assert sessions[0]["revoked_at"] is None

    revoked = await client.delete(f"/api/v1/auth/sessions/{session_id}", headers=headers)
    assert revoked.status_code == 204, revoked.text

    # The access token was bound to that session, so revoking the session ends
    # the token too rather than letting it run out its remaining minutes.
    dead = await client.get("/api/v1/auth/sessions", headers=headers)
    assert dead.status_code == 401
    assert dead.json()["code"] == "not_authenticated"

    fresh = await _access_token(client, identity)
    fresh_headers = {"Authorization": f"Bearer {fresh}"}
    again = await client.get("/api/v1/auth/sessions", headers=fresh_headers)
    revoked_rows = [row for row in again.json() if row["id"] == session_id]
    assert len(revoked_rows) == 1
    assert revoked_rows[0]["revoked_at"] is not None

    missing = await client.delete(f"/api/v1/auth/sessions/{new_id()}", headers=fresh_headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "session_not_found"
