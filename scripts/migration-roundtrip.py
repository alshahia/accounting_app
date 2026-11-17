"""Migrate a scratch database up/down/up; the local mirror of the CI roundtrip.

CI runs the roundtrip against the empty PostgreSQL service container. Locally the
development database may hold business data (downgrading below 0009 deletes chart
rows that journal lines reference), so this script uses its own scratch database
and drops it afterwards. Exit code mirrors the last alembic step.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg

SCRATCH_DATABASE = "almahmoud_roundtrip"
DOWNGRADE_REVISION = "0005_posting"
DEFAULT_URL = "postgresql+asyncpg://postgres@127.0.0.1:55432/almahmoud_test"


def _without_asyncpg(url: str) -> str:
    return url.replace("+asyncpg", "")


def _with_database(url: str, name: str) -> str:
    # Keep the driver suffix: alembic's environment reads this URL asynchronously.
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


async def _drop_and_create(admin_url: str) -> None:
    connection = await asyncpg.connect(admin_url)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)')
        await connection.execute(f'CREATE DATABASE "{SCRATCH_DATABASE}"')
    finally:
        await connection.close()


async def _drop(admin_url: str) -> None:
    connection = await asyncpg.connect(admin_url)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)')
    finally:
        await connection.close()


def _alembic(scratch_url: str, *arguments: str) -> int:
    environment = os.environ.copy()
    environment["MAHMOUD_DATABASE_URL"] = scratch_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        env=environment,
        check=False,
    )
    return result.returncode


async def main() -> int:
    url = os.environ.get("MAHMOUD_DATABASE_URL", DEFAULT_URL)
    admin_url = _without_asyncpg(_with_database(url, "postgres"))
    scratch_url = _with_database(url, SCRATCH_DATABASE)
    await _drop_and_create(admin_url)
    try:
        steps = (("upgrade", "head"), ("downgrade", DOWNGRADE_REVISION), ("upgrade", "head"))
        for arguments in steps:
            label = " ".join(arguments)
            print(f"alembic {label} on {SCRATCH_DATABASE}")
            code = _alembic(scratch_url, *arguments)
            if code != 0:
                print(f"alembic {label} failed with exit code {code}")
                return code
        return 0
    finally:
        await _drop(admin_url)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
