"""The auth and session contract the release ruling pins down (AC-6, AC-7, AC-8).

These tests run the real ASGI application against PostgreSQL, so they also prove
the new session-binding and lockout behaviour survives the RLS-bound request
path rather than only the service functions.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.modules.identity.models import AppUser, AuditLog
from app.shared.ids import new_id
from tests.integration.conftest import SeededIdentity

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/auth/me"


def _credentials(identity: SeededIdentity) -> dict[str, str]:
    return {"email": identity.email, "password": identity.password}


def _csrf(client: AsyncClient) -> str:
    token = client.cookies.get("csrf_token")
    assert token is not None, "the CSRF cookie is missing"
    return token


async def _login(client: AsyncClient, identity: SeededIdentity) -> Response:
    response = await client.post(LOGIN_URL, json=_credentials(identity))
    assert response.status_code == 200, response.text
    return response


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_security_headers_on_public_and_error_responses(client: AsyncClient) -> None:
    public = await client.get("/api/v1/health")
    unauthorized = await client.get(ME_URL)

    for response in (public, unauthorized):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        policy = response.headers["content-security-policy"]
        assert "default-src 'none'" in policy
        assert "frame-ancestors 'none'" in policy
        # The test deployment runs over plain HTTP; HSTS there would poison a
        # developer's browser for every later local project.
        assert "strict-transport-security" not in response.headers

    assert unauthorized.status_code == 401
    assert unauthorized.headers["content-type"].startswith("application/problem+json")


async def test_logout_without_csrf_is_refused_and_with_csrf_revokes_the_family(
    client: AsyncClient,
    identity: SeededIdentity,
) -> None:
    login = await _login(client, identity)
    access_token = login.json()["access_token"]
    refresh_token = client.cookies.get("refresh_token")
    assert refresh_token is not None

    assert (await client.get(ME_URL, headers=_auth(access_token))).status_code == 200

    refused = await client.post(LOGOUT_URL)
    assert refused.status_code == 403
    assert refused.json()["code"] == "csrf_failed"

    accepted = await client.post(LOGOUT_URL, headers={"X-CSRF-Token": _csrf(client)})
    assert accepted.status_code == 204

    # The access token was bound to the revoked session, so it dies with it
    # instead of staying usable for the rest of its fifteen minutes.
    dead = await client.get(ME_URL, headers=_auth(access_token))
    assert dead.status_code == 401
    assert dead.json()["code"] == "not_authenticated"

    replay_client = client
    replay_client.cookies.clear()
    replay_client.cookies.set("refresh_token", refresh_token, path="/api/v1/auth")
    replay_client.cookies.set("csrf_token", "self-consistent", path="/api/v1/auth")
    replay = await replay_client.post(REFRESH_URL, headers={"X-CSRF-Token": "self-consistent"})
    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_refresh_token"


async def test_refresh_rotation_invalidates_the_previous_access_token(
    client: AsyncClient,
    identity: SeededIdentity,
) -> None:
    login = await _login(client, identity)
    old_access = login.json()["access_token"]

    rotated = await client.post(REFRESH_URL, headers={"X-CSRF-Token": _csrf(client)})
    assert rotated.status_code == 200, rotated.text
    new_access = rotated.json()["access_token"]
    assert new_access != old_access

    assert (await client.get(ME_URL, headers=_auth(new_access))).status_code == 200
    old = await client.get(ME_URL, headers=_auth(old_access))
    assert old.status_code == 401
    assert old.json()["code"] == "not_authenticated"


async def test_forged_session_id_is_rejected(
    client: AsyncClient,
    identity: SeededIdentity,
    settings: Settings,
) -> None:
    login = await _login(client, identity)
    token = login.json()["access_token"]

    decoded = jwt.decode(token, options={"verify_signature": False})
    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "sub": decoded["sub"],
            "tid": decoded["tid"],
            "sid": str(new_id()),
            "typ": "access",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "jti": "forged",
        },
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    response = await client.get(ME_URL, headers=_auth(forged))
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


async def test_two_concurrent_refreshes_leave_one_winner_and_no_live_session(
    client: AsyncClient,
    identity: SeededIdentity,
) -> None:
    await _login(client, identity)
    refresh_token = client.cookies.get("refresh_token")
    csrf_token = _csrf(client)
    assert refresh_token is not None

    client.cookies.clear()
    request_headers = {
        "X-CSRF-Token": csrf_token,
        "Cookie": f"refresh_token={refresh_token}; csrf_token={csrf_token}",
    }
    responses = await asyncio.gather(
        client.post(REFRESH_URL, headers=request_headers),
        client.post(REFRESH_URL, headers=request_headers),
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 401], [response.text for response in responses]

    # A concurrent rotation is treated as a replay: the whole family is burnt,
    # including whichever session the winner had just minted.
    winner_refresh = next(
        response.cookies.get("refresh_token")
        for response in responses
        if response.status_code == 200
    )
    assert winner_refresh is not None
    replay_headers = {
        "X-CSRF-Token": csrf_token,
        "Cookie": f"refresh_token={winner_refresh}; csrf_token={csrf_token}",
    }
    after = await client.post(REFRESH_URL, headers=replay_headers)
    assert after.status_code == 401


async def test_twenty_failed_logins_from_one_address_return_429(client: AsyncClient) -> None:
    for index in range(20):
        response = await client.post(
            LOGIN_URL,
            json={"email": f"ghost-{index}@example.com", "password": "definitely wrong"},
        )
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "unknown_email"

    throttled = await client.post(
        LOGIN_URL,
        json={"email": "ghost-final@example.com", "password": "definitely wrong"},
    )
    assert throttled.status_code == 429
    assert throttled.json()["code"] == "rate_limited"


async def test_one_hundred_wrong_totp_codes_lock_the_account(
    client: AsyncClient,
    identity: SeededIdentity,
    engine: AsyncEngine,
    settings: Settings,
) -> None:
    from app.modules.identity import totp

    login = await _login(client, identity)
    headers = _auth(login.json()["access_token"])
    setup = await client.post("/api/v1/auth/mfa/setup", headers=headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    confirmed = await client.post(
        "/api/v1/auth/mfa/confirm",
        headers=headers,
        json={"code": totp.generate_code(secret)},
    )
    assert confirmed.status_code == 200, confirmed.text

    client.cookies.clear()
    codes: list[str] = []
    for _ in range(100):
        response = await client.post(
            LOGIN_URL,
            json={**_credentials(identity), "totp_code": "000000"},
        )
        codes.append(response.json()["code"])
        assert response.status_code in (401, 429), response.text

    assert "totp_invalid" in codes
    assert "account_locked" in codes
    assert codes.count("totp_invalid") == settings.max_failed_logins

    async with engine.connect() as connection:
        locked_until = await connection.scalar(
            select(AppUser.locked_until).where(AppUser.id == identity.user_id)
        )
        assert locked_until is not None
        mfa_failures = await connection.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.tenant_id == identity.tenant_id,
                AuditLog.action == "auth.mfa_failed",
            )
        )
    assert mfa_failures == settings.max_failed_logins
