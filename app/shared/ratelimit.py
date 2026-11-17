"""A small in-process fixed-window limiter for the unauthenticated auth surface.

The API is deployed as one process per host today, so an in-process window is
enough to blunt online password guessing; it is deliberately not a distributed
limiter. It counts only failed sign-in steps, so a busy desk never throttles
itself, while 20 failures for one address and account within a minute answer
429 before Argon2 hashing becomes a lever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock

MAX_TRACKED_KEYS = 8192


@dataclass
class _Window:
    started: float
    count: int


class FixedWindowLimiter:
    """Count events per key in fixed windows."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._windows: dict[str, _Window] = {}
        self._lock = Lock()

    def _current(self, key: str, now: float) -> _Window | None:
        window = self._windows.get(key)
        if window is None or now - window.started >= self._window_seconds:
            return None
        return window

    def blocked(self, key: str) -> bool:
        """Return True when the key has exhausted its budget for this window."""
        with self._lock:
            window = self._current(key, time.monotonic())
            return window is not None and window.count >= self._limit

    def hit(self, key: str) -> bool:
        """Record one event; return False when the key is now over budget."""
        now = time.monotonic()
        with self._lock:
            if len(self._windows) > MAX_TRACKED_KEYS:
                self._prune(now)
            window = self._current(key, now)
            if window is None:
                self._windows[key] = _Window(started=now, count=1)
                return True
            window.count += 1
            return window.count <= self._limit

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, window in self._windows.items()
            if now - window.started >= self._window_seconds
        ]
        for key in expired:
            del self._windows[key]
