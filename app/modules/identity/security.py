"""Password hashing with Argon2id (report 21 §6).

Only this module hashes or verifies passwords; the rest of the code never sees a
plaintext password once it leaves the request model. Parameters are tuned for an
interactive login on a small server; a rehash check lets a future parameter
change migrate hashes on next login.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.shared.errors import AppError

# A floor for operator-set and self-service passwords. Length is the strongest
# single factor; the demo fixture and tests use longer values anyway.
MIN_PASSWORD_LENGTH = 10

# 19 MiB, 3 iterations, 1 lane: OWASP's lower-memory recommendation for Argon2id.
_HASHER = PasswordHasher(time_cost=3, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16)


def hash_password(password: str) -> str:
    """Return an Argon2id hash with a fresh random salt."""
    return _HASHER.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Return True only when the password matches the stored Argon2id hash."""
    if not password_hash:
        return False
    try:
        return _HASHER.verify(password_hash, password)
    except VerificationError, InvalidHashError:
        return False


def validate_password_strength(password: str) -> None:
    """Reject an obviously weak password with a 422 the client can display."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AppError(
            code="weak_password",
            title="Unprocessable entity",
            status_code=422,
            detail=f"The password must be at least {MIN_PASSWORD_LENGTH} characters long.",
        )
    if password.strip() != password or not any(ch.isdigit() for ch in password):
        raise AppError(
            code="weak_password",
            title="Unprocessable entity",
            status_code=422,
            detail="The password must contain a digit and no leading or trailing spaces.",
        )


def password_needs_rehash(password_hash: str) -> bool:
    """True when the stored hash predates the current parameters."""
    try:
        return _HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True
