<#
.SYNOPSIS
  Encrypted logical backup of the Al-Mahmoud database, from the host or the deployment.
.DESCRIPTION
  Writes an AES-256-GCM encrypted custom-format dump plus a JSON manifest, appends
  one line to the access log, and prunes older dumps beyond the retention count.
  The manifest records the trial-balance digest, the journal totals and the
  migration revision captured when the dump was taken, so the restore drill can
  prove the dump restores to that exact recorded state without depending on the
  live database staying unchanged.

  Two modes:
    - host/server mode: connects with -DatabaseUrl (for example a local cluster);
    - deployment mode: runs pg_dump inside the compose "db" service, so the
      pilot stack can be backed up without publishing a database port.

  The key file is supplied by the operator and never leaves the host.
.EXAMPLE
  $env:MAHMOUD_BACKUP_DATABASE_URL = 'postgresql://almahmoud:...@127.0.0.1:55432/almahmoud'
  pwsh -File deploy/backup/backup.ps1
.EXAMPLE
  pwsh -File deploy/backup/backup.ps1 -ComposeFile deploy/docker-compose.prod.yml -ComposeEnvFile .local/pilot.env -ComposeProject almahmoud-erp-prod
#>
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
    [int]$RetainCount = 14,
    [string]$PgBin = $env:MAHMOUD_PG_BIN
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
New-Item -ItemType Directory -Force $BackupDir | Out-Null
$logPath = Join-Path $BackupDir 'backup-access.log'
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$baseName = "almahmoud-$stamp"
$plainPath = Join-Path $BackupDir "$baseName.dump"
$encryptedPath = Join-Path $BackupDir "$baseName.dump.enc"
$manifestPath = Join-Path $BackupDir "$baseName.manifest.json"
$startedAt = Get-Date

function Get-ComposeArgumentList {
    $list = New-Object System.Collections.Generic.List[string]
    $list.Add('compose')
    if ($ComposeProject -ne '') { $list.Add('-p'); $list.Add($ComposeProject) }
    if ($ComposeEnvFile -ne '') { $list.Add('--env-file'); $list.Add($ComposeEnvFile) }
    $list.Add('-f'); $list.Add($ComposeFile)
    return [string[]]$list.ToArray()
}

function Get-ComposeArgumentString {
    $text = 'compose'
    if ($ComposeProject -ne '') { $text += ' -p ' + $ComposeProject }
    if ($ComposeEnvFile -ne '') { $text += ' --env-file "' + $ComposeEnvFile + '"' }
    $text += ' -f "' + $ComposeFile + '"'
    return $text
}

function Invoke-Compose([string[]]$Arguments) {
    $all = @(Get-ComposeArgumentList) + $Arguments
    $output = & docker @all 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ('docker ' + ($all -join ' ') + ' failed (' + $LASTEXITCODE + '): ' + ($output -join [Environment]::NewLine))
    }
    return (($output | ForEach-Object { "$_" }) -join [Environment]::NewLine).Trim()
}

function Invoke-SourceSql([string]$Sql) {
    if ($dockerMode) {
        return (Invoke-Compose (@('exec', '-T', $Service, 'psql', '-U', $DbUser, '-d', $Database, '-t', '-A', '-c', $Sql))).Trim()
    }
    return (Invoke-Psql -PgBin $pg -Url $connection.Uri -Sql $Sql -TuplesOnly).Trim()
}

function Invoke-SourceDump([string]$Path) {
    if ($dockerMode) {
        $argumentString = (Get-ComposeArgumentString) + ' exec -T ' + $Service + ' pg_dump --format=custom --no-owner --no-privileges -U ' + $DbUser + ' -d ' + $Database
        $errorPath = [System.IO.Path]::GetTempFileName()
        $process = Start-Process -FilePath 'docker' -ArgumentList $argumentString -RedirectStandardOutput $Path -RedirectStandardError $errorPath -NoNewWindow -Wait -PassThru
        $errorText = ''
        if (Test-Path -LiteralPath $errorPath) { $errorText = (Get-Content -LiteralPath $errorPath -Raw) }
        Remove-Item -LiteralPath $errorPath -Force -ErrorAction SilentlyContinue
        if ($process.ExitCode -ne 0) {
            throw ('docker compose exec pg_dump failed (' + $process.ExitCode + '): ' + $errorText)
        }
        return
    }
    $dumpArgs = @('--format=custom', '--no-owner', '--no-privileges', '--file', $Path, '--dbname', $connection.Uri)
    & (Join-Path $pg 'pg_dump.exe') @dumpArgs
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE" }
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

try {
    $beforeTrialBalance = Invoke-SourceSql $trialBalanceSql
    $beforeTotals = Invoke-SourceSql $totalsSql
    $beforeRevision = Invoke-SourceSql $revisionSql

    Invoke-SourceDump -Path $plainPath

    $afterTrialBalance = Invoke-SourceSql $trialBalanceSql
    $afterTotals = Invoke-SourceSql $totalsSql
    $afterRevision = Invoke-SourceSql $revisionSql
    $stable = ($beforeTrialBalance -eq $afterTrialBalance) -and ($beforeTotals -eq $afterTotals) -and ($beforeRevision -eq $afterRevision)

    $plainHash = (Get-FileHash -Path $plainPath -Algorithm SHA256).Hash
    $encryptedHash = Protect-File -PlainPath $plainPath -EncryptedPath $encryptedPath -KeyMaterial $keyMaterial
    $plainBytes = (Get-Item $plainPath).Length
    $encryptedBytes = (Get-Item $encryptedPath).Length
    $durationSeconds = [Math]::Round(((Get-Date) - $startedAt).TotalSeconds, 1)

    $manifest = [ordered]@{
        kind = 'almahmoud-postgres-logical-backup'
        version = 2
        created_at = (Get-Date).ToUniversalTime().ToString('o')
        mode = $(if ($dockerMode) { 'docker-compose' } else { 'host' })
        compose_file = $ComposeFile
        compose_project = $ComposeProject
        service = $(if ($dockerMode) { $Service } else { $null })
        database = $(if ($dockerMode) { $Database } else { $connection.Database })
        server = $(if ($dockerMode) { 'docker compose service ' + $Service } else { "$($connection.Host):$($connection.Port)" })
        file = (Split-Path -Leaf $encryptedPath)
        encryption = 'AES-256-GCM, PBKDF2-SHA256 (200000 iterations), 16-byte salt, 12-byte nonce'
        plaintext_sha256 = $plainHash
        encrypted_sha256 = $encryptedHash
        plaintext_bytes = $plainBytes
        encrypted_bytes = $encryptedBytes
        dump_format = 'pg_dump custom'
        duration_seconds = $durationSeconds
        expected_state = [ordered]@{
            trial_balance_sha256 = $beforeTrialBalance
            totals = $beforeTotals
            migration = $beforeRevision
            consistency = $(if ($stable) { 'stable-during-backup' } else { 'changed-during-backup' })
            captured_at = (Get-Date).ToUniversalTime().ToString('o')
        }
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -Path $manifestPath -Encoding utf8

    Write-AccessLog -LogPath $logPath -Entry ([ordered]@{
        action = 'backup'; status = 'ok'; file = (Split-Path -Leaf $encryptedPath); mode = $manifest.mode
        plaintext_sha256 = $plainHash; bytes = $encryptedBytes; duration_seconds = $durationSeconds
        database = $manifest.database; operator = $env:USERNAME
    })

    $existing = Get-ChildItem -Path $BackupDir -Filter 'almahmoud-*.dump.enc' |
        Sort-Object LastWriteTime -Descending
    $expired = @($existing | Select-Object -Skip $RetainCount)
    foreach ($file in $expired) {
        Remove-Item -Path $file.FullName -Force
        $oldManifest = Join-Path $BackupDir ($file.Name -replace '\.dump\.enc$', '.manifest.json')
        if (Test-Path -LiteralPath $oldManifest) { Remove-Item -LiteralPath $oldManifest -Force }
        Write-AccessLog -LogPath $logPath -Entry ([ordered]@{
            action = 'retention-delete'; status = 'ok'; file = $file.Name
            reason = "retain newest $RetainCount dumps"; operator = $env:USERNAME
        })
    }

    Write-Host "backup ok: $encryptedPath ($encryptedBytes bytes, sha256 $encryptedHash)"
    Write-Host "manifest:  $manifestPath (mode $($manifest.mode), consistency $($manifest.expected_state.consistency))"
}
catch {
    Write-AccessLog -LogPath $logPath -Entry ([ordered]@{
        action = 'backup'; status = 'failed'; error = "$_"; operator = $env:USERNAME
    })
    throw
}
finally {
    if (Test-Path $plainPath) { Remove-Item -Path $plainPath -Force }
    [array]::Clear($keyMaterial, 0, $keyMaterial.Length)
}
