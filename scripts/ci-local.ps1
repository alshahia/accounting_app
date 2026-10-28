#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Run the three app-ci jobs locally, in the order the workflow runs them.

.DESCRIPTION
  A local mirror of .github/workflows/app-ci.yml, for the days the workflow has
  not run yet or has to be debugged without pushing. It is *not* a substitute
  for a green workflow run on the tagged commit: it runs on this machine, with
  whatever this machine has installed.

  The database URLs default to the local development cluster and can be
  overridden to reproduce the CI service container:

    $env:MAHMOUD_DATABASE_URL      owner role used by alembic and the seed
    $env:MAHMOUD_TEST_DATABASE_URL login role used by the integration suite

  The CI emulation used on 2026-10-26 was:

    $psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
    & $psql -h 127.0.0.1 -p 55432 -U postgres -d postgres -c "CREATE ROLE almahmoud LOGIN SUPERUSER PASSWORD 'postgres'"
    & $psql -h 127.0.0.1 -p 55432 -U postgres -d postgres -c "CREATE DATABASE almahmoud_test OWNER almahmoud"
    $env:MAHMOUD_DATABASE_URL      = 'postgresql+asyncpg://almahmoud:postgres@127.0.0.1:55432/almahmoud_test'
    $env:MAHMOUD_TEST_DATABASE_URL = $env:MAHMOUD_DATABASE_URL

.EXAMPLE
  ./scripts/ci-local.ps1
#>
[CmdletBinding()]
param(
    [string] $DatabaseUrl = 'postgresql+asyncpg://almahmoud:postgres@127.0.0.1:55432/almahmoud_test',
    [string] $TestDatabaseUrl = 'postgresql+asyncpg://almahmoud:postgres@127.0.0.1:55432/almahmoud_test'
)

$ErrorActionPreference = 'Stop'
$Root = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $Root

$env:MAHMOUD_ENVIRONMENT = 'test'
$env:MAHMOUD_DATABASE_URL = $DatabaseUrl
$env:MAHMOUD_TEST_DATABASE_URL = $TestDatabaseUrl

$results = [System.Collections.Generic.List[object]]::new()

function Step([string] $Job, [string] $Name, [scriptblock] $Body) {
    $started = Get-Date
    Write-Host ""
    Write-Host "== $Job / $Name" -ForegroundColor Cyan
    try {
        & $Body
        if ($LASTEXITCODE -ne 0) { throw "exit code $LASTEXITCODE" }
        $results.Add([pscustomobject]@{
            Job = $Job; Step = $Name; Result = 'pass'
            Seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
        })
        Write-Host "   pass" -ForegroundColor Green
    }
    catch {
        $results.Add([pscustomobject]@{
            Job = $Job; Step = $Name; Result = 'FAIL'
            Seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
        })
        Write-Host "   FAIL: $_" -ForegroundColor Red
    }
}

function Run([string] $Executable, [string[]] $Arguments) {
    & $Executable @Arguments
}

Step 'backend' 'ruff format' { Run 'uv' @('run', 'ruff', 'format', '--check', '.') }
Step 'backend' 'ruff check' { Run 'uv' @('run', 'ruff', 'check', '.') }
Step 'backend' 'pyright' { Run 'uv' @('run', 'pyright') }
Step 'backend' 'unit tests' { Run 'uv' @('run', 'pytest', '-q') }
# The roundtrip runs on a scratch database: the development database may hold
# business rows that migrations below 0009 legitimately delete, exactly as CI's
# empty service container has none. See scripts/migration-roundtrip.py.
Step 'backend' 'migration roundtrip' { Run 'uv' @('run', 'python', 'scripts/migration-roundtrip.py') }
Step 'backend' 'integration tests' { Run 'uv' @('run', 'pytest', '-q', '-m', 'integration') }
Step 'backend' 'analysis and docs check' { Run 'uv' @('run', 'python', 'work/scripts/check_analysis.py', '--root', '.', '--evidence', '--docs') }

Step 'web' 'pnpm install' { Run 'pnpm' @('install', '--frozen-lockfile', '--dir', 'web') }
Step 'web' 'typecheck' { Run 'pnpm' @('--dir', 'web', 'typecheck') }
Step 'web' 'build' { Run 'pnpm' @('--dir', 'web', 'build') }
Step 'web' 'schema for the browser tests' { Run 'uv' @('run', 'alembic', 'upgrade', 'head') }
Step 'web' 'seed demo for browser tests' { Run 'uv' @('run', 'python', 'scripts/seed_demo.py') }
Step 'web' 'browser tests' { Run 'pnpm' @('--dir', 'web', 'test:e2e') }

Step 'licences-sbom' 'licence allow-list' { Run 'uv' @('run', 'python', 'scripts/check_licences.py', '--quiet') }
Step 'licences-sbom' 'dependency audit' { Run 'uv' @('run', 'pip-audit', '--progress-spinner', 'off') }
Step 'licences-sbom' 'sbom and notices' { Run 'uv' @('run', 'python', 'scripts/generate_sbom.py') }

Write-Host ""
$results | Format-Table -AutoSize
$failed = @($results | Where-Object { $_.Result -ne 'pass' })
if ($failed.Count -gt 0) {
    Write-Host "$($failed.Count) step(s) FAILED" -ForegroundColor Red
    exit 1
}
Write-Host "ci-local-ok: $($results.Count) steps" -ForegroundColor Green
exit 0
