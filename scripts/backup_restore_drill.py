"""Logical backup/restore drill for the phase-0 database (day 10, A15 partial).

Run from the repository root against the development cluster:

    uv run python scripts/backup_restore_drill.py

It takes a custom-format `pg_dump` of the source database, restores it into a
fresh drill database on the same cluster, and compares row counts and ledger
totals. The physical backup/WAL half of A15 (pg_basebackup + archive recovery,
or pgBackRest on Linux) is an operator runbook step; this script proves the
logical restore path end to end.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PG_BIN = Path(r"C:\Program Files\PostgreSQL\18\bin")
HOST = "127.0.0.1"
PORT = "55432"
USER = "postgres"


def _run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(command)}\n{result.stderr}")
    return result.stdout


def _psql(pg_bin: Path, database: str, sql: str) -> str:
    return _run(
        [
            str(pg_bin / "psql.exe"),
            "-h",
            HOST,
            "-p",
            PORT,
            "-U",
            USER,
            "-d",
            database,
            "-tAc",
            sql,
        ]
    ).strip()


CHECKS = {
    "tables": "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'",
    "journal_entries": "SELECT count(*) FROM journal_entry",
    "debit_total": "SELECT COALESCE(sum(debit_minor), 0) FROM journal_line",
    "credit_total": "SELECT COALESCE(sum(credit_minor), 0) FROM journal_line",
    "tenant_total": "SELECT COALESCE(sum(base_total_minor), 0) FROM sales_invoice",
    "stock_value": "SELECT COALESCE(sum(stock_value_minor), 0) FROM item",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="pg_dump/pg_restore drill with ledger checks")
    parser.add_argument("--pg-bin", type=Path, default=DEFAULT_PG_BIN)
    parser.add_argument("--source", default="almahmoud")
    parser.add_argument("--restore", default="almahmoud_restore_drill")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "build" / "backup")
    args = parser.parse_args()

    pg_bin: Path = args.pg_bin
    if not (pg_bin / "pg_dump.exe").exists():
        raise SystemExit(f"pg_dump not found under {pg_bin}")
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dump_path = args.backup_dir / f"{args.source}-{stamp}.dump"

    print(f"1. pg_dump {args.source} -> {dump_path}")
    _run(
        [
            str(pg_bin / "pg_dump.exe"),
            "-h",
            HOST,
            "-p",
            PORT,
            "-U",
            USER,
            "-d",
            args.source,
            "-Fc",
            "-f",
            str(dump_path),
        ]
    )
    size = dump_path.stat().st_size
    print(f"   dump written ({size} bytes)")

    print(f"2. recreate drill database {args.restore}")
    _run(
        [
            str(pg_bin / "dropdb.exe"),
            "-h",
            HOST,
            "-p",
            PORT,
            "-U",
            USER,
            "--if-exists",
            args.restore,
        ]
    )
    _run(
        [
            str(pg_bin / "createdb.exe"),
            "-h",
            HOST,
            "-p",
            PORT,
            "-U",
            USER,
            args.restore,
        ]
    )

    print("3. pg_restore")
    _run(
        [
            str(pg_bin / "pg_restore.exe"),
            "-h",
            HOST,
            "-p",
            PORT,
            "-U",
            USER,
            "-d",
            args.restore,
            "--no-owner",
            "--exit-on-error",
            str(dump_path),
        ]
    )

    print("4. compare source and restored database")
    failures: list[str] = []
    for name, sql in CHECKS.items():
        source_value = _psql(pg_bin, args.source, sql)
        restored_value = _psql(pg_bin, args.restore, sql)
        status = "ok" if source_value == restored_value else "MISMATCH"
        if source_value != restored_value:
            failures.append(f"{name}: source={source_value} restored={restored_value}")
        print(f"   {name:16} source={source_value:>12} restored={restored_value:>12} {status}")

    debit = int(_psql(pg_bin, args.restore, CHECKS["debit_total"]))
    credit = int(_psql(pg_bin, args.restore, CHECKS["credit_total"]))
    if debit != credit:
        failures.append(f"restored ledger is unbalanced: debit={debit} credit={credit}")
    print(f"   restored ledger balanced: {debit} == {credit}")

    if failures:
        print("RESTORE DRILL FAILED", file=sys.stderr)
        for failure in failures:
            print(f"   - {failure}", file=sys.stderr)
        return 1

    print(f"restore-ok: {args.restore} matches {args.source}; dump kept at {dump_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
