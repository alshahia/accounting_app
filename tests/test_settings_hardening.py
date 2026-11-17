"""Deployment-secret hardening (ruling AC-8)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.config import MIN_PRODUCTION_SECRET_BYTES, Settings


def test_production_rejects_an_empty_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", jwt_secret=SecretStr(""))


def test_production_rejects_a_short_secret() -> None:
    short = "x" * (MIN_PRODUCTION_SECRET_BYTES - 1)
    with pytest.raises(ValidationError):
        Settings(environment="production", jwt_secret=SecretStr(short))


def test_production_accepts_a_secret_at_the_minimum_length() -> None:
    settings = Settings(
        environment="production",
        jwt_secret=SecretStr("x" * MIN_PRODUCTION_SECRET_BYTES),
        bootstrap_token=SecretStr("b" * MIN_PRODUCTION_SECRET_BYTES),
    )
    assert len(settings.jwt_secret.get_secret_value()) == MIN_PRODUCTION_SECRET_BYTES


def test_production_requires_a_bootstrap_token() -> None:
    # A production process without the token could not provision its first
    # tenant, and an unprotected setup screen is not shippable, so startup fails.
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            jwt_secret=SecretStr("x" * MIN_PRODUCTION_SECRET_BYTES),
        )
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            jwt_secret=SecretStr("x" * MIN_PRODUCTION_SECRET_BYTES),
            bootstrap_token=SecretStr("short"),
        )


def test_weak_secrets_are_tolerated_outside_production() -> None:
    # Local development and tests must stay runnable without ceremony; the
    # guard is a deployment gate, not a developer gate.
    assert Settings(environment="development", jwt_secret=SecretStr("short")).environment == (
        "development"
    )
