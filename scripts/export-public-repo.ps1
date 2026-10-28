<#
.SYNOPSIS
  Build a sanitised, publishable snapshot of the application source.
.DESCRIPTION
  The working tree also holds the product specification, the read-only study of
  the vendor suite, review-room transcripts and agent rules. None of that is
  part of a public repository. This script copies only the application source,
  tests, deployment assets and operator runbooks into a fresh git repository,
  writes a public README and SECURITY.md, runs the secret and internal-mention
  gates, and makes a single commit authored as a neutral build identity.

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

<img src="docs/assets/banner.svg" alt="Al-Mahmoud ERP — accounting-first software for Arabic-speaking businesses" width="100%">

# Al-Mahmoud ERP

**Accounting-first ERP for Arabic-speaking businesses.** Arabic and English,
RTL-first, multi-currency, on-premise, and built so the books provably balance.

[![CI](https://github.com/alshahia/accounting_app/actions/workflows/app-ci.yml/badge.svg?branch=main)](https://github.com/alshahia/accounting_app/actions/workflows/app-ci.yml)
![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.116-009688?logo=fastapi&logoColor=white)
![React 19](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)
![PostgreSQL 18](https://img.shields.io/badge/PostgreSQL-18-4169E1?logo=postgresql&logoColor=white)
![Licence MIT](https://img.shields.io/badge/licence-MIT-c9a227)

[English](README.md) · [العربية](README.ar.md)

</div>

> **Status: `v0.2.0-rc1` release candidate for a controlled pilot — not general
> availability.** The pilot accountant sign-off and the clean offline-install
> evidence are still owner steps, and interfaces may change before v1. The
> repository contains no customer data and no production secrets.

## Why this exists

Small and medium businesses across the Arabic-speaking market keep their books in
Arabic, often on machines that are offline, and have to prove that every posting
balances. Al-Mahmoud ERP is built around those three facts:

- **Arabic first.** The interface defaults to Arabic with a true right-to-left
  layout; English is a first-class second language, not a translation overlay.
- **On-premise.** One node, no external services, a single TLS entry point, and an
  offline Windows installer for sites without dependable internet.
- **Provable books.** The ledger invariants live in the database — deferred
  debit-equals-credit triggers, immutable posted entries, closed-period checks,
  per-tenant row-level security — instead of being trusted to the application.

## Screenshots

| | |
|---|---|
| ![Arabic sign-in](docs/assets/screenshots/login-ar.png) | ![Sales invoices](docs/assets/screenshots/invoices-ar.png) |
| Arabic-first sign-in, RTL by default | Sales invoices with posting state and per-invoice PDF |
| ![Trial balance in Arabic](docs/assets/screenshots/trial-balance-ar.png) | ![Trial balance in English](docs/assets/screenshots/trial-balance-en.png) |
| Trial balance: balanced totals, drill-down, PDF/Excel export | The same screen in English, rendered left-to-right |

<img src="docs/assets/screenshots/login-mobile.png" alt="Sign-in on a phone" width="280">

## What it does

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

## Architecture

<img src="docs/assets/architecture.svg" alt="Browser SPA → Nginx TLS gateway → FastAPI API → PostgreSQL with forced row-level security" width="100%">

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

## Production artifact

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

For a Windows server without internet, build the offline bundle on a connected
machine and install with `deploy/windows/install.bat`; the procedure, evidence
requirements and rollback are in
[docs/10-runbooks/offline-windows-install.md](docs/10-runbooks/offline-windows-install.md).

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

## Documentation

| Document | For |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | release notes for this source snapshot |
| [docs/10-runbooks/release.md](docs/10-runbooks/release.md) | cutting and verifying a release |
| [docs/10-runbooks/offline-windows-install.md](docs/10-runbooks/offline-windows-install.md) | the offline Windows installation |
| [docs/10-runbooks/tenant-provisioning.md](docs/10-runbooks/tenant-provisioning.md) | creating a tenant and its first administrator |
| [docs/10-runbooks/backup-restore.md](docs/10-runbooks/backup-restore.md) | backup, restore and drills |
| [docs/10-runbooks/incident.md](docs/10-runbooks/incident.md) | incident response |
| [docs/10-runbooks/accountant-signoff.md](docs/10-runbooks/accountant-signoff.md) | the accountant acceptance walkthrough |
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

<img src="docs/assets/banner.svg" alt="المحمود ERP — نظام محاسبة للشركات الناطقة بالعربية" width="100%">

# المحمود ERP

**نظام محاسبة ومبيعات للشركات الناطقة بالعربية.** واجهة عربية أولاً، واتجاه
كتابة من اليمين إلى اليسار، ومتعدد العملات، ويعمل محلياً على خادم واحد، ومبني
بحيث تكون القيود متوازنة وقابلة للإثبات.

[![CI](https://github.com/alshahia/accounting_app/actions/workflows/app-ci.yml/badge.svg?branch=main)](https://github.com/alshahia/accounting_app/actions/workflows/app-ci.yml)
![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.116-009688?logo=fastapi&logoColor=white)
![React 19](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)
![PostgreSQL 18](https://img.shields.io/badge/PostgreSQL-18-4169E1?logo=postgresql&logoColor=white)
![الرخصة MIT](https://img.shields.io/badge/licence-MIT-c9a227)

[English](README.md) · [العربية](README.ar.md)

</div>

<div dir="rtl" align="right">

> **الحالة: الإصدار المرشح `v0.2.0-rc1` لتجربة ميدانية محدودة — وليس إصداراً
> عاماً.** ما زال توقيع المحاسب على قبول التجربة، وإثبات التثبيت دون اتصال
> بالإنترنت، من مهام المالك، وقد تتغير الواجهات قبل الإصدار الأول. ولا يحتوي
> هذا المستودع على بيانات عملاء أو أسرار إنتاج.

## لماذا هذا النظام

تدير الشركات الصغيرة والمتوسطة في السوق العربي حساباتها باللغة العربية، وغالباً
على أجهزة غير متصلة بالإنترنت، وتحتاج إلى إثبات أن كل قيد متوازن. بُني المحمود ERP
حول هذه الحقائق الثلاث:

- **العربية أولاً.** الواجهة عربية افتراضياً بتخطيط حقيقي من اليمين إلى اليسار،
  والإنجليزية لغة ثانية كاملة وليست طبقة ترجمة.
- **يعمل محلياً.** خادم واحد دون خدمات خارجية، ومنفذ واحد مؤمَّن بـ TLS، ومثبّت
  ويندوز يعمل دون إنترنت للمواقع ذات الاتصال غير المستقر.
- **قيود قابلة للإثبات.** قواعد دفتر الأستاذ مفروضة في قاعدة البيانات — قيد
  مؤجل للتحقق من توازن المدين والدائن، ومنع تعديل القيود المرحَّلة، وفحص الفترات
  المغلقة، وعزل الصفوف لكل مستأجر — بدلاً من الاعتماد على التطبيق وحده.

## لقطات الشاشة

| | |
|---|---|
| ![تسجيل الدخول بالعربية](docs/assets/screenshots/login-ar.png) | ![فواتير المبيعات](docs/assets/screenshots/invoices-ar.png) |
| تسجيل الدخول بالعربية، واتجاه RTL افتراضي | فواتير المبيعات مع حالة الترحيل وتنزيل PDF لكل فاتورة |
| ![ميزان المراجعة بالعربية](docs/assets/screenshots/trial-balance-ar.png) | ![ميزان المراجعة بالإنجليزية](docs/assets/screenshots/trial-balance-en.png) |
| ميزان المراجعة: إجماليات متوازنة، وتفصيل، وتصدير PDF/Excel | الشاشة نفسها بالإنجليزية، من اليسار إلى اليمين |

<img src="docs/assets/screenshots/login-mobile.png" alt="تسجيل الدخول على الهاتف" width="280">

## ما يقدمه النظام

| المجال | المتوفر في هذا الإصدار المرشح |
|---|---|
| المبيعات | فواتير المبيعات، وإشعارات دائنة (مرتجعات)، وكشوف العملاء، وسندات قبض مع تخصيص الفواتير |
| المشتريات | فواتير الشراء، وإشعارات مدينة، وكشوف الموردين، وسندات دفع مع تخصيص الفواتير |
| النقد والبنوك | حسابات النقد والبنوك، وسندات القبض والدفع، والشيكات، والتحويلات، والمطابقة البنكية، والأرصدة الافتتاحية |
| دفتر الأستاذ | قيود يومية يدوية بدورة حياة: مسودة ← ترحيل ← قيد غير قابل للتعديل، مع العكس والإقفال السنوي |
| التقارير | ميزان المراجعة، والأرباح والخسائر، والميزانية العمومية، وأعمار الذمم المدينة والدائنة، ودفتر الأستاذ العام، والإقرار الضريبي، وتقييم المخزون، وحركة المخزون، وسجل المستندات، وسجل التدقيق |
| المخزون | المستودعات، والتكلفة المرجحة، والجرد والتسويات، والتحويلات بين المستودعات |
| تعدد الشركات | عدة شركات للمستأجر الواحد، والتبديل بينها، وعزل إجباري على مستوى الصفوف |
| المستخدمون والصلاحيات | المستخدمون والأدوار والصلاحيات، ودعوة المستخدمين، والتحقق بخطوتين (TOTP مع رموز استرداد)، وإدارة الجلسات |
| المستندات | ملفات PDF عربية للفواتير وسندات القبض والدفع والإشعارات والتقارير، وتصدير Excel باتجاه من اليمين إلى اليسار |
| الواجهة ثنائية اللغة | واجهة عربية وإنجليزية كاملة، مع RTL أو LTR حسب اللغة |

## البنية المعمارية

<img src="docs/assets/architecture.svg" alt="المتصفح ← بوابة Nginx بـ TLS ← واجهة FastAPI ← PostgreSQL مع عزل إجباري على مستوى الصفوف" width="100%">

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

## نسخة الإنتاج

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

لخادم ويندوز دون إنترنت، أنشئ حزمة التثبيت على جهاز متصل ثم ثبّت باستخدام
`deploy/windows/install.bat`؛ الإجراءات ومتطلبات الإثبات وخطة التراجع في
[docs/10-runbooks/offline-windows-install.md](docs/10-runbooks/offline-windows-install.md).

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

## الوثائق

| الوثيقة | الغرض |
|---|---|
| [CHANGELOG.md](CHANGELOG.md) | ما تغير في هذا الإصدار المرشح |
| [docs/10-runbooks/release.md](docs/10-runbooks/release.md) | إصدار نسخة والتحقق منها |
| [docs/10-runbooks/offline-windows-install.md](docs/10-runbooks/offline-windows-install.md) | التثبيت على ويندوز دون إنترنت |
| [docs/10-runbooks/tenant-provisioning.md](docs/10-runbooks/tenant-provisioning.md) | إنشاء مستأجر وأول مسؤول له |
| [docs/10-runbooks/backup-restore.md](docs/10-runbooks/backup-restore.md) | النسخ الاحتياطي والاستعادة والتدريبات |
| [docs/10-runbooks/incident.md](docs/10-runbooks/incident.md) | الاستجابة للحوادث |
| [docs/10-runbooks/accountant-signoff.md](docs/10-runbooks/accountant-signoff.md) | جولة قبول المحاسب |
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
$publicChangelog = @'
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
'@

Set-Content -LiteralPath (Join-Path $output 'SECURITY.md') -Value $security -Encoding utf8
Set-Content -LiteralPath (Join-Path $output 'CHANGELOG.md') -Value $publicChangelog -Encoding utf8

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
Write-Host '  3. verify on the repository page that README.md, README.ar.md, LICENSE and SECURITY.md render,'
Write-Host '     that the docs/assets/ images load, and that'
Write-Host '     the file list contains no analysis/, work/ or review-room/ directory.'
Write-Host ''
Write-Host 'For a private repository of the whole working tree instead, see docs/10-runbooks/github-publish.md.'
