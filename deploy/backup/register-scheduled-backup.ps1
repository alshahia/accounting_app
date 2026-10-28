# Register the Windows Task Scheduler jobs that keep backups honest (T-33, AC-18/AC-19).
#
#   # deployment mode (recommended: covers the pilot stack, no database port)
#   pwsh -File deploy/backup/register-scheduled-backup.ps1 `
#       -ComposeFile deploy/docker-compose.prod.yml `
#       -ComposeEnvFile .local/pilot.env -ComposeProject almahmoud-erp-prod
#
#   # host/server mode (a database you can reach directly)
#   pwsh -File deploy/backup/register-scheduled-backup.ps1 -DatabaseUrl 'postgresql://almahmoud:...@127.0.0.1:55432/almahmoud'
#
# Creates two daily tasks under the current user: an encrypted backup at 02:00
# and a restore drill at 03:00. Both call run-scheduled.ps1, which starts Docker
# Desktop when the machine rebooted, and both write their result to the task
# result so a failure is visible in Task Scheduler. Secrets stay in the compose
# env file or the task argument list, never in the repository; the backup key
# stays in deploy/backup/keys.
param(
    [string]$DatabaseUrl = '',
    [string]$ComposeFile = '',
    [string]$ComposeEnvFile = '',
    [string]$ComposeProject = '',
    [string]$TaskPrefix = 'Al-Mahmoud',
    [string]$BackupTime = '02:00',
    [string]$DrillTime = '03:00'
)

$ErrorActionPreference = 'Stop'
if ($ComposeFile -eq '' -and [string]::IsNullOrWhiteSpace($DatabaseUrl)) {
    throw 'pass -ComposeFile (deployment mode) or -DatabaseUrl (host/server mode)'
}
if ($ComposeFile -ne '' -and -not (Test-Path -LiteralPath $ComposeFile)) {
    throw "compose file not found: $ComposeFile (run from the repository root or pass an absolute path)"
}

$pwsh = (Get-Command pwsh).Source
$wrapper = Join-Path $PSScriptRoot 'run-scheduled.ps1'
$dumps = Join-Path $PSScriptRoot 'dumps'
$key = Join-Path $PSScriptRoot 'keys\backup.key'
$reports = Join-Path $PSScriptRoot 'reports'

$modeArguments = ''
if ($ComposeFile -ne '') {
    $modeArguments = '-ComposeFile "{0}"' -f (Resolve-Path -LiteralPath $ComposeFile).Path
    if ($ComposeEnvFile -ne '') {
        $modeArguments += ' -ComposeEnvFile "{0}"' -f (Resolve-Path -LiteralPath $ComposeEnvFile).Path
    }
    if ($ComposeProject -ne '') { $modeArguments += ' -ComposeProject "{0}"' -f $ComposeProject }
}
else {
    $modeArguments = '-DatabaseUrl "{0}"' -f $DatabaseUrl
}

function Register-Al-MahmoudTask {
    param([string]$Name, [string]$TaskAction, [string]$Time, [string]$ExtraArguments, [string]$Description)
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Action {1} {2} -BackupDir "{3}" -KeyFile "{4}" {5}' -f $wrapper, $TaskAction, $modeArguments, $dumps, $key, $ExtraArguments
    $scheduledAction = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -Daily -At $Time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName $Name -Action $scheduledAction -Trigger $trigger -Settings $settings -Description $Description -Force | Out-Null
    Write-Host "registered task: $Name (daily at $Time, action $TaskAction)"
}

$backupExtra = ''
Register-Al-MahmoudTask -Name "$TaskPrefix Backup" -TaskAction 'backup' -Time $BackupTime -ExtraArguments $backupExtra -Description 'Encrypted Al-Mahmoud database backup (deploy/backup/backup.ps1 via run-scheduled.ps1)'
$drillExtra = '-ReportsDir "{0}"' -f $reports
Register-Al-MahmoudTask -Name "$TaskPrefix Restore Drill" -TaskAction 'drill' -Time $DrillTime -ExtraArguments $drillExtra -Description 'Nightly restore drill comparing the backup manifest (deploy/backup/restore-drill.ps1 via run-scheduled.ps1)'
