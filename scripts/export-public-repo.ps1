<#
.SYNOPSIS
  Build a sanitised, publishable snapshot of the application source.
.DESCRIPTION
  The working tree also holds the product specification, the read-only study of
  the vendor suite, review-room transcripts and agent rules. None of that is
  part of a public repository. This script copies only the application source,
  tests, deployment assets and operator runbooks into a fresh git repository,
  writes a public README in English and Arabic, a machine-readable llms.txt and
  SECURITY.md, runs the secret and internal-mention gates, and makes a single
  commit authored as a neutral build identity.

  The result is printed with the exact commands to create the GitHub repository.
  Nothing is pushed automatically and no GitHub account is touched.

.EXAMPLE
  pwsh -File scripts/export-public-repo.ps1
  pwsh -File scripts/export-public-repo.ps1 -OutputDirectory build/public-export -Force
#>
[CmdletBinding()]
param(
    [string] $OutputDirectory = 'build/public-export',
    [string] $RepositoryName = 'almahmoud-erp',
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$output = Join-Path $root $OutputDirectory

$includePrefixes = @(
    'app/',
    'migrations/',
    'tests/',
    'web/',
    'deploy/',
    'scripts/',
    '.github/workflows/',
    'docs/assets/'
)
$includeRootFiles = @(
    'LICENSE',
    'pyproject.toml',
    'uv.lock',
    'alembic.ini',
    '.gitignore',
    '.dockerignore',
    '.env.example',
    '.python-version',
    '.gitattributes',
    'VERSION',
    'THIRD_PARTY_NOTICES.md'
)
$includeRunbooks = @(
    'docs/10-runbooks/release.md',
    'docs/10-runbooks/backup-restore.md',
    'docs/10-runbooks/offline-windows-install.md',
    'docs/10-runbooks/tenant-provisioning.md',
    'docs/10-runbooks/incident.md',
    'docs/10-runbooks/accountant-signoff.md',
    'docs/09-decisions/0002-phase-0-licence-exceptions.md'
)
$excludePatterns = @(
    '^web/dist/',
    '^web/node_modules/',
    '^deploy/certs/',
    '^deploy/backup/dumps/',
    '^deploy/backup/keys/',
    '^deploy/backup/reports/',
    '^\.github/workflows/context\.yml$',
    '^web/tsconfig.tsbuildinfo$',
    '^\.env$'
)

if (Test-Path -LiteralPath $output) {
    if (-not $Force) { throw "$output already exists; pass -Force to replace it" }
    Remove-Item -LiteralPath $output -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $output | Out-Null

Push-Location $root
try {
    $tracked = @(git ls-files)
}
finally {
    Pop-Location
}
if ($tracked.Count -eq 0) { throw 'the working tree has no tracked files' }

$copied = 0
foreach ($file in $tracked) {
    $relative = $file.Replace('\', '/')
    $keep = $false
    foreach ($prefix in $includePrefixes) {
        if ($relative.StartsWith($prefix)) { $keep = $true; break }
    }
    if (-not $keep -and ($includeRootFiles -contains $relative)) { $keep = $true }
    if (-not $keep -and ($includeRunbooks -contains $relative)) { $keep = $true }
    if (-not $keep) { continue }
    $skip = $false
    foreach ($pattern in $excludePatterns) {
        if ($relative -match $pattern) { $skip = $true; break }
    }
    if ($skip) { continue }
    $source = Join-Path $root $file
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
    $target = Join-Path $output $relative
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Copy-Item -LiteralPath $source -Destination $target
    $copied++
}

$publicReadme = @'
<div align="center">

<img src="docs/assets/banner.svg" alt="Al-Mahmoud ERP banner: open-source Arabic and English accounting and ERP software for Arabic-speaking businesses" width="100%">

# Al-Mahmoud ERP

**Open-source accounting and ERP software for Arabic-speaking businesses.**
Bilingual Arabic/English and RTL-first, self-hosted on a single node, backed by
PostgreSQL where the double-entry ledger enforces itself, and MIT licensed.

[![CI](https://github.com/alshahia/accounting_app/actions/workflows/app-ci.yml/badge.svg?branch=main)](https://github.com/alshahia/accounting_app/actions/workflows/app-ci.yml)
![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.116-009688?logo=fastapi&logoColor=white)
![React 19](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)
![PostgreSQL 18](https://img.shields.io/badge/PostgreSQL-18-4169E1?logo=postgresql&logoColor=white)
![Licence MIT](https://img.shields.io/badge/licence-MIT-c9a227)

[English](README.md) · [العربية](README.ar.md) · [llms.txt for AI agents](llms.txt)

</div>

> **Status: `v0.2.0-rc1`, a release candidate for a controlled pilot — not
> general availability.** The pilot accountant sign-off and the clean
> offline-install evidence are still owner steps, and interfaces may change
> before v1. The repository contains no customer data and no production secrets.

## What Al-Mahmoud ERP is

**Al-Mahmoud ERP** (Arabic: **المحمود ERP**) is an open-source accounting and
enterprise resource planning (ERP) system for Arabic-speaking businesses. It puts
a PostgreSQL-backed double-entry general ledger — sales invoicing, purchasing,
cash and bank management, inventory, and financial reporting — behind a bilingual
Arabic/English interface whose default layout is genuinely right-to-left. You run
it yourself: one node with Docker Compose, or an offline Windows install, with no
external services and no per-seat licence. The source is MIT-licensed at
<https://github.com/alshahia/accounting_app>.

In one line: Arabic accounting software and a small, self-hosted ERP in a single
auditable codebase.

## Who it is for

Trading companies, distributors, retailers, workshops, clinics and contractors in
Arabic-speaking markets — and the accountants who keep their books — where an
English-first, cloud-only ERP is a poor fit.

| If you are… | What this gives you |
|---|---|
| an SMB in an Arabic-speaking market | an Arabic interface and Arabic documents your staff already read, with no translation layer to maintain |
| a branch, warehouse or factory with slow or no internet | a single-node stack that needs no outbound connection, plus an offline Windows bundle and installer |
| an accountant or auditor | double-entry books whose balance the database enforces at commit, with posted entries that cannot be edited |
| a multi-company group | several companies per tenant, isolated by forced row-level security |
| a self-hosting team | Docker Compose, TLS on one port, and written backup, restore and incident runbooks |
| a researcher or an AI agent | a small, complete, MIT-licensed codebase whose tests and CI gates are the published evidence |

## Why Al-Mahmoud ERP

| Differentiator | What it means in practice |
|---|---|
| **Arabic-first, RTL by design** | Arabic is the default language and the layout is built right-to-left rather than mirrored afterwards; English is a complete second language, not an overlay. |
| **On-premise and offline** | One node, no external service, one TLS entry point, and a Windows installer for sites without dependable internet. The books stay on your hardware. |
| **The database enforces the ledger** | Deferred debit-equals-credit checks at commit, immutable posted entries, closed-period rules and per-tenant row-level security are PostgreSQL constraints and triggers, not application etiquette. |
| **Arabic documents out of the box** | Invoice, receipt, payment, note and report PDFs render Arabic with embedded fonts, and Excel exports run right-to-left. |
| **Bilingual parity** | Every screen exists in Arabic and English; interface language and text direction change together. |
| **MIT, no lock-in** | Read, audit, fork and run it commercially. Third-party components and their licences are listed in THIRD_PARTY_NOTICES.md. |

## What it does

The feature set below is what `v0.2.0-rc1` ships; it is not a roadmap.

| Area | Included in this release candidate |
|---|---|
| Sales | invoices, credit notes (returns), customer statements, receipts with invoice allocation |
| Purchases | bills, debit notes, supplier statements, payments with bill allocation |
| Cash and banks | cash and bank accounts, receipts, payments, cheques, transfers, bank reconciliation, opening balances |
| Ledger | manual journal entries with a draft → post → immutable lifecycle, reversal, year-end close |
| Reports | trial balance, profit and loss, balance sheet, AR/AP aging, general ledger, tax return, inventory valuation, stock movement, document register, audit trail |
| Inventory | warehouses, weighted-average costing, stock counts and adjustments, transfers |
| Multi-company | several companies per tenant, company switching, forced row-level security |
| People and access | users, roles and permissions, invitations, MFA (TOTP plus recovery codes), session management |
| Documents | Arabic PDFs for invoices, bills, receipts, payments, notes and reports; right-to-left Excel exports |
| Bilingual UI | complete Arabic and English interface, RTL or LTR per language |

## Screenshots

| | |
|---|---|
| ![Arabic sign-in screen with a right-to-left layout](docs/assets/screenshots/login-ar.png) | ![Sales invoice list showing posting state and the PDF action](docs/assets/screenshots/invoices-ar.png) |
| Arabic-first sign-in, right-to-left by default | Sales invoices with posting state and an Arabic PDF per invoice |
| ![Trial balance report in Arabic with balanced debit and credit totals](docs/assets/screenshots/trial-balance-ar.png) | ![The same trial balance report in English, rendered left to right](docs/assets/screenshots/trial-balance-en.png) |
| Trial balance: balanced totals, drill-down, PDF and Excel export | The same report in English, rendered left-to-right |

<img src="docs/assets/screenshots/login-mobile.png" alt="Sign-in page on a phone, Arabic interface, right-to-left" width="280">

## Architecture

<img src="docs/assets/architecture.svg" alt="Architecture: browser single-page app to an Nginx TLS gateway to the FastAPI API to PostgreSQL with forced row-level security" width="100%">

A single node, one published port, and every accounting guarantee pushed as far
into PostgreSQL as it will go.

- **Single node.** The gateway publishes one port; the database publishes none.
  Migrations run as a one-shot container before the API starts.
- **Tenant isolation in the database.** Every request transaction drops to a
  non-owner role with the tenant bound, so row-level security applies to the
  request path, not only to background jobs.
- **Ledger invariants in the database.** A deferred constraint checks debit
  equals credit at commit; posted entries and their lines are immutable; account,
  period and source rules are checked by triggers.
- **Async API.** FastAPI 0.116 on Python 3.14, SQLAlchemy 2 async, Alembic
  migrations with a tested up/down/up round trip, and RFC 9457 problem responses
  throughout.
- **Arabic documents.** ReportLab-based PDFs with embedded DejaVu/Noto fonts, so
  Arabic renders identically in a container and on Windows.

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Python 3.14, FastAPI 0.116, SQLAlchemy 2 (async), Alembic, Pydantic v2 |
| Database | PostgreSQL 18, forced row-level security, deferred constraint triggers |
| Frontend | React 19, TypeScript, Vite 8, custom RTL/LTR i18n layer |
| Security | Argon2id, TOTP second factor, JWT access tokens, rotating refresh cookies with CSRF protection |
| Documents | ReportLab (PDF), openpyxl (RTL Excel), CycloneDX SBOM |
| Delivery | Docker Compose single-node stack, Windows offline installer and bundle |
| Quality | Ruff, Pyright strict, pytest, Playwright, licence allow-list gate, pip-audit |
| CI | GitHub Actions: backend, web and licences-sbom jobs on Ubuntu |

## Quick start (local development)

Requirements: Python 3.14 and [uv](https://docs.astral.sh/uv/), Node 24 and pnpm,
and PostgreSQL 18 (a local service or Docker).

~~~powershell
# 1. development PostgreSQL on 127.0.0.1:55432
pwsh -File deploy/dev-postgres.ps1 start

# 2. backend dependencies, schema and demo tenant
uv sync --locked
uv run alembic upgrade head
uv run python scripts/seed_demo.py

# 3. API on http://127.0.0.1:8000 (health: /api/v1/health)
uv run uvicorn app.main:app --reload

# 4. web app on http://127.0.0.1:5173, proxying /api to the API
cd web
pnpm install --frozen-lockfile
pnpm dev
~~~

Open <http://127.0.0.1:5173> and sign in with the demo tenant created in step 2
(the fixtures are in [web/e2e/auth.ts](web/e2e/auth.ts); the login form itself
ships no credentials). OpenAPI is at <http://127.0.0.1:8000/docs> in development
only.

## Self-hosted production deployment

~~~powershell
pwsh -File deploy/generate-certs.ps1 -DnsName erp.example.local
$env:POSTGRES_PASSWORD     = '<strong password>'
$env:MAHMOUD_JWT_SECRET      = '<at least 32 random bytes>'
$env:MAHMOUD_BOOTSTRAP_TOKEN = '<at least 32 random bytes>'
docker compose -f deploy/docker-compose.prod.yml up -d --build
~~~

Then open https://erp.example.local:8443/setup with the bootstrap token to create
the first tenant and administrator. The database is reachable only from the
Compose network, and migrations run before the API accepts traffic.

## Offline installation (no internet)

For a Windows server without internet, build the offline bundle on a connected
machine and install with `deploy/windows/install.bat`; the procedure, evidence
requirements and rollback are in
[docs/10-runbooks/offline-windows-install.md](docs/10-runbooks/offline-windows-install.md).

A clean install on a target-class 2 vCPU / 4 GB machine is one of the pilot gates
listed under [Status and roadmap](#status-and-roadmap), so this path is documented
and scripted, not yet evidenced on your hardware.

## Quality gates

The same gates run locally and in CI:

~~~powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q                  # unit tests
uv run pytest -q -m integration   # PostgreSQL integration tests
uv run python scripts/check_licences.py
uv run pip-audit --progress-spinner off
cd web; pnpm typecheck; pnpm build; pnpm test:e2e
~~~

The GitHub Actions workflow has three jobs: **backend** (lint, types, unit,
PostgreSQL integration, migration round trip), **web** (typecheck, build,
Playwright browser suite against the live API) and **licences-sbom** (allow-list
gate, dependency audit, SBOM and third-party notices). A full local mirror is
`pwsh -File scripts/ci-local.ps1`.

## Security

- Tenant isolation is enforced by PostgreSQL row-level security on every request
  path; the application role cannot read another tenant's rows.
- Passwords use Argon2id; sign-in supports TOTP with single-use recovery codes;
  access tokens are short-lived and refresh cookies rotate with CSRF checks.
- Login attempts are rate-limited and accounts lock after repeated failures.
- Posted accounting entries cannot be edited or deleted; corrections are
  reversals, and the audit trail is append-only.
- The repository ships no credentials. Report a vulnerability privately as
  described in [SECURITY.md](SECURITY.md) rather than in a public issue.

## FAQ

### What is Al-Mahmoud ERP?

A self-hosted, open-source accounting and ERP system for Arabic-speaking
businesses: a double-entry ledger plus sales, purchasing, cash and bank,
inventory and financial reporting, on PostgreSQL 18, with a bilingual
Arabic/English interface that lays out right-to-left in Arabic. See
[What Al-Mahmoud ERP is](#what-al-mahmoud-erp-is).

### Is Al-Mahmoud ERP free and open source?

Yes. The licence is MIT — read, modify, redistribute and run it commercially.
There is no closed edition in this repository, and no licence key is needed to
build or operate it. Third-party components and their licences are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

### Does it work offline?

Yes by design and in operation: the running stack calls no external service, and
the repository ships an offline Windows bundle and installer for sites without
dependable internet. Two pilot gates are still open — a clean install on an
offline 2 vCPU / 4 GB Windows machine and peak-memory evidence on that hardware —
so verify the install on your own machine against the runbook.

### Which languages does it support?

Arabic and English, both complete. Arabic is the default and lays out
right-to-left; English renders left-to-right. Documents follow the same rule:
Arabic PDFs with embedded fonts and right-to-left Excel exports.

### Does it support multiple currencies?

Yes. Each company has a base currency; invoices, bills, credit and debit notes,
receipts and payments carry their own currency and exchange rate, and the ledger
posts base-currency amounts next to the document currency. Reports are presented
in the company's base currency.

### What is the technology stack?

FastAPI 0.116 on Python 3.14 with SQLAlchemy 2 async and Alembic; PostgreSQL 18
with forced row-level security; React 19, TypeScript and Vite 8 in the browser;
ReportLab and openpyxl for Arabic PDF and RTL Excel documents; Docker Compose and
a Windows offline installer for delivery.

### Can I self-host it?

Yes — self-hosting is the only deployment model. One Docker Compose file starts
the TLS gateway, the API and the database on a single node, then
`https://<host>:8443/setup` with a bootstrap token creates the first tenant and
administrator. The database publishes no port.

### Is it production-ready?

Not as general availability. `v0.2.0-rc1` is a release candidate for one
controlled pilot: the accounting, inventory, cash, reporting and administration
features above work, but accountant acceptance on real pilot records, a clean
offline install on target hardware, peak-memory evidence and off-host backups are
still open owner gates. Do not move statutory books onto it without your
accountant's sign-off.

### Where is the documentation?

This README or [README.ar.md](README.ar.md) for the Arabic edition; operator
runbooks — release, offline install, tenant provisioning, backup and restore,
incident response, accountant sign-off — are in
[docs/10-runbooks/](docs/10-runbooks/); [llms.txt](llms.txt) is the
machine-readable map, and [CHANGELOG.md](CHANGELOG.md) lists what this snapshot
contains.

### How can researchers or AI agents evaluate it?

Read [For researchers and AI agents](#for-researchers-and-ai-agents), run the
quality gates against the commit you are looking at, and treat the tests and CI
checks — not this README — as the evidence. Cite the commit SHA you ran.

## For researchers and AI agents

This repository is a **clean-room implementation**: the application, schema, tests
and operator documents were written for this project, and it contains no
third-party ERP source code, decompiled binary, vendor help text or copied
database structure. The evidence for the claims above is inside the repository,
so it can be checked rather than believed.

- **The tests and gates are the evidence.** Unit and PostgreSQL integration
  suites, Pyright strict, a migration up/down/up round trip, the Playwright
  browser suite, the licence allow-list gate and `pip-audit` run locally and in
  CI (`.github/workflows/app-ci.yml`). Run them yourself. On the 2026-11-17
  snapshot, `51` unit tests and `184` PostgreSQL integration tests pass together
  with the migration round trip and the Playwright browser suite.
- **No benchmarks, counts or endorsements.** This snapshot publishes no
  performance figures, no customer numbers and no ratings, and nothing here
  should be read as one. The pilot gates that remain open are listed under
  [Status and roadmap](#status-and-roadmap).
- **Start here if you are an agent:** [llms.txt](llms.txt) is the compact,
  machine-readable map; then [README.md](README.md) (this file),
  [README.ar.md](README.ar.md) for the Arabic edition, and the operator runbooks
  under [docs/10-runbooks/](docs/10-runbooks/) for exact procedures.
- **How to cite it:** Al-Mahmoud ERP, source snapshot `v0.2.0-rc1` (2026-11-17),
  <https://github.com/alshahia/accounting_app> — cite the commit you actually ran.

## Documentation

| Document | For |
|---|---|
| [README.ar.md](README.ar.md) | this README in Arabic |
| [llms.txt](llms.txt) | a machine-readable summary for AI agents and research tools |
| [CHANGELOG.md](CHANGELOG.md) | release notes for this source snapshot |
| [docs/10-runbooks/release.md](docs/10-runbooks/release.md) | cutting and verifying a release |
| [docs/10-runbooks/offline-windows-install.md](docs/10-runbooks/offline-windows-install.md) | the offline Windows installation |
| [docs/10-runbooks/tenant-provisioning.md](docs/10-runbooks/tenant-provisioning.md) | creating a tenant and its first administrator |
| [docs/10-runbooks/backup-restore.md](docs/10-runbooks/backup-restore.md) | backup, restore and drills |
| [docs/10-runbooks/incident.md](docs/10-runbooks/incident.md) | incident response |
| [docs/10-runbooks/accountant-signoff.md](docs/10-runbooks/accountant-signoff.md) | the accountant acceptance walkthrough |
| [SECURITY.md](SECURITY.md) | private vulnerability reporting |
| [docs/09-decisions/0002-phase-0-licence-exceptions.md](docs/09-decisions/0002-phase-0-licence-exceptions.md) | dependency licence decisions |

## Project layout

~~~
app/                 FastAPI application (modules, api, core, db)
migrations/          Alembic migration chain
tests/               unit and PostgreSQL integration tests
web/                 React + TypeScript + Vite app and Playwright suite
deploy/              Dockerfile, Compose stacks, nginx, Windows installer
scripts/             seed, acceptance, licence/SBOM and backup tooling
docs/                operator runbooks and licence decision
.github/workflows/   CI workflow
~~~

## Status and roadmap

- **Done in `v0.2.0-rc1`:** the accounting, purchasing, cash, inventory and
  reporting slices above, bilingual RTL UI, MFA, multi-company isolation,
  backup/restore drills, Docker and offline Windows packaging, and the CI gate set.
- **Pilot gates still open:** accountant acceptance on real pilot records, clean
  offline install on a 2 vCPU / 4 GB machine, peak-memory evidence on that class
  of machine, and off-host backup copies. Until those are signed off this stays a
  release candidate.
- **After the pilot:** multi-branch consolidation, additional jurisdictions and
  report formats, and a hosted option.

## Contributing

The project is in a controlled pilot, so please open an issue before a large
change. Pull requests should keep the gates above green and the demo tenant
working; never commit real credentials or customer data.

## Licence

MIT — see [LICENSE](LICENSE). Third-party components and their licences are
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
'@

$publicReadmeAr = @'
<div dir="rtl" align="right">

<img src="docs/assets/banner.svg" alt="لافتة المحمود ERP: برنامج محاسبة وتخطيط موارد مؤسسات مفتوح المصدر للشركات الناطقة بالعربية" width="100%">

# المحمود ERP

**برنامج محاسبة وتخطيط موارد مؤسسات مفتوح المصدر للشركات الناطقة بالعربية.**
واجهة عربية وإنجليزية كاملة تبدأ من اليمين إلى اليسار، ويعمل باستضافة ذاتية على
خادم واحد، وقاعدة بياناته PostgreSQL تفرض توازن القيد المزدوج، ورخصته MIT.

[![CI](https://github.com/alshahia/accounting_app/actions/workflows/app-ci.yml/badge.svg?branch=main)](https://github.com/alshahia/accounting_app/actions/workflows/app-ci.yml)
![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.116-009688?logo=fastapi&logoColor=white)
![React 19](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)
![PostgreSQL 18](https://img.shields.io/badge/PostgreSQL-18-4169E1?logo=postgresql&logoColor=white)
![الرخصة MIT](https://img.shields.io/badge/licence-MIT-c9a227)

[English](README.md) · [العربية](README.ar.md) · [llms.txt لوكلاء الذكاء الاصطناعي](llms.txt)

</div>

<div dir="rtl" align="right">

> **الحالة: الإصدار المرشح `v0.2.0-rc1` لتجربة ميدانية محدودة — وليس إصداراً
> عاماً.** ما زال توقيع المحاسب على قبول التجربة، وإثبات التثبيت دون اتصال
> بالإنترنت، من مهام المالك، وقد تتغير الواجهات قبل الإصدار الأول. ولا يحتوي
> هذا المستودع على بيانات عملاء أو أسرار إنتاج.

## ما هو المحمود ERP

**المحمود ERP** نظام مفتوح المصدر للمحاسبة وتخطيط موارد المؤسسات (ERP) موجَّه إلى
الشركات الناطقة بالعربية. يجمع دفتر أستاذ عام بالقيد المزدوج مدعوماً بقاعدة
PostgreSQL — مع فواتير المبيعات والمشتريات، وإدارة النقد والبنوك، والمخزون،
والتقارير المالية — خلف واجهة عربية/إنجليزية تخطيطها الافتراضي من اليمين إلى
اليسار فعلاً. تشغّله بنفسك: خادم واحد عبر Docker Compose، أو تثبيت ويندوز دون
إنترنت، بلا خدمات خارجية وبلا رسوم لكل مستخدم. الشيفرة المصدرية برخصة MIT على
<https://github.com/alshahia/accounting_app>.

باختصار: برنامج محاسبة عربي ونظام ERP صغير باستضافة ذاتية في قاعدة شيفرة واحدة
قابلة للتدقيق.

## لمن هذا النظام

شركات التجارة والتوزيع والتجزئة، والورش والعيادات والمقاولون في الأسواق الناطقة
بالعربية — والمحاسبون الذين يمسكون دفاترها — حيث لا تناسبها الأنظمة السحابية ذات
الواجهة الإنجليزية أولاً.

| إذا كنت… | ما يقدمه لك النظام |
|---|---|
| شركة صغيرة أو متوسطة في سوق عربي | واجهة عربية ومستندات عربية يقرأها فريقك بلا طبقة ترجمة تحتاج صيانة |
| فرعاً أو مستودعاً أو مصنعاً باتصال ضعيف أو بلا إنترنت | حزمة خادم واحد لا تحتاج اتصالاً خارجياً، مع حزمة ومثبّت ويندوز يعملان دون إنترنت |
| محاسباً أو مدققاً | دفاتر قيد مزدوج تفرض قاعدة البيانات توازنها عند الاعتماد، وقيوداً مرحَّلة لا يمكن تعديلها |
| مجموعة بعدة شركات | عدة شركات للمستأجر الواحد معزولة بعزل إجباري على مستوى الصفوف |
| فريق استضافة ذاتية | Docker Compose، وTLS على منفذ واحد، وأدلة مكتوبة للنسخ الاحتياطي والاستعادة والحوادث |
| باحثاً أو وكيل ذكاء اصطناعي | شيفرة صغيرة ومكتملة برخصة MIT، واختباراتها وبوابات التكامل المستمر هي الإثبات المنشور |

## لماذا المحمود ERP

| الميزة الفارقة | ماذا تعني عملياً |
|---|---|
| **العربية أولاً واتجاه RTL بالتصميم** | العربية هي اللغة الافتراضية والتخطيط مبني من اليمين إلى اليسار لا معكوساً بعده، والإنجليزية لغة ثانية كاملة لا طبقة إضافية. |
| **داخل مقر المؤسسة ودون إنترنت** | خادم واحد بلا خدمة خارجية ومنفذ TLS واحد، ومثبّت ويندوز للمواقع ذات الاتصال غير المستقر. تبقى الدفاتر على أجهزتك. |
| **قاعدة البيانات تفرض قواعد الأستاذ** | التحقق المؤجل من توازن المدين والدائن عند الاعتماد، والقيود المرحَّلة غير القابلة للتعديل، وقواعد الفترات المغلقة، وعزل الصفوف لكل مستأجر — قيود ومشغلات في PostgreSQL لا مجرد التزام في التطبيق. |
| **مستندات عربية جاهزة** | فواتير وسندات وإشعارات وتقارير PDF تُظهر العربية بخطوط مضمّنة، وتصدير Excel باتجاه من اليمين إلى اليسار. |
| **تكافؤ اللغتين** | كل شاشة موجودة بالعربية والإنجليزية، وتتغير لغة الواجهة واتجاه الكتابة معاً. |
| **رخصة MIT بلا احتكار** | اقرأ الشيفرة ودقّقها واشتقّ منها وشغّلها تجارياً. المكوّنات الخارجية ورخصها في THIRD_PARTY_NOTICES.md. |

## ما يقدمه النظام

الميزات التالية هي ما يقدمه `v0.2.0-rc1`، وليست خطة مستقبلية.

| المجال | المتوفر في هذا الإصدار المرشح |
|---|---|
| المبيعات | فواتير المبيعات، وإشعارات دائنة (مرتجعات)، وكشوف العملاء، وسندات قبض مع تخصيص الفواتير |
| المشتريات | فواتير الشراء، وإشعارات مدينة، وكشوف الموردين، وسندات دفع مع تخصيص الفواتير |
| النقد والبنوك | حسابات النقد والبنوك، وسندات القبض والدفع، والشيكات، والتحويلات، والمطابقة البنكية، والأرصدة الافتتاحية |
| دفتر الأستاذ | قيود يومية يدوية بدورة حياة: مسودة ← ترحيل ← قيد غير قابل للتعديل، مع العكس والإقفال السنوي |
| التقارير | ميزان المراجعة، والأرباح والخسائر، والميزانية العمومية، وأعمار الذمم المدينة والدائنة، ودفتر الأستاذ العام، والإقرار الضريبي، وتقييم المخزون، وحركة المخزون، وسجل المستندات، وسجل التدقيق |
| المخزون | المستودعات، والتكلفة بالمتوسط المرجح، والجرد والتسويات، والتحويلات بين المستودعات |
| تعدد الشركات | عدة شركات للمستأجر الواحد، والتبديل بينها، وعزل إجباري على مستوى الصفوف |
| المستخدمون والصلاحيات | المستخدمون والأدوار والصلاحيات، ودعوة المستخدمين، والتحقق بخطوتين (TOTP مع رموز استرداد)، وإدارة الجلسات |
| المستندات | ملفات PDF عربية للفواتير وسندات القبض والدفع والإشعارات والتقارير، وتصدير Excel باتجاه من اليمين إلى اليسار |
| الواجهة ثنائية اللغة | واجهة عربية وإنجليزية كاملة، مع RTL أو LTR حسب اللغة |

## لقطات الشاشة

| | |
|---|---|
| ![شاشة تسجيل الدخول بالعربية بتخطيط من اليمين إلى اليسار](docs/assets/screenshots/login-ar.png) | ![قائمة فواتير المبيعات مع حالة الترحيل وزر تنزيل PDF](docs/assets/screenshots/invoices-ar.png) |
| تسجيل الدخول بالعربية، واتجاه RTL افتراضي | فواتير المبيعات مع حالة الترحيل وتنزيل PDF عربي لكل فاتورة |
| ![تقرير ميزان المراجعة بالعربية مع إجماليات مدين ودائن متوازنة](docs/assets/screenshots/trial-balance-ar.png) | ![تقرير ميزان المراجعة نفسه بالإنجليزية من اليسار إلى اليمين](docs/assets/screenshots/trial-balance-en.png) |
| ميزان المراجعة: إجماليات متوازنة، وتفصيل، وتصدير PDF وExcel | التقرير نفسه بالإنجليزية، من اليسار إلى اليمين |

<img src="docs/assets/screenshots/login-mobile.png" alt="شاشة تسجيل الدخول على الهاتف بواجهة عربية من اليمين إلى اليسار" width="280">

## البنية المعمارية

<img src="docs/assets/architecture.svg" alt="البنية: تطبيق صفحة واحدة في المتصفح ← بوابة Nginx بـ TLS ← واجهة FastAPI ← PostgreSQL مع عزل إجباري على مستوى الصفوف" width="100%">

خادم واحد، ومنفذ واحد منشور، وكل ضمان محاسبي مدفوع في PostgreSQL إلى أقصى حد
تسمح به قاعدة البيانات.

- **خادم واحد.** البوابة تنشر منفذاً واحداً فقط، وقاعدة البيانات لا تنشر أي منفذ،
  وترحيلات قاعدة البيانات تعمل كحاوية منفصلة قبل بدء الواجهة البرمجية.
- **عزل المستأجرين في قاعدة البيانات.** كل معاملة طلب تنتقل إلى دور غير مالك مع
  ربط المستأجر، فيُطبَّق عزل الصفوف على مسار الطلب نفسه وليس على المهام الخلفية وحدها.
- **قواعد دفتر الأستاذ في قاعدة البيانات.** قيد مؤجل يتحقق من توازن المدين
  والدائن عند الاعتماد، والقيود المرحَّلة وسطورها غير قابلة للتعديل، وقواعد
  الحسابات والفترات والمصادر مفروضة بمشغلات.
- **واجهة برمجية غير متزامنة.** FastAPI 0.116 على Python 3.14، وSQLAlchemy 2
  غير متزامن، وترحيلات Alembic مع اختبار صعود/هبوط/صعود، واستجابات أخطاء RFC 9457.
- **مستندات عربية.** ملفات PDF عبر ReportLab بخطوط DejaVu/Noto مضمّنة، فتظهر
  العربية بالشكل نفسه داخل الحاوية وعلى ويندوز.

## المكوّنات التقنية

| الطبقة | الاختيار |
|---|---|
| الواجهة الخلفية | Python 3.14 وFastAPI 0.116 وSQLAlchemy 2 غير متزامن وAlembic وPydantic v2 |
| قاعدة البيانات | PostgreSQL 18 مع عزل إجباري على مستوى الصفوف ومشغلات قيود مؤجلة |
| الواجهة الأمامية | React 19 وTypeScript وVite 8 وطبقة i18n خاصة تدعم RTL وLTR |
| الأمان | Argon2id، والتحقق بخطوتين TOTP، ورموز وصول JWT، وكوكيز تحديث دوّارة مع حماية CSRF |
| المستندات | ReportLab لـ PDF وopenpyxl لـ Excel من اليمين إلى اليسار، وSBOM بصيغة CycloneDX |
| النشر | حزمة Docker Compose لخادم واحد، ومثبّت ويندوز يعمل دون إنترنت |
| الجودة | Ruff وPyright strict وpytest وPlaywright وبوابة قائمة الرخص المسموحة وpip-audit |
| التكامل المستمر | GitHub Actions: مهام الخلفية والواجهة والرخص على Ubuntu |

## البدء السريع (بيئة تطوير محلية)

المتطلبات: Python 3.14 مع uv، وNode 24 مع pnpm، وPostgreSQL 18 (خدمة محلية أو Docker).

~~~powershell
# 1. قاعدة بيانات تطوير على 127.0.0.1:55432
pwsh -File deploy/dev-postgres.ps1 start

# 2. تبعيات الخلفية والمخطط والمستأجر التجريبي
uv sync --locked
uv run alembic upgrade head
uv run python scripts/seed_demo.py

# 3. تشغيل الواجهة البرمجية على http://127.0.0.1:8000
uv run uvicorn app.main:app --reload

# 4. تشغيل واجهة الويب على http://127.0.0.1:5173 مع تمرير /api إلى الخلفية
cd web
pnpm install --frozen-lockfile
pnpm dev
~~~

افتح <http://127.0.0.1:5173> وسجّل الدخول بالمستأجر التجريبي الذي أنشأته الخطوة 2
(بيانات التجربة موجودة في [web/e2e/auth.ts](web/e2e/auth.ts)، ونموذج الدخول نفسه
لا يحتوي أي بيانات اعتماد). وثائق OpenAPI متاحة على
<http://127.0.0.1:8000/docs> في بيئة التطوير فقط.

## النشر الإنتاجي على خادمك

~~~powershell
pwsh -File deploy/generate-certs.ps1 -DnsName erp.example.local
$env:POSTGRES_PASSWORD     = '<كلمة مرور قوية>'
$env:MAHMOUD_JWT_SECRET      = '<32 بايت عشوائية على الأقل>'
$env:MAHMOUD_BOOTSTRAP_TOKEN = '<32 بايت عشوائية على الأقل>'
docker compose -f deploy/docker-compose.prod.yml up -d --build
~~~

ثم افتح https://erp.example.local:8443/setup مع رمز التهيئة لإنشاء المستأجر الأول
والمسؤول الأول. قاعدة البيانات غير قابلة للوصول إلا من شبكة Compose، والترحيلات
تعمل قبل أن تبدأ الواجهة باستقبال الطلبات.

## التثبيت دون اتصال بالإنترنت

لخادم ويندوز دون إنترنت، أنشئ حزمة التثبيت على جهاز متصل ثم ثبّت باستخدام
`deploy/windows/install.bat`؛ الإجراءات ومتطلبات الإثبات وخطة التراجع في
[docs/10-runbooks/offline-windows-install.md](docs/10-runbooks/offline-windows-install.md).

التثبيت النظيف على جهاز بمواصفات 2 vCPU / 4 GB إحدى بوابات التجربة المذكورة في
[الحالة وخطة العمل](#الحالة-وخطة-العمل)، لذا هذا المسار موثّق ومكتوب كسكربت، ولم
يُثبَت بعد على جهازك.

## بوابات الجودة

البوابات نفسها تعمل محلياً وفي التكامل المستمر:

~~~powershell
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q                  # اختبارات الوحدة
uv run pytest -q -m integration   # اختبارات التكامل مع PostgreSQL
uv run python scripts/check_licences.py
uv run pip-audit --progress-spinner off
cd web; pnpm typecheck; pnpm build; pnpm test:e2e
~~~

تتضمن مهمة GitHub Actions ثلاث وظائف: **backend** (التنسيق والأنواع واختبارات
الوحدة والتكامل مع PostgreSQL ودورة الترحيل)، و**web** (فحص الأنواع والبناء
واختبارات المتصفح عبر Playwright على الواجهة الحية)، و**licences-sbom** (بوابة
الرخص وتدقيق التبعيات وإنتاج SBOM وإشعارات الأطراف الثالثة). ولتشغيل المرآة
المحلية الكاملة: `pwsh -File scripts/ci-local.ps1`.

## الأمان

- عزل المستأجرين مفروض بـ row-level security في PostgreSQL على مسار كل طلب، ولا
  يستطيع دور التطبيق قراءة صفوف مستأجر آخر.
- كلمات المرور بـ Argon2id، والدخول يدعم TOTP مع رموز استرداد تُستخدم مرة واحدة،
  ورموز الوصول قصيرة العمر، وكوكيز التحديث دوّارة مع فحص CSRF.
- محاولات الدخول محدودة المعدل، ويُقفل الحساب بعد تكرار الفشل.
- القيود المحاسبية المرحَّلة لا يمكن تعديلها أو حذفها؛ التصحيح يكون بقيد عكسي،
  وسجل التدقيق غير قابل للتعديل.
- لا يحتوي المستودع أي بيانات اعتماد. أبلغ عن الثغرات بشكل خاص كما هو موضح في
  [SECURITY.md](SECURITY.md) وليس في مشكلة عامة.

## الأسئلة الشائعة

### ما هو المحمود ERP؟

نظام محاسبة وتخطيط موارد مؤسسات مفتوح المصدر ويعمل باستضافة ذاتية، موجَّه إلى
الشركات الناطقة بالعربية: دفتر أستاذ بالقيد المزدوج، مع المبيعات والمشتريات والنقد
والبنوك والمخزون والتقارير المالية، على PostgreSQL 18، وواجهة عربية/إنجليزية
تتخطّط من اليمين إلى اليسار بالعربية. راجع
[ما هو المحمود ERP](#ما-هو-المحمود-erp).

### هل النظام مجاني ومفتوح المصدر؟

نعم. الرخصة MIT — اقرأ الشيفرة وعدّلها وأعد توزيعها وشغّلها تجارياً. لا توجد نسخة
مغلقة في هذا المستودع، ولا يلزم مفتاح ترخيص للبناء أو التشغيل. المكوّنات الخارجية
ورخصها في [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

### هل يعمل دون اتصال بالإنترنت؟

نعم، بالتصميم وفي التشغيل: الحزمة العاملة لا تستدعي أي خدمة خارجية، ويشمل
المستودع حزمة ومثبّت ويندوز للمواقع ذات الاتصال غير المستقر. وما زالت بوابتان
مفتوحتان — تثبيت نظيف على جهاز ويندوز دون إنترنت بمواصفات 2 vCPU / 4 GB، وإثبات
الذاكرة القصوى على تلك الفئة — فتحقّق من التثبيت على جهازك وفق الدليل.

### ما اللغات التي يدعمها؟

العربية والإنجليزية، وكلتاهما كاملة. العربية هي الافتراضية وتتخطّط من اليمين إلى
اليسار، والإنجليزية من اليسار إلى اليمين. وتتبع المستندات القاعدة نفسها: ملفات
PDF عربية بخطوط مضمّنة وتصدير Excel من اليمين إلى اليسار.

### هل يدعم تعدد العملات؟

نعم. لكل شركة عملة أساسية؛ وللفواتير وإشعارات الدائن والمدين وسندات القبض والدفع
عملتها وسعر صرفها، ويُرحَّل إلى دفتر الأستاذ مبلغ بعملة الأساس إلى جانب مبلغ
المستند. وتُعرض التقارير بعملة الشركة الأساسية.

### ما التقنيات المستخدمة؟

FastAPI 0.116 على Python 3.14 مع SQLAlchemy 2 غير متزامن وAlembic؛ وPostgreSQL 18
بعزل إجباري على مستوى الصفوف؛ وReact 19 وTypeScript وVite 8 في المتصفح؛
وReportLab وopenpyxl لمستندات PDF العربية وExcel من اليمين إلى اليسار؛ وDocker
Compose ومثبّت ويندوز دون إنترنت للنشر.

### هل يمكنني استضافته على خادمي؟

نعم — الاستضافة الذاتية هي نموذج النشر الوحيد. ملف Docker Compose واحد يشغّل بوابة
TLS والواجهة البرمجية وقاعدة البيانات على خادم واحد، ثم يُنشئ
`https://<host>:8443/setup` مع رمز التهيئة المستأجر الأول والمسؤول الأول. وقاعدة
البيانات لا تنشر أي منفذ.

### هل النظام جاهز للإنتاج؟

لا كإصدار عام. `v0.2.0-rc1` إصدار مرشح لتجربة ميدانية محدودة واحدة: ميزات
المحاسبة والمخزون والنقد والتقارير والإدارة أعلاه تعمل، لكن قبول المحاسب على سجلات
تجربة حقيقية، والتثبيت النظيف دون إنترنت على الأجهزة المستهدفة، وإثبات الذاكرة
القصوى، والنسخ الاحتياطي خارج الجهاز، ما زالت بوابات مفتوحة لدى المالك. لا تنقل
دفاتر رسمية إليه قبل توقيع المحاسب.

### أين أجد الوثائق؟

هذا الملف أو [README.md](README.md) للنسخة الإنجليزية؛ وأدلة التشغيل — الإصدار،
والتثبيت دون إنترنت، وتجهيز المستأجر، والنسخ الاحتياطي والاستعادة، والاستجابة
للحوادث، وقبول المحاسب — في [docs/10-runbooks/](docs/10-runbooks/)؛ وملف
[llms.txt](llms.txt) خريطة قابلة للقراءة آلياً، و[CHANGELOG.md](CHANGELOG.md) يبيّن
ما تحتويه هذه اللقطة.

### كيف يقيّمه الباحثون ووكلاء الذكاء الاصطناعي؟

اقرأ [للباحثين ووكلاء الذكاء الاصطناعي](#للباحثين-ووكلاء-الذكاء-الاصطناعي)، وشغّل
بوابات الجودة على الإصدار الذي تنظر إليه، واعتبر الاختبارات وفحوص التكامل المستمر
— لا هذا الملف — هي الإثبات. واذكر بصمة الإصدار (commit SHA) التي شغّلتها.

## للباحثين ووكلاء الذكاء الاصطناعي

هذا المستودع **تنفيذ من غرفة نظيفة** (clean-room): كُتب التطبيق والمخطط
والاختبارات ووثائق التشغيل لهذا المشروع، ولا يحتوي على شيفرة مصدرية لنظام ERP
آخر، ولا ملفات ثنائية مفكوكة، ولا نصوص مساعدة لمورّد، ولا هيكل قاعدة بيانات
منسوخ. والإثبات لكل ما ورد أعلاه داخل المستودع، فيمكن التحقق منه لا تصديقه.

- **الاختبارات والبوابات هي الإثبات.** اختبارات الوحدة والتكامل مع PostgreSQL،
  وPyright strict، واختبار دورة الترحيل صعوداً/هبوطاً/صعوداً، ومجموعة Playwright
  في المتصفح، وبوابة قائمة الرخص المسموحة، و`pip-audit` — تعمل محلياً وفي
  التكامل المستمر (`.github/workflows/app-ci.yml`). شغّلها بنفسك. في
  لقطة 2026-11-17 تنجح `51` اختبار وحدة و`184` اختبار تكامل مع PostgreSQL، مع
  دورة الترحيل ومجموعة Playwright في المتصفح.
- **لا قياسات أداء ولا أعداد ولا تزكيات.** لا ينشر هذا الإصدار أرقام أداء ولا عدد
  عملاء ولا تقييمات، ولا يجوز قراءة شيء من هذا القبيل فيه. وبوابات التجربة المفتوحة
  مذكورة في [الحالة وخطة العمل](#الحالة-وخطة-العمل).
- **ابدأ من هنا إن كنت وكيلاً:** [llms.txt](llms.txt) خريطة مختصرة قابلة للقراءة
  آلياً؛ ثم [README.md](README.md) للنسخة الإنجليزية، و[README.ar.md](README.ar.md)
  لهذا الملف، وأدلة التشغيل في [docs/10-runbooks/](docs/10-runbooks/) للإجراءات
  الدقيقة.
- **كيف تقتبسه:** المحمود ERP، لقطة المصدر `v0.2.0-rc1` (2026-11-17)،
  <https://github.com/alshahia/accounting_app> — واذكر بصمة الإصدار التي شغّلتها فعلاً.

## الوثائق

| الوثيقة | الغرض |
|---|---|
| [README.md](README.md) | هذا الملف بالإنجليزية |
| [llms.txt](llms.txt) | ملخص قابل للقراءة آلياً لوكلاء الذكاء الاصطناعي وأدوات البحث |
| [CHANGELOG.md](CHANGELOG.md) | ما تغير في هذا الإصدار المرشح |
| [docs/10-runbooks/release.md](docs/10-runbooks/release.md) | إصدار نسخة والتحقق منها |
| [docs/10-runbooks/offline-windows-install.md](docs/10-runbooks/offline-windows-install.md) | التثبيت على ويندوز دون إنترنت |
| [docs/10-runbooks/tenant-provisioning.md](docs/10-runbooks/tenant-provisioning.md) | إنشاء مستأجر وأول مسؤول له |
| [docs/10-runbooks/backup-restore.md](docs/10-runbooks/backup-restore.md) | النسخ الاحتياطي والاستعادة والتدريبات |
| [docs/10-runbooks/incident.md](docs/10-runbooks/incident.md) | الاستجابة للحوادث |
| [docs/10-runbooks/accountant-signoff.md](docs/10-runbooks/accountant-signoff.md) | جولة قبول المحاسب |
| [SECURITY.md](SECURITY.md) | الإبلاغ الخاص عن الثغرات |
| [docs/09-decisions/0002-phase-0-licence-exceptions.md](docs/09-decisions/0002-phase-0-licence-exceptions.md) | قرارات رخص التبعيات |

## بنية المشروع

~~~
app/                 تطبيق FastAPI (الوحدات، واجهة API، النواة، قاعدة البيانات)
migrations/          سلسلة ترحيلات Alembic
tests/               اختبارات الوحدة والتكامل مع PostgreSQL
web/                 تطبيق React وTypeScript وVite مع اختبارات Playwright
deploy/              Dockerfile وحزم Compose وnginx ومثبّت ويندوز
scripts/             أدوات البذر والقبول والرخص وSBOM والنسخ الاحتياطي
docs/                أدلة التشغيل وقرار الرخصة
.github/workflows/   ملف التكامل المستمر
~~~

## الحالة وخطة العمل

- **مكتمل في `v0.2.0-rc1`:** قطاعات المحاسبة والمشتريات والنقد والمخزون
  والتقارير أعلاه، وواجهة عربية/إنجليزية بتخطيط RTL، والتحقق بخطوتين، وعزل تعدد
  الشركات، وتدريبات النسخ الاحتياطي والاستعادة، وحزم Docker وويندوز دون إنترنت،
  ومجموعة بوابات التكامل المستمر.
- **بوابات التجربة الميدانية المتبقية:** قبول المحاسب على سجلات تجربة حقيقية،
  وتثبيت نظيف دون إنترنت على جهاز بمواصفات 2 vCPU / 4 GB، وإثبات الذروة
  للذاكرة على هذه الفئة من الأجهزة، ونسخ احتياطي خارج الجهاز. ويبقى الإصدار
  مرشحاً حتى توقيع هذه البنود.
- **بعد التجربة:** توحيد الفروع المتعددة، وولايات قضائية وصيغ تقارير إضافية،
  وخيار الاستضافة.

## المساهمة

المشروع في مرحلة تجربة محدودة، لذا يُرجى فتح مشكلة قبل أي تغيير كبير. يجب أن
تبقى البوابات أعلاه ناجحة والمستأجر التجريبي يعمل، ولا تُضِف بيانات اعتماد حقيقية
أو بيانات عملاء إلى المستودع.

## الرخصة

MIT — راجع [LICENSE](LICENSE). المكوّنات الخارجية ورخصها مذكورة في
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

</div>
'@
$publicLlmsTxt = @'
# Al-Mahmoud ERP

> Al-Mahmoud ERP (المحمود ERP) is an open-source, bilingual Arabic/English accounting and ERP system for Arabic-speaking businesses: RTL-first, self-hosted on a single node on PostgreSQL, MIT licensed.

Al-Mahmoud ERP is accounting software with a database-enforced double-entry ledger,
sales invoicing, purchasing, cash and bank management, inventory and financial
reporting. It is a release candidate for a controlled pilot, not general
availability: the accountant sign-off on real pilot records and the clean
offline-install evidence are still open owner gates. This file is a compact,
machine-readable map for AI agents and research tools. It summarises the product
and links the authoritative documents instead of duplicating them; the
repository's tests and CI gates, not this text, are the evidence for the claims
below.

## Key facts

- Product: Al-Mahmoud ERP (Arabic: المحمود ERP)
- Category: open-source accounting software and small-business ERP
- Version: v0.2.0-rc1, release candidate for a controlled pilot (not general availability)
- Licence: MIT — see LICENSE
- Languages: Arabic (default, right-to-left) and English (left-to-right), full parity
- Deployment: self-hosted, single node, Docker Compose; offline Windows bundle and installer; no external services required
- Database: PostgreSQL 18 with forced per-tenant row-level security and deferred debit-equals-credit constraints
- Stack: FastAPI 0.116 on Python 3.14, SQLAlchemy 2 async, Alembic, Pydantic v2; React 19, TypeScript and Vite 8; ReportLab and openpyxl for PDF and Excel documents
- Repository: https://github.com/alshahia/accounting_app
- Data: the repository contains no customer data and no production secrets

## What it does

- Ledger: double-entry journal entries with a draft to post to immutable lifecycle, reversal, year-end close, and database-enforced balance at commit
- Sales: invoices, credit notes (returns), customer statements, receipts with invoice allocation
- Purchasing: bills, debit notes, supplier statements, payments with bill allocation
- Cash and banks: cash and bank accounts, cheques, transfers, bank reconciliation, opening balances
- Inventory: warehouses, weighted-average costing, stock counts, adjustments, transfers
- Reports: trial balance, profit and loss, balance sheet, AR/AP aging, general ledger, tax return, inventory valuation, stock movement, document register, audit trail
- Documents: Arabic PDFs with embedded fonts, and right-to-left Excel exports
- Administration: several companies per tenant, users, roles and permissions, invitations, MFA (TOTP with recovery codes), sessions

## Start here

- [README.md](https://github.com/alshahia/accounting_app/blob/main/README.md): English overview, features, architecture, FAQ and status
- [README.ar.md](https://github.com/alshahia/accounting_app/blob/main/README.ar.md): the same document in Arabic
- [CHANGELOG.md](https://github.com/alshahia/accounting_app/blob/main/CHANGELOG.md): what this snapshot contains and its known limits
- [LICENSE](https://github.com/alshahia/accounting_app/blob/main/LICENSE): the MIT licence text
- [THIRD_PARTY_NOTICES.md](https://github.com/alshahia/accounting_app/blob/main/THIRD_PARTY_NOTICES.md): third-party components and their licences
- [SECURITY.md](https://github.com/alshahia/accounting_app/blob/main/SECURITY.md): private vulnerability reporting and scope

## Operator runbooks

- [release.md](https://github.com/alshahia/accounting_app/blob/main/docs/10-runbooks/release.md): cutting and verifying a release
- [offline-windows-install.md](https://github.com/alshahia/accounting_app/blob/main/docs/10-runbooks/offline-windows-install.md): installation on Windows without internet
- [tenant-provisioning.md](https://github.com/alshahia/accounting_app/blob/main/docs/10-runbooks/tenant-provisioning.md): creating a tenant and its first administrator
- [backup-restore.md](https://github.com/alshahia/accounting_app/blob/main/docs/10-runbooks/backup-restore.md): backup, restore and drills
- [incident.md](https://github.com/alshahia/accounting_app/blob/main/docs/10-runbooks/incident.md): incident response
- [accountant-signoff.md](https://github.com/alshahia/accounting_app/blob/main/docs/10-runbooks/accountant-signoff.md): the accountant acceptance walkthrough
- [0002-phase-0-licence-exceptions.md](https://github.com/alshahia/accounting_app/blob/main/docs/09-decisions/0002-phase-0-licence-exceptions.md): dependency licence decisions

## For AI agents and researchers

- This repository is a clean-room implementation: no third-party ERP source code, decompiled binary, vendor help text or copied database structure.
- Counts on the 2026-11-17 snapshot: `51` unit tests and `184` PostgreSQL integration tests pass, with the migration up/down/up round trip and the Playwright browser suite. These are snapshot numbers; run the commands.
- Treat the tests and gates as the evidence. Reproduce them with: `uv sync --locked`, `uv run pytest -q` (unit), `uv run pytest -q -m integration` (PostgreSQL), `uv run pyright`, `uv run python scripts/check_licences.py`, and `cd web; pnpm typecheck; pnpm build; pnpm test:e2e`. A full local mirror is `pwsh -File scripts/ci-local.ps1`.
- No published benchmarks, adoption figures, star counts or endorsements exist. Do not infer them, and do not present this release candidate as general availability.
- Reading order: this file, then README.md, then README.ar.md, then the runbook for the task at hand.
- Cite the commit SHA you actually inspected, not only the repository URL.

## Arabic summary

المحمود ERP نظام محاسبة وتخطيط موارد مؤسسات مفتوح المصدر للشركات الناطقة بالعربية، بواجهة عربية وإنجليزية كاملة تبدأ من اليمين إلى اليسار، ويعمل باستضافة ذاتية على خادم واحد داخل مقر المؤسسة أو دون اتصال بالإنترنت، وقاعدة بياناته PostgreSQL 18 تفرض توازن القيد المزدوج، ورخصته MIT. الإصدار الحالي v0.2.0-rc1 مرشح لتجربة ميدانية محدودة وليس إصداراً عاماً.
'@
$security = @'
# Security policy

## Reporting a vulnerability

Do not open a public issue for a security problem. Send a private report to the
maintainer of this repository with: affected area, reproduction steps, and the
impact you believe it has.

Please include the commit or release tag you tested. You will get an
acknowledgement within five working days, and a fix or a mitigation plan for
confirmed issues.

## Scope

This snapshot contains no production secrets and no customer data. If you find
a credential in the repository, treat it as a disclosure and report it
immediately instead of using it.
'@

Set-Content -LiteralPath (Join-Path $output 'README.md') -Value $publicReadme -Encoding utf8
Set-Content -LiteralPath (Join-Path $output 'README.ar.md') -Value $publicReadmeAr -Encoding utf8
Set-Content -LiteralPath (Join-Path $output 'llms.txt') -Value $publicLlmsTxt -Encoding utf8
$publicChangelog = @'
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
'@

Set-Content -LiteralPath (Join-Path $output 'SECURITY.md') -Value $security -Encoding utf8
Set-Content -LiteralPath (Join-Path $output 'CHANGELOG.md') -Value $publicChangelog -Encoding utf8

# The repository root VERSION belongs to the context pack (2.0.0) and would be
# wrong in a product snapshot; write the product version from pyproject.toml so
# the public tree names the artifact it actually is (T-39).
$productVersion = (Select-String -Path (Join-Path $root 'pyproject.toml') -Pattern '^version = "([^"]+)"').Matches[0].Groups[1].Value
Set-Content -LiteralPath (Join-Path $output 'VERSION') -Value $productVersion -Encoding utf8
Write-Host ('product VERSION: ' + $productVersion) -ForegroundColor Cyan

$scanPath = Join-Path $root 'scripts/secret-scan.ps1'
Write-Host 'running the secret gate on the snapshot' -ForegroundColor Cyan
& pwsh -NoProfile -File $scanPath -Root $output
if ($LASTEXITCODE -ne 0) { throw 'the public snapshot contains a credential; not publishing it' }

$internalDirectories = @('analysis/', 'work/', 'review-room/', 'rules/', '_templates/', '.cursor/')
$internalMentions = @([string]::Join('', @('alameen', 'soft')), [string]::Join('', @('Al-Ameen', ' 9.23')), 'review-room/', 'analysis/19-')
$internalHits = New-Object System.Collections.Generic.List[string]

Push-Location $output
try {
    $snapshotFiles = @(git ls-files)
}
finally {
    Pop-Location
}
foreach ($snapshotFile in $snapshotFiles) {
    $normalised = $snapshotFile.Replace('\', '/')
    foreach ($directory in $internalDirectories) {
        if ($normalised.StartsWith($directory) -or $normalised.Contains('/' + $directory)) {
            $internalHits.Add($normalised + ' -> internal directory ' + $directory)
        }
    }
}

$contentFiles = @(Get-ChildItem -LiteralPath $output -Recurse -File | Where-Object {
    $_.Extension -in @('.md', '.html', '.txt')
})
foreach ($file in $contentFiles) {
    $text = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
    if ($null -eq $text) { continue }
    foreach ($needle in $internalMentions) {
        if ($text.Contains($needle, [System.StringComparison]::OrdinalIgnoreCase)) {
            $internalHits.Add($file.FullName.Substring($output.Length + 1) + ' -> ' + $needle)
        }
    }
}
if ($internalHits.Count -gt 0) {
    Write-Host 'internal material in the snapshot:' -ForegroundColor Red
    $internalHits | Select-Object -First 20
    throw 'the public snapshot still mentions internal research material'
}
Write-Host 'internal-material gate: clean' -ForegroundColor Green

Push-Location $output
try {
    git init --initial-branch=main | Out-Null
    # A neutral identity that resolves to no GitHub account. Do NOT use a
    # *@users.noreply.github.com address here: GitHub reads the local part as a
    # username, and 'build@users.noreply.github.com' attributed every snapshot
    # commit to the unrelated account https://github.com/build (seen 2026-10-27).
    git config user.name 'Al-Mahmoud Build'
    git config user.email 'build@almahmoud.invalid'
    git add -A
    git -c user.name='Al-Mahmoud Build' -c user.email='build@almahmoud.invalid' commit -m 'Al-Mahmoud ERP v0.2.0-rc1: public source snapshot' | Out-Null
    $count = @(git ls-files).Count
    $sizeMb = [math]::Round((Get-ChildItem -Recurse -File -Force | Measure-Object Length -Sum).Sum / 1MB, 2)
}
finally {
    Pop-Location
}

Write-Host ''
Write-Host ('public snapshot ready: ' + $output) -ForegroundColor Green
Write-Host ('files: ' + $count + '  size: ' + $sizeMb + ' MB  (research, review and agent material excluded)')
Write-Host ''
Write-Host 'Next steps (the account and the push are yours to do):'
Write-Host ('  1. create the repository on github.com: + -> New repository -> name ' + $RepositoryName)
Write-Host '  2. in the snapshot:'
Write-Host ('       cd ' + $OutputDirectory)
Write-Host ('       git remote add origin https://github.com/<your-account>/' + $RepositoryName + '.git')
Write-Host '       git push -u origin main'
Write-Host '  3. verify on the repository page that README.md, README.ar.md, llms.txt, LICENSE and SECURITY.md render,'
Write-Host '     that the docs/assets/ images load, and that'
Write-Host '     the file list contains no analysis/, work/ or review-room/ directory.'
Write-Host ''
Write-Host 'For a private repository of the whole working tree instead, see docs/10-runbooks/github-publish.md.'
