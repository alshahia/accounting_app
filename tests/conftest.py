"""Shared test fixtures."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app

# Local development trusts the `postgres` role on port 55432 (deploy/dev-postgres.ps1).
# CI provides its own login role, so the URL is overridable by environment.
TEST_DATABASE_URL = os.environ.get(
    "MAHMOUD_TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres@127.0.0.1:55432/almahmoud_test",
)


@pytest.fixture
def settings() -> Settings:
    """Settings for a test application; the database URL points at almahmoud_test."""
    return Settings(
        environment="test",
        database_url=SecretStr(TEST_DATABASE_URL),
        log_level="WARNING",
        jwt_secret=SecretStr("test-secret-key-for-unit-and-integration-tests"),
        cookie_secure=False,
        max_failed_logins=3,
        lockout_minutes=1,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to a fresh application with its lifespan running."""
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield http_client
