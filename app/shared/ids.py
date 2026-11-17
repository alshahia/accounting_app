"""Time-ordered identifiers (UUIDv7)."""

from __future__ import annotations

from uuid import UUID, uuid7


def new_id() -> UUID:
    """Return a new UUIDv7; the only id generator application code uses."""
    return uuid7()
