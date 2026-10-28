"""Per-request context values, propagated through context variables."""

from __future__ import annotations

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str:
    """Return the current request id, or '-' outside a request."""
    return request_id_var.get() or "-"
