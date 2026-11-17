"""Unit tests for access-token creation and validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.modules.identity.tokens import (
    TokenError,
    create_access_token,
    decode_access_token,
    hash_refresh_token,
    new_refresh_token,
)


@pytest.fixture
def token_settings() -> Settings:
    return Settings(
        environment="test",
        jwt_secret=SecretStr("unit-test-secret-that-is-long-enough-1234"),
        access_token_minutes=15,
    )


def test_access_token_roundtrip(token_settings: Settings) -> None:
    user_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    token, expires_at = create_access_token(
        settings=token_settings,
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=session_id,
    )
    claims = decode_access_token(token, token_settings)
    assert claims.user_id == user_id
    assert claims.tenant_id == tenant_id
    assert claims.session_id == session_id
    assert claims.expires_at == expires_at.replace(microsecond=0)


def test_tampered_token_is_rejected(token_settings: Settings) -> None:
    token, _ = create_access_token(
        settings=token_settings,
        user_id=uuid4(),
        tenant_id=uuid4(),
        session_id=uuid4(),
    )
    tampered = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
    with pytest.raises(TokenError):
        decode_access_token(tampered, token_settings)


def test_expired_token_is_rejected(token_settings: Settings) -> None:
    token, _ = create_access_token(
        settings=token_settings,
        user_id=uuid4(),
        tenant_id=uuid4(),
        session_id=uuid4(),
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    with pytest.raises(TokenError):
        decode_access_token(token, token_settings)


def test_token_signed_with_another_secret_is_rejected(token_settings: Settings) -> None:
    token, _ = create_access_token(
        settings=Settings(
            environment="test",
            jwt_secret=SecretStr("other-secret-that-is-long-enough-12345"),
        ),
        user_id=uuid4(),
        tenant_id=uuid4(),
        session_id=uuid4(),
    )
    with pytest.raises(TokenError):
        decode_access_token(token, token_settings)


def test_refresh_tokens_are_random_and_hashed() -> None:
    first, first_hash = new_refresh_token()
    second, _second_hash = new_refresh_token()
    assert first != second
    assert first_hash == hash_refresh_token(first)
    assert first_hash != first
    assert first_hash == hash_refresh_token(first)
