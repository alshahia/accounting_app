# Al-Mahmoud ERP — backend (phase-0 build complete)

FastAPI service for the clean-room ERP. The plan, ledger rules and guardrails
are in [analysis/22-phase-0-plan.md](../analysis/22-phase-0-plan.md),
[analysis/21-technical-spec.md](../analysis/21-technical-spec.md) and
[analysis/24-engineering-guardrails.md](../analysis/24-engineering-guardrails.md).

## Phase 0 status

The phase-0 build is complete: identity/tenancy with forced row-level security, JWT auth
with refresh rotation and CSRF, the ledger with database-enforced invariants,
invoice and bill posting with weighted-average inventory, trial balance as JSON,
Arabic PDF and right-to-left Excel, and a React/Vite web shell (see
[`web/`](../web/)). Day 10 added the licence/SBOM gate, CI workflow,
backup/restore runbook, resource budget and an acceptance runner:

```powershell
uv run python scripts/seed_demo.py        # idempotent demo tenant for the web shell
uv run python scripts/acceptance.py       # A1-A13, A15, A16 in one run (about 25 s)
uv run python scripts/measure_resources.py
uv run python scripts/backup_restore_drill.py
uv run python scripts/check_licences.py
uv run python scripts/generate_sbom.py    # build/sbom.json + THIRD_PARTY_NOTICES.md
```

The browser suite lives in `web/`: `pnpm typecheck`, `pnpm build`, `pnpm test:e2e`.
A14 (offline Windows virtual machine) and the Docker Compose path remain
unverified; the runbook is [docs/10-runbooks/backup-restore.md](../docs/10-runbooks/backup-restore.md).

## Local setup

Requirements: Python 3.14, [uv](https://docs.astral.sh/uv/), PostgreSQL 18.

```powershell
# 1. PostgreSQL 18 dev cluster on 127.0.0.1:55432 (isolated from any installed service)
pwsh -File deploy/dev-postgres.ps1 start

# 2. Dependencies
uv sync

# 3. Configuration
Copy-Item .env.example .env

# 4. Schema
uv run alembic upgrade head

# 5. Run
uv run uvicorn app.main:app --reload
```

Health: <http://127.0.0.1:8000/api/v1/health> (liveness) and
<http://127.0.0.1:8000/api/v1/health/ready> (checks PostgreSQL).
OpenAPI: <http://127.0.0.1:8000/docs> (disabled in production).

## Tests and gates

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q                  # unit tests
uv run pytest -q -m integration   # needs PostgreSQL on 127.0.0.1:55432
uv run python work/scripts/check_analysis.py --root . --evidence --docs
uv run python scripts/check_licences.py
uv run pip-audit --progress-spinner off
uv run python scripts/acceptance.py
```

## Docker

The Compose stack needs a development-only database password in the
environment; it is never stored in the repository:

```powershell
$env:POSTGRES_PASSWORD = '<local-dev-password>'
docker compose --env-file .env -f deploy/docker-compose.yml up --build
```

## Layout

| Path | Holds |
|---|---|
| `app/main.py` | application factory, middleware wiring |
| `app/config.py` | deployment configuration only (no business values) |
| `app/api/v1/` | HTTP routers |
| `app/shared/` | ids, clock, errors, logging, database session |
| `app/modules/` | business modules (identity, ledger, cashbank, tax, sales, purchasing, inventory, reporting, integration) |
| `migrations/` | Alembic migration chain |
| `tests/` | unit and integration tests |
| `deploy/` | Docker, Compose, dev PostgreSQL helper |
| `web/` | React 19 + TypeScript + Vite shell and Playwright smoke tests |
| `scripts/` | demo seed, acceptance runner, licence/SBOM gate, backup drill, resource measurement |
| `docs/10-runbooks/` | repeatable procedures (backup/restore) |

## Rules that bind this code

- Ledger invariants live in PostgreSQL; never weaken them to pass a test
  (R-18.2).
- No business value hard-coded; use reference data, the settings service or
  locale files (R-18.4, report 21 §3.1).
- Money never float: integer minor units plus currency.
- One command = one transaction; routers never commit.
- Every dependency passes the licence gate (R-18.8, report 24 §7).
