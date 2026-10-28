"""Measure the phase-0 resource budget (day 10, A13) against a 4 GB target.

Run from the repository root against the development cluster:

    uv run python scripts/measure_resources.py

The script seeds a temporary tenant in the `almahmoud` database, posts 20 journal
entries concurrently through the real ledger service, renders one trial balance
as PDF and one as XLSX, and reports the resident memory of the Python process,
the dev PostgreSQL cluster and the running API/Vite servers. Every artifact it
creates is removed again.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.ledger.service import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.modules.reports.render import render_trial_balance_pdf, render_trial_balance_xlsx
from app.modules.reports.service import trial_balance
from app.shared.ids import new_id
from app.shared.unit_of_work import UnitOfWork
from tests.integration.conftest import delete_identity, seed_identity, seed_ledger

DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:55432/almahmoud"
BUDGET_MB = 4096
PARALLEL_POSTINGS = 20


def _rss_mb(process: psutil.Process) -> float:
    try:
        return process.memory_info().rss / (1024 * 1024)
    except psutil.Error:
        return 0.0


def _server_rss() -> dict[str, float]:
    """RSS of the API and Vite dev servers, when they are running."""
    found: dict[str, float] = {}
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(process.info["cmdline"] or [])
        except (psutil.Error, TypeError):
            continue
        if "uvicorn" in cmdline and "app.main" in cmdline:
            found["api (uvicorn)"] = _rss_mb(process)
        elif "vite" in cmdline and "node" in (process.info["name"] or "").lower():
            found["web (vite)"] = _rss_mb(process)
    return found


def _cluster_rss() -> float:
    total = 0.0
    for process in psutil.process_iter(["name"]):
        if (process.info["name"] or "").lower() == "postgres.exe":
            total += _rss_mb(process)
    return total


async def _exercise(engine: AsyncEngine) -> dict[str, float]:
    slug = f"measure-{new_id().hex[-12:]}"
    identity = await seed_identity(
        engine,
        tenant_slug=slug,
        email=f"{slug}@example.com",
        password="correct horse battery staple",
    )
    try:
        return await _run_checks(engine, identity, slug)
    finally:
        await delete_identity(engine, identity.tenant_id)


async def _run_checks(engine: AsyncEngine, identity: object, slug: str) -> dict[str, float]:
    ledger = await seed_ledger(engine, identity)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    unit_of_work = UnitOfWork(factory)
    account_ids = ledger.account_ids
    results: dict[str, float] = {}

    async def post_one(index: int) -> int:
        request = JournalEntryInput(
            company_id=identity.company_a,
            period_id=ledger.period_id,
            entry_date=date(2026, 1, 15),
            memo=f"resource probe {index}",
            post=True,
            idempotency_key=f"measure-{slug}-{index}",
            lines=(
                JournalLineInput(account_id=account_ids["1000"], debit_minor=100 + index),
                JournalLineInput(account_id=account_ids["3000"], credit_minor=100 + index),
            ),
        )
        result = await create_journal_entry(
            unit_of_work,
            tenant_id=identity.tenant_id,
            actor_user_id=identity.user_id,
            request=request,
        )
        assert result.entry.status == "posted"
        return result.entry.id

    started = time.perf_counter()
    await asyncio.gather(*(post_one(index) for index in range(PARALLEL_POSTINGS)))
    results["20_parallel_postings_seconds"] = time.perf_counter() - started

    async with factory() as session:
        balance = await trial_balance(
            session,
            tenant_id=identity.tenant_id,
            company_id=identity.company_a,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )
    assert balance.rows, "the trial balance must not be empty"
    results["trial_balance_rows"] = float(len(balance.rows))
    results["trial_balance_debit_minor"] = float(balance.total_debit_minor)
    results["trial_balance_credit_minor"] = float(balance.total_credit_minor)

    started = time.perf_counter()
    pdf = render_trial_balance_pdf(balance, company_name_ar="شركة القياس", company_name_en="Measure Co")
    results["pdf_render_seconds"] = time.perf_counter() - started
    results["pdf_bytes"] = float(len(pdf))
    assert pdf.startswith(b"%PDF")

    started = time.perf_counter()
    workbook = render_trial_balance_xlsx(balance, company_name_ar="شركة القياس", company_name_en="Measure Co")
    results["xlsx_render_seconds"] = time.perf_counter() - started
    results["xlsx_bytes"] = float(len(workbook))
    assert workbook.startswith(b"PK")

    return results


async def main() -> int:
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    process = psutil.Process(os.getpid())
    baseline = _rss_mb(process)
    try:
        results = await _exercise(engine)
    finally:
        await engine.dispose()
    peak = _rss_mb(process)
    server = _server_rss()
    cluster = _cluster_rss()
    python_total = max(baseline, peak)
    accounted = python_total + cluster + sum(server.values())
    budget_ok = accounted <= BUDGET_MB

    print()
    print(f"python process (measurement script) : {python_total:8.1f} MB")
    for name, value in sorted(server.items()):
        print(f"{name:35}: {value:8.1f} MB")
    print(f"PostgreSQL cluster (all databases)  : {cluster:8.1f} MB")
    print(f"accounted resident memory           : {accounted:8.1f} MB (budget {BUDGET_MB} MB)")
    print()
    print(f"20 parallel posted journal entries  : {results['20_parallel_postings_seconds']:.2f} s")
    print(f"trial balance rows / debit / credit : {int(results['trial_balance_rows'])} / "
          f"{int(results['trial_balance_debit_minor'])} / {int(results['trial_balance_credit_minor'])}")
    print(f"PDF render                          : {results['pdf_render_seconds']:.2f} s, "
          f"{int(results['pdf_bytes'])} bytes")
    print(f"XLSX render                         : {results['xlsx_render_seconds']:.2f} s, "
          f"{int(results['xlsx_bytes'])} bytes")
    print()
    print(f"memory-budget: {'ok' if budget_ok else 'OVER BUDGET'}")
    return 0 if budget_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
