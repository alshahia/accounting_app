# Changelog

## v0.2.0-rc1 — 2026-10-27

First public source snapshot of Al-Mahmoud ERP, a release candidate for a controlled
pilot. It is the application source, database migrations, browser tests,
deployment assets and operator runbooks; the product specification and research
notes are not part of the public repository.

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
