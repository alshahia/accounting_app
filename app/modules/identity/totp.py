"""RFC 6238 time-based one-time passwords for the second factor.

The implementation is deliberately dependency-free: HMAC-SHA1 over a 30-second
counter, base32 secrets and a six-digit code are the whole protocol every
authenticator app implements. A code is accepted from the current step or one
step either side, and the accepted counter is returned so the caller can refuse
to reuse a code inside the same window.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD_SECONDS = 30
SECRET_BYTES = 20
_WINDOW = 1


def generate_secret() -> str:
    """Return a fresh base32 secret without padding, as authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    normalized = secret.strip().upper()
    padding = "=" * (-len(normalized) % 8)
    return base64.b32decode(normalized + padding)


def counter_at(now: float | None = None) -> int:
    """Return the RFC 6238 counter for the current (or given) unix time."""
    moment = time.time() if now is None else now
    return int(moment) // PERIOD_SECONDS


def generate_code(secret: str, *, now: float | None = None) -> str:
    """Return the six-digit code for one time step."""
    counter = counter_at(now)
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    (binary,) = struct.unpack(">I", digest[offset : offset + 4])
    return f"{(binary & 0x7FFFFFFF) % (10**DIGITS):0{DIGITS}d}"


def match_counter(secret: str, code: str, *, now: float | None = None) -> int | None:
    """Return the matched counter, or None when the code is not valid nearby."""
    candidate = code.strip().replace(" ", "")
    if len(candidate) != DIGITS or not candidate.isdigit():
        return None
    current = counter_at(now)
    for offset in range(-_WINDOW, _WINDOW + 1):
        step = current + offset
        candidate_time = float(step * PERIOD_SECONDS)
        if hmac.compare_digest(generate_code(secret, now=candidate_time), candidate):
            return step
    return None


def provisioning_uri(secret: str, *, account_name: str, issuer: str = "Al-Mahmoud") -> str:
    """Build the otpauth:// URI an authenticator app reads from a QR code."""
    label = quote(f"{issuer}:{account_name}", safe="")
    query = f"secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period=30"
    return f"otpauth://totp/{label}?{query}"
