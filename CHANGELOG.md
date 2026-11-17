# Changelog

## v0.2.0-rc1 — 2026-11-17

Second public source snapshot of Al-Mahmoud ERP, a release candidate for a
controlled pilot. It is the application source, database migrations, browser
tests, deployment assets and operator runbooks; the product specification and
research notes are not part of the public repository. The first snapshot was
published 2026-10-27.

### Updated since the first snapshot

- The production edge now trusts a forwarded client address only from the TLS
  gateway; the login limiter and the audit trail key on the resolved edge
  address, and every healthcheck probes readiness instead of liveness.
- The pilot host was renamed to Al-Mahmoud with the previous installation kept
  as a rollback; backups are retention-managed (30 encrypted dumps) and every
  dump is verified by the nightly restore drill.
- A signed fiscal year can no longer have one of its periods reopened, and the
  year-end closing entry is excluded from the profit-and-loss statement; the
  guard ships as the reversible migration `0024_closed_year_reopen_guard`.
- The next calendar year can be created through the audited API and the close
  tab of the Tools screen, so the 2027 books can be opened before 2027-01-01.
- Dependency security: the FastAPI dependency `opentelemetry-api` was bumped
  for CVE-2026-126360; the licence allow-list and `pip-audit` stay in the gate
  set.

### Counts at this snapshot

- `51` unit tests and `184` PostgreSQL integration tests pass together with the
  migration up/down/up round trip, the Playwright browser suite, Ruff, Pyright
  strict, the licence allow-list gate and `pip-audit`. These are snapshot
  numbers, not a promise: run the commands above.

### Included

- Arabic-first interface with a true RTL layout and English as a second language,
  including the sign-in, sales, purchases, cash and banking, inventory, journal,
  reports and administration screens.
- FastAPI 0.116 backend on Python 3.14: JWT authentication with rotating refresh
  cookies and CSRF protection, Argon2id passwords, TOTP second factor with
  recovery codes, permissions, invitations, and session management.
- PostgreSQL 18 schema with forced row-level security per tenant, deferred
  debit-equals-credit constraints, immutable posted entries, closed-period and
  account rules, append-only audit trail, and reversible Alembic migrations.
- Sales invoices and credit notes, purchase bills and debit notes, receipts and
  payments with allocation, cheques, transfers, bank reconciliation, opening
  balances, inventory with weighted-average costing, stock counts and transfers.
- Reports: trial balance, profit and loss, balance sheet, AR/AP aging, general
  ledger, tax return, inventory valuation, stock movement, document register and
  audit trail, with Arabic PDF and right-to-left Excel exports.
- Docker Compose single-node production stack (TLS gateway, no database port),
  and a Windows offline installer with an offline bundle build script.
- CI gates: Ruff, Pyright strict, unit and PostgreSQL integration tests, a
  migration up/down/up round trip, Playwright browser tests, a licence
  allow-list gate, pip-audit, CycloneDX SBOM and third-party notices.

### Known limits

- The pilot accountant sign-off and the clean offline-install evidence are still
  owner steps; this is not general availability. See the status note in
  [README.md](README.md).
- Interfaces may change before v1.
