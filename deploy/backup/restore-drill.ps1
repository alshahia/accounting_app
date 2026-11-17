# Nightly restore drill (T-34, AC-18/AC-19).
#
#   pwsh -File deploy/backup/restore-drill.ps1
#   pwsh -File deploy/backup/restore-drill.ps1 -ComposeFile deploy/docker-compose.prod.yml -ComposeEnvFile .local/pilot.env -ComposeProject almahmoud-erp-prod
#
# Picks the newest encrypted dump, decrypts it to a temporary file, restores it
# into a scratch database, and compares the per-account trial balance, the
# journal totals and the migration revision with the values recorded in the
# backup manifest at backup time. A green or red JSON report is written to
# deploy/backup/reports (tracked), and every attempt is appended to the access
# log. Exit code 0 means the restore reproduced the recorded state exactly.
#
# Deployment mode restores inside the compose "db" service, so the production
# stack can be verified without publishing a database port on the host.
param(
    [string]$DatabaseUrl = $env:MAHMOUD_BACKUP_DATABASE_URL,
    [string]$ComposeFile = '',
    [string]$ComposeEnvFile = '',
    [string]$ComposeProject = '',
    [string]$Service = 'db',
    [string]$Database = 'almahmoud',
    [string]$DbUser = 'almahmoud',
    [string]$BackupDir = (Join-Path $PSScriptRoot 'dumps'),
    [string]$KeyFile = (Join-Path $PSScriptRoot 'keys\backup.key'),
    [string]$ReportsDir = (Join-Path $PSScriptRoot 'reports'),
    [string]$ScratchDatabase = 'almahmoud_restore_drill',
    [string]$PgBin = $env:MAHMOUD_PG_BIN,
    [switch]$KeepScratchDatabase
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$dockerMode = $ComposeFile -ne ''
if (-not $dockerMode -and [string]::IsNullOrWhiteSpace($DatabaseUrl)) {
    throw 'pass -DatabaseUrl for a host/server database, or -ComposeFile for a deployment via docker compose'
}
if ($dockerMode -and -not (Test-Path -LiteralPath $ComposeFile)) { throw "compose file not found: $ComposeFile" }
if ($dockerMode -and $ComposeEnvFile -ne '' -and -not (Test-Path -LiteralPath $ComposeEnvFile)) { throw "compose env file not found: $ComposeEnvFile" }

$pg = $null
$connection = $null
if (-not $dockerMode) {
    $pg = Find-PgBin -Explicit $PgBin
    $connection = ConvertFrom-MahmoudUrl -Url $DatabaseUrl
}
$keyMaterial = Read-KeyMaterial -KeyFile $KeyFile
New-Item -ItemType Directory -Force $ReportsDir | Out-Null
$logPath = Join-Path $BackupDir 'backup-access.log'
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$startedAt = Get-Date

function Get-ComposeArgumentList {
    $list = New-Object System.Collections.Generic.List[string]
    $list.Add('compose')
    if ($ComposeProject -ne '') { $list.Add('-p'); $list.Add($ComposeProject) }
    if ($ComposeEnvFile -ne '') { $list.Add('--env-file'); $list.Add($ComposeEnvFile) }
    $list.Add('-f'); $list.Add($ComposeFile)
    return [string[]]$list.ToArray()
}

function Invoke-Compose([string[]]$Arguments, [switch]$AllowFailure) {
    $all = @(Get-ComposeArgumentList) + $Arguments
    $output = & docker @all 2>&1
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
        throw ('docker ' + ($all -join ' ') + ' failed (' + $LASTEXITCODE + '): ' + ($output -join [Environment]::NewLine))
    }
    return (($output | ForEach-Object { "$_" }) -join [Environment]::NewLine).Trim()
}

function Invoke-DatabaseSql([string]$DatabaseName, [string]$Sql, [string]$Url) {
    if ($dockerMode) {
        return (Invoke-Compose (@('exec', '-T', $Service, 'psql', '-U', $DbUser, '-d', $DatabaseName, '-t', '-A', '-c', $Sql))).Trim()
    }
    return (Invoke-Psql -PgBin $pg -Url $Url -Sql $Sql -TuplesOnly).Trim()
}

function New-ScratchDatabase {
    if ($dockerMode) {
        Invoke-Compose (@('exec', '-T', $Service, 'psql', '-U', $DbUser, '-d', 'postgres', '-c', ('DROP DATABASE IF EXISTS ' + $ScratchDatabase + ' WITH (FORCE);'))) -AllowFailure | Out-Null
        Invoke-Compose (@('exec', '-T', $Service, 'psql', '-U', $DbUser, '-d', 'postgres', '-c', ('CREATE DATABASE ' + $ScratchDatabase + ';'))) | Out-Null
        return
    }
    $adminUrl = Get-AdminUrl -Connection $connection
    Invoke-Psql -PgBin $pg -Url $adminUrl -Sql "DROP DATABASE IF EXISTS $ScratchDatabase WITH (FORCE);" | Out-Null
    Invoke-Psql -PgBin $pg -Url $adminUrl -Sql "CREATE DATABASE $ScratchDatabase;" | Out-Null
}

function Restore-DumpInto([string]$PlainPath) {
    if ($dockerMode) {
        $containerPath = '/tmp/almahmoud-drill-' + $stamp + '.dump'
        Invoke-Compose (@('cp', $PlainPath, ($Service + ':' + $containerPath))) | Out-Null
        $restoreScript = 'pg_restore --clean --if-exists --no-owner --no-privileges --dbname=postgresql://' + $DbUser + ':$POSTGRES_PASSWORD@127.0.0.1:5432/' + $ScratchDatabase + ' ' + $containerPath
        Invoke-Compose (@('exec', '-T', $Service, 'sh', '-c', $restoreScript)) | Out-Null
        return $containerPath
    }
    $scratchUrl = Get-ScratchUrl -Connection $connection -Database $ScratchDatabase
    $restoreArgs = @('--dbname', $scratchUrl, '--clean', '--if-exists', '--no-owner', '--no-privileges', '--jobs', '2', $PlainPath)
    & (Join-Path $pg 'pg_restore.exe') @restoreArgs 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { throw "pg_restore failed with exit code $LASTEXITCODE" }
    return $null
}

function Remove-ScratchDatabase {
    if ($dockerMode) {
        Invoke-Compose (@('exec', '-T', $Service, 'psql', '-U', $DbUser, '-d', 'postgres', '-c', ('DROP DATABASE IF EXISTS ' + $ScratchDatabase + ' WITH (FORCE);'))) -AllowFailure | Out-Null
        return
    }
    $adminUrl = Get-AdminUrl -Connection $connection
    Invoke-Psql -PgBin $pg -Url $adminUrl -Sql "DROP DATABASE IF EXISTS $ScratchDatabase WITH (FORCE);" | Out-Null
}

function Get-ScratchUrl {
    param($Connection, [string]$Database)
    $password = [Uri]::EscapeDataString($Connection.Password)
    return "postgresql://$($Connection.User):$password@$($Connection.Host):$($Connection.Port)/$Database"
}

$trialBalanceSql = @'
WITH balances AS (
    SELECT tenant_id::text || '|' || company_id::text || '|' || account_id::text || '|' ||
           COALESCE(SUM(debit_minor), 0)::text || '|' || COALESCE(SUM(credit_minor), 0)::text AS part
    FROM journal_line
    GROUP BY tenant_id, company_id, account_id
)
SELECT COALESCE(md5(string_agg(part, chr(10) ORDER BY part)), 'empty')
FROM balances;
'@

$totalsSql = @'
SELECT COUNT(*)::text || '|' || COALESCE(SUM(debit_minor), 0)::text || '|' || COALESCE(SUM(credit_minor), 0)::text
FROM journal_line;
'@

$revisionSql = 'SELECT version_num FROM alembic_version;'

$report = [ordered]@{
    kind = 'almahmoud-restore-drill'
    version = 3
    started_at = (Get-Date).ToUniversalTime().ToString('o')
    status = 'red'
    mode = $(if ($dockerMode) { 'docker-compose' } else { 'host' })
    compose_file = $ComposeFile
    compose_project = $ComposeProject
    database = $(if ($dockerMode) { $Database } else { $connection.Database })
    scratch_database = $ScratchDatabase
}
$plainPath = $null
$containerDumpPath = $null

try {
    $latest = Get-ChildItem -Path $BackupDir -Filter 'almahmoud-*.dump.enc' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) { throw "no encrypted backup found in $BackupDir; run backup.ps1 first" }
    $report.backup_file = $latest.Name
    $report.backup_created_at = $latest.LastWriteTimeUtc.ToString('o')

    $manifestPath = Join-Path $BackupDir ($latest.Name -replace '\.dump\.enc$', '.manifest.json')
    $manifest = $null
    if (Test-Path -LiteralPath $manifestPath) {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        $report.backup_manifest = (Split-Path -Leaf $manifestPath)
    }

    $plainPath = Join-Path ([System.IO.Path]::GetTempPath()) ("almahmoud-drill-$stamp.dump")
    $restoredHash = Unprotect-File -EncryptedPath $latest.FullName -PlainPath $plainPath -KeyMaterial $keyMaterial
    $report.decrypted_sha256 = $restoredHash

    New-ScratchDatabase
    $containerDumpPath = Restore-DumpInto -PlainPath $plainPath

    if ($dockerMode) {
        $restoredDigest = Invoke-DatabaseSql -DatabaseName $ScratchDatabase -Sql $trialBalanceSql -Url $null
        $restoredTotals = Invoke-DatabaseSql -DatabaseName $ScratchDatabase -Sql $totalsSql -Url $null
        $restoredRevision = Invoke-DatabaseSql -DatabaseName $ScratchDatabase -Sql $revisionSql -Url $null
    }
    else {
        $scratchUrl = Get-ScratchUrl -Connection $connection -Database $ScratchDatabase
        $restoredDigest = Invoke-Psql -PgBin $pg -Url $scratchUrl -Sql $trialBalanceSql -TuplesOnly
        $restoredTotals = Invoke-Psql -PgBin $pg -Url $scratchUrl -Sql $totalsSql -TuplesOnly
        $restoredRevision = Invoke-Psql -PgBin $pg -Url $scratchUrl -Sql $revisionSql -TuplesOnly
    }

    # The dump is verified against the state recorded when it was taken, not
    # against today's live rows: once business activity resumes, a correct
    # restore would otherwise look like a mismatch.
    if ($null -ne $manifest -and $null -ne $manifest.expected_state) {
        $expectedDigest = [string]$manifest.expected_state.trial_balance_sha256
        $expectedTotals = [string]$manifest.expected_state.totals
        $expectedRevision = [string]$manifest.expected_state.migration
        $report.comparison_basis = 'backup-manifest'
        $report.manifest_consistency = [string]$manifest.expected_state.consistency
    }
    else {
        if ($dockerMode) {
            $expectedDigest = Invoke-DatabaseSql -DatabaseName $Database -Sql $trialBalanceSql -Url $null
            $expectedTotals = Invoke-DatabaseSql -DatabaseName $Database -Sql $totalsSql -Url $null
            $expectedRevision = Invoke-DatabaseSql -DatabaseName $Database -Sql $revisionSql -Url $null
        }
        else {
            $expectedDigest = Invoke-Psql -PgBin $pg -Url $connection.Uri -Sql $trialBalanceSql -TuplesOnly
            $expectedTotals = Invoke-Psql -PgBin $pg -Url $connection.Uri -Sql $totalsSql -TuplesOnly
            $expectedRevision = Invoke-Psql -PgBin $pg -Url $connection.Uri -Sql $revisionSql -TuplesOnly
        }
        $report.comparison_basis = 'live-source'
    }

    $report.expected_trial_balance_sha256 = $expectedDigest
    $report.restored_trial_balance_sha256 = $restoredDigest
    $report.expected_totals = $expectedTotals
    $report.restored_totals = $restoredTotals
    $report.expected_migration = $expectedRevision
    $report.restored_migration = $restoredRevision
    $report.trial_balance_matches = ($expectedDigest -eq $restoredDigest)
    $report.totals_match = ($expectedTotals -eq $restoredTotals)
    $report.migration_matches = ($expectedRevision -eq $restoredRevision)
    $report.duration_seconds = [Math]::Round(((Get-Date) - $startedAt).TotalSeconds, 1)

    if ($report.trial_balance_matches -and $report.totals_match -and $report.migration_matches) {
        $report.status = 'green'
    }
    else {
        throw 'restore mismatch: restored trial balance, totals or migration revision differs from the backup manifest'
    }
}
catch {
    $report.error = "$_"
    throw
}
finally {
    if (-not $KeepScratchDatabase) {
        try {
            Remove-ScratchDatabase
            $report.scratch_database_dropped = $true
        }
        catch {
            $report.scratch_database_dropped = $false
        }
    }
    if ($dockerMode -and $containerDumpPath) {
        try { Invoke-Compose (@('exec', '-T', $Service, 'rm', '-f', $containerDumpPath)) -AllowFailure | Out-Null } catch { }
    }
    if ($plainPath -and (Test-Path $plainPath)) { Remove-Item -Path $plainPath -Force }
    [array]::Clear($keyMaterial, 0, $keyMaterial.Length)
    $report.finished_at = (Get-Date).ToUniversalTime().ToString('o')
    $reportPath = Join-Path $ReportsDir "drill-$stamp.json"
    $report | ConvertTo-Json -Depth 5 | Set-Content -Path $reportPath -Encoding utf8
    Write-AccessLog -LogPath $logPath -Entry ([ordered]@{
        action = 'restore-drill'; status = $report.status; report = (Split-Path -Leaf $reportPath)
        backup_file = $report.backup_file; mode = $report.mode; operator = $env:USERNAME
    })
    Write-Host "restore drill: $($report.status) -> $reportPath"
}
