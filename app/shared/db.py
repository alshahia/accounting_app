"""Async SQLAlchemy engine and session ownership."""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class Database:
    """Owns the engine for one application instance."""

    def __init__(self, url: str, *, set_role: str | None = None) -> None:
        if set_role is not None and not ROLE_PATTERN.fullmatch(set_role):
            raise ValueError(f"database_set_role is not a valid role name: {set_role!r}")
        self._set_role = set_role
        self._engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        """Return the underlying engine."""
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the session factory; the UnitOfWork owns commits, not callers."""
        return self._session_factory

    @property
    def set_role(self) -> str | None:
        """Return the restricted role every tenant transaction runs as, if any."""
        return self._set_role

    async def ping(self) -> None:
        """Raise if the database cannot answer a trivial query."""
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        """Close pooled connections on shutdown."""
        await self._engine.dispose()


async def set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    """Bind the current transaction to one tenant for row-level security.

    Uses the transaction-local form of `set_config` (the SQL equivalent of
    `SET LOCAL`) so a pooled connection cannot carry the tenant into another
    request. Call it inside an open transaction, after authentication and before
    the first tenant-scoped query (report 21 §4.5).
    """
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


async def set_database_role(session: AsyncSession, role: str) -> None:
    """Drop the current transaction to a non-owner role (validated identifier).

    Row-level security is forced on every business table, but the application
    user owns them. Running each tenant transaction as a role that owns nothing
    makes the policies apply to the request path instead of only to raw SQL.
    """
    if not ROLE_PATTERN.fullmatch(role):
        raise ValueError(f"not a valid role name: {role!r}")
    await session.execute(text(f"SET LOCAL ROLE {role}"))
