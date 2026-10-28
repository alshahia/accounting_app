"""Access and refresh tokens (report 21 §5-§6).

Access tokens are short-lived signed JWTs carrying the user and tenant. Refresh
tokens are opaque 384-bit secrets; only their SHA-256 hash is stored, so a
database leak does not leak usable tokens. Rotation and reuse detection live in
the identity service.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from jwt import exceptions as jwt_exceptions

from app.config import Settings

ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - a token type label, not a password
CREDENTIAL_TOKEN_TYPE = "credential"  # noqa: S105 - a token type label, not a password
REFRESH_TOKEN_BYTES = 48
INVITE_PURPOSE = "invite"
RESET_PURPOSE = "reset"
CREDENTIAL_PURPOSES = frozenset({INVITE_PURPOSE, RESET_PURPOSE})


class TokenError(Exception):
    """A token is malformed, expired or of the wrong kind."""


@dataclass(frozen=True)
class AccessTokenClaims:
    """Validated claims the API needs from an access token."""

    user_id: UUID
    tenant_id: UUID
    session_id: UUID
    token_id: str
    expires_at: datetime


def create_access_token(
    *,
    settings: Settings,
    user_id: UUID,
    tenant_id: UUID,
    session_id: UUID,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Return a signed access token and its expiry time.

    The sid claim binds the access token to the refresh session that minted it,
    so revoking that session (logout, replay detection, an administrator) kills
    every access token derived from it instead of leaving a 15-minute window.
    """
    issued_at = now if now is not None else datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.access_token_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "sid": str(session_id),
        "typ": ACCESS_TOKEN_TYPE,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_hex(16),
    }
    encoded = jwt.encode(  # pyright: ignore[reportUnknownMemberType] - PyJWT stubs use Unknown key types
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return encoded, expires_at


def decode_access_token(token: str, settings: Settings) -> AccessTokenClaims:
    """Validate an access token and return its claims."""
    try:
        payload = jwt.decode(  # pyright: ignore[reportUnknownMemberType] - PyJWT stubs use Unknown key types
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub", "tid", "sid", "typ"]},
        )
    except jwt_exceptions.PyJWTError as exc:
        raise TokenError("invalid or expired access token") from exc

    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise TokenError("wrong token type")
    try:
        user_id = UUID(str(payload["sub"]))
        tenant_id = UUID(str(payload["tid"]))
        session_id = UUID(str(payload["sid"]))
        expires_at = datetime.fromtimestamp(float(payload["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("invalid access token claims") from exc
    return AccessTokenClaims(
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=session_id,
        token_id=str(payload.get("jti", "")),
        expires_at=expires_at,
    )


@dataclass(frozen=True)
class CredentialClaims:
    """The user, tenant, purpose and password epoch a one-time link was issued for."""

    user_id: UUID
    tenant_id: UUID
    purpose: str
    epoch: int
    expires_at: datetime


def create_credential_token(
    *,
    settings: Settings,
    user_id: UUID,
    tenant_id: UUID,
    purpose: str,
    epoch: int,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Return a signed single-use invitation or password-reset token and its expiry."""
    if purpose not in CREDENTIAL_PURPOSES:
        raise ValueError(f"not a credential purpose: {purpose!r}")
    issued_at = now if now is not None else datetime.now(UTC)
    expires_at = issued_at + timedelta(hours=settings.credential_token_hours)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "pur": purpose,
        "ep": epoch,
        "typ": CREDENTIAL_TOKEN_TYPE,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    encoded = jwt.encode(  # pyright: ignore[reportUnknownMemberType] - PyJWT stubs use Unknown key types
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return encoded, expires_at


def decode_credential_token(token: str, settings: Settings) -> CredentialClaims:
    """Validate a credential link and return its claims."""
    try:
        payload = jwt.decode(  # pyright: ignore[reportUnknownMemberType] - PyJWT stubs use Unknown key types
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub", "tid", "pur", "ep", "typ"]},
        )
    except jwt_exceptions.PyJWTError as exc:
        raise TokenError("invalid or expired credential link") from exc
    if payload.get("typ") != CREDENTIAL_TOKEN_TYPE:
        raise TokenError("wrong token type")
    try:
        user_id = UUID(str(payload["sub"]))
        tenant_id = UUID(str(payload["tid"]))
        purpose = str(payload["pur"])
        epoch = int(payload["ep"])
        expires_at = datetime.fromtimestamp(float(payload["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("invalid credential token claims") from exc
    if purpose not in CREDENTIAL_PURPOSES:
        raise TokenError("unknown credential purpose")
    return CredentialClaims(
        user_id=user_id,
        tenant_id=tenant_id,
        purpose=purpose,
        epoch=epoch,
        expires_at=expires_at,
    )


def new_refresh_token() -> tuple[str, str]:
    """Return a new opaque refresh token and its stored hash."""
    raw = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    """Hash a refresh token for storage and lookup."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def refresh_token_expiry(settings: Settings, now: datetime | None = None) -> datetime:
    """Return the absolute expiry for a newly issued refresh token."""
    issued_at = now if now is not None else datetime.now(UTC)
    return issued_at + timedelta(days=settings.refresh_token_days)
