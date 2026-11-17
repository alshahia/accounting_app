"""Deployment settings behaviour."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAHMOUD_ENVIRONMENT", "production")
    monkeypatch.setenv("MAHMOUD_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("MAHMOUD_JWT_SECRET", "production-secret-that-is-long-enough-123456")
    monkeypatch.setenv("MAHMOUD_BOOTSTRAP_TOKEN", "bootstrap-token-that-is-long-enough-123456")

    settings = Settings()

    assert settings.environment == "production"
    assert settings.log_level == "ERROR"
    assert settings.docs_are_enabled is False
    assert settings.cookies_are_secure is True


def test_production_rejects_the_development_jwt_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production")
    with pytest.raises(ValidationError):
        Settings(
            environment="production", jwt_secret=SecretStr("dev-only-insecure-secret-change-me")
        )

    settings = Settings(
        environment="production",
        jwt_secret=SecretStr("x" * 48),
        bootstrap_token=SecretStr("b" * 48),
    )
    assert settings.docs_are_enabled is False


def test_docs_are_enabled_outside_production() -> None:
    assert Settings(environment="development").docs_are_enabled is True


def test_database_url_is_a_secret() -> None:
    secret_url = "postgresql+asyncpg://user:topsecret@host:5432/db"
    settings = Settings(database_url=SecretStr(secret_url))

    assert "topsecret" not in repr(settings)
    assert settings.database_url.get_secret_value() == secret_url
