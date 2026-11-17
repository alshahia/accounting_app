# Register the Windows Task Scheduler job that watches the Al-Mahmoud pilot (T-35).
#
#   pwsh -File deploy/backup/register-health-watch.ps1                  # SYSTEM + at-startup, or refuse
#   pwsh -File deploy/backup/register-health-watch.ps1 -RequireSystem   # the same, said explicitly
#   pwsh -File deploy/backup/register-health-watch.ps1 -AllowInteractiveFallback
#
# Creates "Al-Mahmoud Health Watch": every -IntervalMinutes minutes and at
# startup, running deploy/backup/watch-health.ps1 hidden, as SYSTEM with no
# interactive login, so the check keeps running whether or not the operator is
# signed in. Both the SYSTEM principal and the at-startup trigger need an
# elevated shell; Task Scheduler answers "Access is denied" without one.
#
# SYSTEM is the default and the requirement: an unelevated run registers nothing
# and throws, unless -AllowInteractiveFallback is passed explicitly. With that
# switch the script registers the same watch for the invoking account (hidden,
# every -IntervalMinutes minutes and at that account's logon) so the pilot is
# not left unwatched. The fallback never overwrites a task that belongs to
# another principal - if the name already exists and is not already the invoking
# user's interactive task, it throws instead, so an unelevated rerun cannot
# silently downgrade a SYSTEM task. -RequireSystem and -AllowInteractiveFallback
# are mutually exclusive.
#
# The task description is built after the branch, so it states the schedule and
# principal that were actually registered. A red check exits 1 with or without a
# mailbox; with .local/alert.env configured it also sends one mail per 60
# minutes. The description claims neither more nor less (T-44).
#
# Conventions follow register-scheduled-backup.ps1: the task calls
# pwsh -NoProfile -ExecutionPolicy Bypass -File <script> and paths come from
# $PSScriptRoot.
param(
    [string]$TaskName = 'Al-Mahmoud Health Watch',
    [string]$BaseUrl = 'http://127.0.0.1:8090/api/v1/health',
    [string]$StatePath = (Join-Path $PSScriptRoot 'reports\health-watch-state.json'),
    [int]$IntervalMinutes = 15,
    [switch]$RequireSystem,
    [switch]$AllowInteractiveFallback
)

$ErrorActionPreference = 'Stop'

if ($RequireSystem -and $AllowInteractiveFallback) {
    throw '-RequireSystem and -AllowInteractiveFallback contradict each other: choose one'
}

$watch = Join-Path $PSScriptRoot 'watch-health.ps1'
if (-not (Test-Path -LiteralPath $watch)) { throw "watch script not found: $watch" }
if ($IntervalMinutes -lt 1) { throw 'IntervalMinutes must be at least 1' }

# A SYSTEM task cannot use the per-user app-execution alias in WindowsApps, so
# prefer a machine-wide pwsh.exe whenever one exists.
$pwshCandidates = @(
    (Join-Path $PSHOME 'pwsh.exe'),
    'C:\Program Files\PowerShell\7\pwsh.exe',
    (Get-Command pwsh -ErrorAction SilentlyContinue).Source
)
$userProfilePrefix = $env:USERPROFILE + '\'
$pwsh = $pwshCandidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
    Where-Object { -not $_.StartsWith($userProfilePrefix, [System.StringComparison]::OrdinalIgnoreCase) } |
    Select-Object -First 1
if (-not $pwsh) { $pwsh = (Get-Command pwsh).Source }
$userPwsh = (Get-Command pwsh).Source

function Test-SameAccount {
    param([string]$Left, [string]$Right)
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) { return $false }
    if ([string]::Equals($Left, $Right, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    $leftName = ($Left -split '\\')[-1]
    $rightName = ($Right -split '\\')[-1]
    return [string]::Equals($leftName, $rightName, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-TruthfulDescription {
    param([string]$Schedule, [string]$Principal)
    # Only facts that are true of the registered task: the interval, the trigger
    # and the principal. Exit semantics are stated exactly as implemented.
    return ("Watches {0} {1}; runs {2}; logs to deploy/backup/reports/health-watch.log. A red check exits 1 with or without a mailbox, so the task result is the alarm; with .local/alert.env configured it also sends one mail per 60 minutes." -f $BaseUrl, $Schedule, $Principal)
}

$arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -BaseUrl "{1}" -StatePath "{2}"' -f $watch, $BaseUrl, $StatePath
$settingsArgs = @{
    StartWhenAvailable         = $true
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    ExecutionTimeLimit         = (New-TimeSpan -Minutes 5)
    MultipleInstances          = 'IgnoreNew'
    Hidden                     = $true
}
$settings = New-ScheduledTaskSettingsSet @settingsArgs
# No -RepetitionDuration: repetition then continues indefinitely, which is what
# Task Scheduler accepts (a MaxValue duration is rejected by the task XML schema).
$once = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$registeredAs = 'SYSTEM'
$triggerSummary = "every $IntervalMinutes minutes and at system startup"
try {
    $description = Get-TruthfulDescription -Schedule $triggerSummary -Principal 'as SYSTEM with no interactive logon'
    $action = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments
    $triggers = @($once, (New-ScheduledTaskTrigger -AtStartup))
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description $description -Force | Out-Null
}
catch {
    $reason = $_.Exception.Message
    if ($RequireSystem) {
        throw "registering '$TaskName' as SYSTEM with an at-startup trigger failed: $reason (run this script from an elevated shell)"
    }
    if (-not $AllowInteractiveFallback) {
        throw ("registering '{0}' as SYSTEM with an at-startup trigger failed: {1}. SYSTEM is the required principal and nothing was registered. Re-run from an elevated shell, or pass -AllowInteractiveFallback to register the watch for the invoking account deliberately." -f $TaskName, $reason)
    }
    Write-Warning ("registering as SYSTEM with an at-startup trigger failed: {0}" -f $reason)
    Write-Warning 'both the SYSTEM principal and the at-startup trigger need elevation; -AllowInteractiveFallback was passed, so the task is being registered for the invoking account instead, and runs only while that account is signed in.'

    # Never let the fallback overwrite a task that is not already this user's own
    # interactive task: that is how a SYSTEM watch would be silently downgraded.
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        $existingUser = [string]$existing.Principal.UserId
        $existingLogon = [string]$existing.Principal.LogonType
        $isOwnInteractive = (Test-SameAccount -Left $existingUser -Right $currentUser) -and ($existingLogon -in @('Interactive', 'InteractiveToken'))
        if (-not $isOwnInteractive) {
            throw ("task '{0}' already exists with principal '{1}' (logon {2}); refusing to overwrite it with the interactive fallback. Run this script from an elevated shell with -RequireSystem to replace it deliberately, or remove the task first." -f $TaskName, $existingUser, $existingLogon)
        }
        Write-Warning ("re-registering the existing interactive task '{0}' for '{1}'" -f $TaskName, $existingUser)
    }

    $registeredAs = "$currentUser (interactive logon; not SYSTEM)"
    $triggerSummary = "every $IntervalMinutes minutes and at logon of $currentUser"
    $description = Get-TruthfulDescription -Schedule $triggerSummary -Principal "as $currentUser in that account's interactive session"
    $action = New-ScheduledTaskAction -Execute $userPwsh -Argument $arguments
    $triggers = @($once, (New-ScheduledTaskTrigger -AtLogOn -User $currentUser))
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description $description -Force | Out-Null
}

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ("registered task: {0}" -f $task.TaskName)
Write-Host ("  runs as:      {0} (logon {1}, runlevel {2})" -f $task.Principal.UserId, $task.Principal.LogonType, $task.Principal.RunLevel)
Write-Host ("  schedule:     {0}" -f $triggerSummary)
Write-Host ("  hidden:       {0}" -f $task.Settings.Hidden)
Write-Host ("  description:  {0}" -f $task.Description)
Write-Host ("  action:       {0} {1}" -f $task.Actions[0].Execute, $task.Actions[0].Arguments)
Write-Host ("  state:        {0}; last result {1}; next run {2}" -f $task.State, $info.LastTaskResult, $info.NextRunTime)
Write-Host ("  registered:   {0}" -f $registeredAs)
Write-Host 'run it now with: Start-ScheduledTask -TaskName "Al-Mahmoud Health Watch"'
