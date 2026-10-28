"""Fixed-window limiter used by the login endpoint."""

from __future__ import annotations

import pytest

from app.shared import ratelimit


class _Clock:
    now = 1000.0


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    frozen = _Clock()
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: frozen.now)
    return frozen


def test_a_key_is_allowed_up_to_the_limit(clock: _Clock) -> None:
    limiter = ratelimit.FixedWindowLimiter(limit=3, window_seconds=60)

    # Three events fit the window; the fourth is refused and shows as blocked.
    assert [limiter.hit("client") for _ in range(3)] == [True, True, True]
    assert limiter.blocked("client") is True
    assert limiter.hit("client") is False


def test_other_keys_have_their_own_budget(clock: _Clock) -> None:
    limiter = ratelimit.FixedWindowLimiter(limit=1, window_seconds=60)

    assert limiter.blocked("first") is False
    assert limiter.hit("first") is True
    assert limiter.blocked("first") is True
    assert limiter.blocked("second") is False
    assert limiter.hit("second") is True


def test_the_window_resets(clock: _Clock) -> None:
    limiter = ratelimit.FixedWindowLimiter(limit=1, window_seconds=60)

    assert limiter.hit("client") is True
    assert limiter.blocked("client") is True
    clock.now += 61
    assert limiter.blocked("client") is False
    assert limiter.hit("client") is True
