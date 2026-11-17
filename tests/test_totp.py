"""RFC 6238 vectors and helpers for the TOTP second factor (phase 1)."""

from __future__ import annotations

from app.modules.identity import totp

# Base32 of the RFC 6238 SHA-1 seed "12345678901234567890".
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_rfc6238_vectors_match_for_six_digits() -> None:
    assert totp.generate_code(RFC_SECRET, now=59.0) == "287082"
    assert totp.generate_code(RFC_SECRET, now=1111111109.0) == "081804"
    assert totp.generate_code(RFC_SECRET, now=1234567890.0) == "005924"


def test_match_accepts_one_step_either_side() -> None:
    code = totp.generate_code(RFC_SECRET, now=120.0)
    assert totp.match_counter(RFC_SECRET, code, now=120.0) == 4
    assert totp.match_counter(RFC_SECRET, code, now=120.0 + 30.0) == 4
    assert totp.match_counter(RFC_SECRET, code, now=120.0 - 30.0) == 4
    assert totp.match_counter(RFC_SECRET, code, now=120.0 + 120.0) is None


def test_match_rejects_malformed_codes() -> None:
    assert totp.match_counter(RFC_SECRET, "", now=60.0) is None
    assert totp.match_counter(RFC_SECRET, "12345", now=60.0) is None
    assert totp.match_counter(RFC_SECRET, "abcdef", now=60.0) is None


def test_generated_secrets_are_unique_and_usable() -> None:
    first = totp.generate_secret()
    second = totp.generate_secret()
    assert first != second
    assert "=" not in first
    code = totp.generate_code(first, now=90.0)
    assert totp.match_counter(first, code, now=90.0) == 3


def test_provisioning_uri_carries_the_secret_and_account() -> None:
    uri = totp.provisioning_uri(RFC_SECRET, account_name="user@example.com")
    assert uri.startswith("otpauth://totp/Al-Mahmoud%3Auser%40example.com?")
    assert f"secret={RFC_SECRET}" in uri
    assert "issuer=Al-Mahmoud" in uri
    assert "digits=6" in uri
