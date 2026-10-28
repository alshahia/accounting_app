"""Unit tests for the Argon2id password helpers."""

from __future__ import annotations

from argon2 import PasswordHasher

from app.modules.identity.security import (
    hash_password,
    password_needs_rehash,
    verify_password,
)


def test_hash_and_verify_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert hashed.startswith("$argon2id$")
    assert verify_password("correct horse battery staple", hashed) is True


def test_verify_rejects_wrong_password_and_none() -> None:
    hashed = hash_password("s3cret")
    assert verify_password("wrong", hashed) is False
    assert verify_password("s3cret", None) is False


def test_hashes_are_salted() -> None:
    assert hash_password("same") != hash_password("same")


def test_needs_rehash_tracks_current_parameters() -> None:
    current = hash_password("current")
    assert password_needs_rehash(current) is False

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("weak")
    assert password_needs_rehash(weak) is True
    assert verify_password("weak", weak) is True
