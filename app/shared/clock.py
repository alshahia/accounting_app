"""An injectable clock so domain code never calls datetime.now directly."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Reads the current instant."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC time."""
        ...


class SystemClock:
    """Production clock backed by the system clock."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC time."""
        return datetime.now(UTC)
