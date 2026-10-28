# Wrapper for the Windows scheduled tasks (T-33, AC-18/AC-19).
#
#   pwsh -File deploy/backup/run-scheduled.ps1 -Action backup -ComposeFile ... -ComposeEnvFile ... -ComposeProject ...
#
# The task fires after a reboot, so the wrapper first makes sure the Docker
# engine is answering (starting Docker Desktop when it is installed but idle),
# then delegates to backup.ps1 or restore-drill.ps1 and propagates the exit
# code, which is what Task Scheduler records as the task result.
param(
    [ValidateSet('backup', 'drill')]
    [string]$Action = 'backup',
    [string]$DatabaseUrl = '',
    [string]$ComposeFile = '',
    [string]$ComposeEnvFile = '',
    [string]$ComposeProject = '',
    [string]$BackupDir = (Join-Path $PSScriptRoot 'dumps'),
    [string]$KeyFile = (Join-Path $PSScriptRoot 'keys\backup.key'),
    [string]$ReportsDir = (Join-Path $PSScriptRoot 'reports')
)

$ErrorActionPreference = 'Stop'
$startedAt = Get-Date
Write-Host ('scheduled ' + $Action + ' started ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))

if ($ComposeFile -ne '') {
    & docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
        if (Test-Path -LiteralPath $desktop) {
            Write-Host 'Docker engine is not running; starting Docker Desktop'
            Start-Process -FilePath $desktop | Out-Null
        }
        $deadline = (Get-Date).AddMinutes(6)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 10
            & docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { break }
        }
        if ($LASTEXITCODE -ne 0) { throw 'Docker engine did not answer within 6 minutes; the scheduled job is aborted' }
    }
    Write-Host 'Docker engine is ready'
}

$scriptName = $(if ($Action -eq 'backup') { 'backup.ps1' } else { 'restore-drill.ps1' })
$arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot $scriptName))
if ($ComposeFile -ne '') {
    $arguments += @('-ComposeFile', $ComposeFile)
    if ($ComposeEnvFile -ne '') { $arguments += @('-ComposeEnvFile', $ComposeEnvFile) }
    if ($ComposeProject -ne '') { $arguments += @('-ComposeProject', $ComposeProject) }
}
else {
    $arguments += @('-DatabaseUrl', $DatabaseUrl)
}
$arguments += @('-BackupDir', $BackupDir, '-KeyFile', $KeyFile)
if ($Action -eq 'drill') { $arguments += @('-ReportsDir', $ReportsDir) }

$pwsh = (Get-Command pwsh).Source
& $pwsh @arguments
$code = $LASTEXITCODE
Write-Host ('scheduled ' + $Action + ' finished with exit code ' + $code + ' after ' + [math]::Round(((Get-Date) - $startedAt).TotalSeconds, 1) + ' s')
exit $code
