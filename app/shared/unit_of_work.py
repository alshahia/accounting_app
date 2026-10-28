"""Unit of work with retry on serialization failure (phase 0, day 6).

One public service call owns exactly one database transaction. The unit of work
opens a fresh session, binds the tenant for row-level security, commits on
success and rolls back on any exception. A transaction that PostgreSQL aborts
with a serialization failure or deadlock (SQLSTATE 40001 / 40P01) is retried
from the start with bounded exponential back-off; every other error is raised to
the caller unchanged.

The caller passes an operation that takes the session and returns a value. It
must be safe to re-run: it may be executed more than once when a retry happens.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.shared.db import set_database_role, set_tenant

T = TypeVar("T")

# SQLSTATE classes PostgreSQL uses for "the transaction may be retried".
RETRYABLE_SQLSTATES: frozenset[str] = frozenset({"40001", "40P01"})


@dataclass(frozen=True)
class RetryPolicy:
    """How often and how long to retry a serialization failure."""

    attempts: int = 3
    base_delay_seconds: float = 0.02
    max_delay_seconds: float = 0.25


def is_retryable_db_error(error: DBAPIError) -> bool:
    """Return True when the driver error is a serialization failure or deadlock."""
    original = error.orig
    sqlstate = getattr(original, "sqlstate", None)
    if sqlstate is None:
        sqlstate = getattr(getattr(original, "__cause__", None), "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate in RETRYABLE_SQLSTATES


class UnitOfWork:
    """Owns one transaction at a time over a session factory."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        retry: RetryPolicy | None = None,
        set_role: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._retry = retry or RetryPolicy()
        self._set_role = set_role

    @asynccontextmanager
    async def transaction(self, tenant_id: UUID | None = None) -> AsyncGenerator[AsyncSession]:
        """Open a session, bind the tenant, commit on success, roll back on error."""
        session = self._session_factory()
        try:
            if tenant_id is not None:
                await set_tenant(session, tenant_id)
                if self._set_role is not None:
                    await set_database_role(session, self._set_role)
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def run(
        self,
        operation: Callable[[AsyncSession], Awaitable[T]],
        *,
        tenant_id: UUID | None = None,
        policy: RetryPolicy | None = None,
    ) -> T:
        """Run an operation in its own transaction, retrying serialization failures."""
        retry = policy or self._retry
        last_error: DBAPIError | None = None
        for attempt in range(1, retry.attempts + 1):
            try:
                async with self.transaction(tenant_id) as session:
                    return await operation(session)
            except DBAPIError as error:
                if not is_retryable_db_error(error) or attempt == retry.attempts:
                    raise
                last_error = error
                delay = min(
                    retry.max_delay_seconds,
                    retry.base_delay_seconds * (2 ** (attempt - 1)),
                )
                await asyncio.sleep(delay * (0.5 + secrets.randbelow(1000) / 1000))
        raise AssertionError("retry loop exited without a result") from last_error
