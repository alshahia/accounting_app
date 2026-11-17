# Al-Mahmoud pilot health watch (T-35).
#
#   pwsh -File deploy/backup/watch-health.ps1
#   pwsh -File deploy/backup/watch-health.ps1 -BaseUrl https://127.0.0.1:8443/api/v1/health
#
# One HTTP check of the pilot stack. Every run appends one line to
# reports/health-watch.log (renamed to health-watch.log.1 once it passes 1 MB)
# and rewrites reports/health-watch-state.json, so the last result, the last
# alert time and the consecutive-failure count survive on disk. The registered
# task is "Al-Mahmoud Health Watch" (register-health-watch.ps1).
#
# Red means the health endpoint did not answer 200. Since T-38 the pilot gateway
# forwards the health and readiness paths on plain HTTP, so the default probe
# (http://127.0.0.1:8090/api/v1/health) answers 200 directly and nothing is
# followed. Redirect following is legacy: it is kept for -HttpsPort and for a
# reintroduced redirect, and a 301 alone would prove only that nginx answered,
# not that the API is healthy. The rewrite applies only while the redirect stays
# on the configured pilot host (-BaseUrl); an external redirect is never pulled
# onto this port.
#
# Mail is config-gated and optional: four ALERT_* keys in .local/alert.env
# (git-ignored) are mandatory - ALERT_MAIL_TO, ALERT_MAIL_FROM, ALERT_SMTP_HOST,
# ALERT_SMTP_PORT - plus ALERT_SMTP_USER / ALERT_SMTP_PASSWORD only when the
# provider authenticates. That pair is optional but must be complete: a user
# without a password, or a password without a user, keeps the gate closed. The
# port must be a number in 1-65535; anything else also keeps the gate closed.
# The exit code does not depend on the mailbox: a red run exits 1 in every case
# - gate closed, throttled, sent, send-failed - so Task Scheduler records a
# failed result and the task history is the alarm (T-44). With the gate closed
# the run appends a WARNING naming the exact reason and logs alert=not-configured;
# a configured mailbox adds off-host delivery, one mail per -ThrottleMinutes
# through the state file. No secret value is ever printed, logged or mailed.
#
# Mail transport: 587 with STARTTLS (-UseSsl) is the supported and only tested
# path. Port 465 is usually implicit TLS, which Send-MailMessage/.NET SmtpClient
# does not speak, so 465 is unsupported until it is tested against a real
# provider.
param(
    [string]$BaseUrl = 'http://127.0.0.1:8090/api/v1/health',
    [string]$StatePath = (Join-Path $PSScriptRoot 'reports\health-watch-state.json'),
    [string]$LogPath = '',
    [string]$AlertEnvPath = (Join-Path $PSScriptRoot '..\..\.local\alert.env'),
    [int]$TimeoutSec = 10,
    [int]$ThrottleMinutes = 60,
    [int]$HttpsPort = 8443,
    [int]$MaxRedirects = 3,
    [switch]$NoMail
)

$ErrorActionPreference = 'Stop'
if ($LogPath -eq '') {
    $stateDirectory = Split-Path -Parent $StatePath
    if (-not $stateDirectory) { $stateDirectory = '.' }
    $LogPath = Join-Path $stateDirectory 'health-watch.log'
}
# The mail body tells the operator where to run the recovery command; derive it
# from this script's location instead of assuming a checkout path.
$repoRoot = $PSScriptRoot
try { $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path } catch { }

function Invoke-HealthProbe {
    param([string]$Uri)
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    # The pilot certificate is self-signed and bound to the host name, not to
    # 127.0.0.1; the default probe is loopback-only, so the chain is accepted
    # here. An operator-supplied remote -BaseUrl is therefore unauthenticated.
    $handler.ServerCertificateCustomValidationCallback = [System.Net.Http.HttpClientHandler]::DangerousAcceptAnyServerCertificateValidator
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
    try {
        $response = $client.GetAsync($Uri).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return [pscustomobject]@{
            Code     = [int]$response.StatusCode
            Location = [string]$response.Headers.Location
            Body     = $body
        }
    }
    finally { $client.Dispose() }
}

function Write-WatchLog {
    param([string]$Line)
    $directory = Split-Path -Parent $LogPath
    if ($directory -and -not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }
    # One line per run is tiny, but the file was unbounded; keep one previous
    # generation and cap the live file at 1 MB.
    if ((Test-Path -LiteralPath $LogPath) -and (Get-Item -LiteralPath $LogPath).Length -gt 1MB) {
        Move-Item -LiteralPath $LogPath -Destination ($LogPath + '.1') -Force
    }
    Add-Content -LiteralPath $LogPath -Value $Line -Encoding utf8
}

function Read-AlertConfig {
    $config = @{
        MAIL_TO  = ''
        MAIL_FROM = ''
        SMTP_HOST = ''
        SMTP_PORT = ''
        SMTP_USER = ''
        SMTP_PASSWORD = ''
    }
    if ($NoMail -or -not (Test-Path -LiteralPath $AlertEnvPath)) { return $config }
    foreach ($line in [System.IO.File]::ReadAllLines($AlertEnvPath)) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $index = $trimmed.IndexOf('=')
        if ($index -lt 1) { continue }
        $key = $trimmed.Substring(0, $index).Trim()
        $value = $trimmed.Substring($index + 1).Trim()
        if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        switch ($key) {
            'ALERT_MAIL_TO' { $config.MAIL_TO = $value }
            'ALERT_MAIL_FROM' { $config.MAIL_FROM = $value }
            'ALERT_SMTP_HOST' { $config.SMTP_HOST = $value }
            'ALERT_SMTP_PORT' { $config.SMTP_PORT = $value }
            'ALERT_SMTP_USER' { $config.SMTP_USER = $value }
            'ALERT_SMTP_PASSWORD' { $config.SMTP_PASSWORD = $value }
        }
    }
    return $config
}

function Get-AlertGate {
    # Fails closed. Returns the port to mail on and the exact reason the gate is
    # closed, so a red run can log why no mail was sent instead of crashing.
    param($Config)
    $mandatory = [ordered]@{
        MAIL_TO   = 'ALERT_MAIL_TO'
        MAIL_FROM = 'ALERT_MAIL_FROM'
        SMTP_HOST = 'ALERT_SMTP_HOST'
        SMTP_PORT = 'ALERT_SMTP_PORT'
    }
    $problems = New-Object System.Collections.Generic.List[string]
    $missing = @()
    foreach ($key in $mandatory.Keys) {
        if ([string]::IsNullOrWhiteSpace($Config[$key])) { $missing += $mandatory[$key] }
    }
    if ($missing.Count -gt 0) { $problems.Add('missing ' + ($missing -join ', ')) }

    $hasUser = -not [string]::IsNullOrWhiteSpace($Config.SMTP_USER)
    $hasPassword = -not [string]::IsNullOrWhiteSpace($Config.SMTP_PASSWORD)
    if ($hasPassword -and -not $hasUser) { $problems.Add('ALERT_SMTP_PASSWORD is set without ALERT_SMTP_USER') }
    if ($hasUser -and -not $hasPassword) { $problems.Add('ALERT_SMTP_USER is set without ALERT_SMTP_PASSWORD') }

    $port = 0
    if (-not [string]::IsNullOrWhiteSpace($Config.SMTP_PORT)) {
        $parsedPort = 0
        if (-not [int]::TryParse($Config.SMTP_PORT, [ref]$parsedPort)) {
            $problems.Add(("ALERT_SMTP_PORT '{0}' is not a number" -f $Config.SMTP_PORT))
        }
        elseif ($parsedPort -lt 1 -or $parsedPort -gt 65535) {
            $problems.Add(("ALERT_SMTP_PORT {0} is outside 1-65535" -f $parsedPort))
        }
        else { $port = $parsedPort }
    }

    return [pscustomobject]@{
        Configured = ($problems.Count -eq 0)
        Port       = $port
        Reason     = ($problems -join '; ')
    }
}

function ConvertTo-Instant {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [datetimeoffset]) { return $Value }
    if ($Value -is [datetime]) { return [datetimeoffset]::new($Value) }
    return [datetimeoffset]::Parse([string]$Value, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::RoundtripKind)
}

$state = [ordered]@{
    url                 = $BaseUrl
    lastCheckLocal      = $null
    lastCheckUtc        = $null
    lastStatus          = $null
    lastHttpStatus      = $null
    lastDetail          = $null
    consecutiveFailures = 0
    lastAlertUtc        = $null
    lastAlertResult     = $null
    alertConfigProblem  = $null
}
if (Test-Path -LiteralPath $StatePath) {
    try {
        $loaded = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        foreach ($property in $loaded.PSObject.Properties) {
            if ($state.Contains($property.Name)) { $state[$property.Name] = $property.Value }
        }
    }
    catch {
        # A corrupt state file must not stop the watch; start a fresh one.
    }
}
# -BaseUrl wins over the previous run's URL: the state must describe this probe.
$state['url'] = $BaseUrl

$status = 'red'
$httpStatus = $null
$probeUrl = $BaseUrl
$detail = ''
try {
    $uri = [Uri]$BaseUrl
    $pilotHost = $uri.Host
    for ($hop = 0; $hop -le $MaxRedirects; $hop++) {
        $probeUrl = $uri.AbsoluteUri
        $probe = Invoke-HealthProbe -Uri $probeUrl
        $httpStatus = $probe.Code
        if ($httpStatus -ge 300 -and $httpStatus -lt 400 -and $probe.Location -ne '') {
            $next = [Uri]::new($uri, $probe.Location)
            # The HTTP listener drops its own port when it redirects to TLS: put the
            # stack's HTTPS port back so the next hop reaches this pilot, not :443 -
            # but only on the pilot host, never on an external redirect target.
            $sameHost = [string]::Equals($next.Host, $pilotHost, [System.StringComparison]::OrdinalIgnoreCase)
            if ($next.Scheme -eq 'https' -and $next.IsDefaultPort -and $HttpsPort -gt 0 -and $sameHost) {
                $builder = [UriBuilder]::new($next)
                $builder.Port = $HttpsPort
                $next = $builder.Uri
            }
            $uri = $next
            continue
        }
        if ($httpStatus -eq 200) {
            $status = 'green'
            try {
                $parsed = $probe.Body | ConvertFrom-Json
                if ($parsed.status -and $parsed.status -ne 'ok') {
                    $status = 'red'
                    $detail = "health body status=$($parsed.status)"
                }
                else { $detail = 'health ok' }
            }
            catch { $detail = 'health ok' }
        }
        else { $detail = "http $httpStatus from $probeUrl" }
        break
    }
    if ($status -eq 'red' -and $detail -eq '') { $detail = "no 200 within $MaxRedirects redirects" }
}
catch {
    $status = 'red'
    $detail = $_.Exception.GetBaseException().Message
}

# Timestamps are taken after the probe, so a slow check reports when the result
# was known rather than when it started.
$finishedAt = Get-Date
$httpDisplay = 'none'
if ($null -ne $httpStatus) { $httpDisplay = [string]$httpStatus }
$nowLocal = $finishedAt.ToString('o')
$nowUtc = $finishedAt.ToUniversalTime().ToString('o')
$state['lastCheckLocal'] = $nowLocal
$state['lastCheckUtc'] = $nowUtc
$state['lastStatus'] = $status
$state['lastHttpStatus'] = $httpStatus
$state['lastDetail'] = $detail
if ($status -eq 'green') { $state['consecutiveFailures'] = 0 }
else { $state['consecutiveFailures'] = [int]$state['consecutiveFailures'] + 1 }

$exitCode = 0
if ($status -ne 'green') {
    # A red check is a failed run whether or not a mailbox exists (T-44): mail is
    # the off-host extra, never the thing that decides the task result.
    $exitCode = 1
    $alertConfig = Read-AlertConfig
    $gate = Get-AlertGate -Config $alertConfig
    if (-not $gate.Configured) {
        $alertResult = 'not-configured'
        $state['lastAlertResult'] = 'not-configured'
        $state['alertConfigProblem'] = $gate.Reason
        Write-Host ("WARNING: alert delivery is not configured: {0}. This red health check exits 1 and is logged locally; set the keys in .local/alert.env to also receive mail." -f $gate.Reason)
        Write-WatchLog ("{0} check={1} url={2} http={3} detail={4} failures={5} alert=not-configured reason={6}" -f $nowLocal, $status, $probeUrl, $httpDisplay, $detail, $state['consecutiveFailures'], $gate.Reason)
    }
    else {
        $state['alertConfigProblem'] = $null
        $lastAlert = ConvertTo-Instant -Value $state['lastAlertUtc']
        $nowOffset = [datetimeoffset]::new($finishedAt)
        if ($lastAlert -and ($nowOffset - $lastAlert).TotalMinutes -lt $ThrottleMinutes) {
            $alertResult = 'throttled'
            Write-WatchLog ("{0} check={1} url={2} http={3} detail={4} failures={5} alert=throttled last={6}" -f $nowLocal, $status, $probeUrl, $httpDisplay, $detail, $state['consecutiveFailures'], $lastAlert.ToString('o'))
        }
        else {
            $subject = "ALERT: Al-Mahmoud ERP pilot is DOWN ($detail)"
            $body = @(
                'The Al-Mahmoud ERP pilot health check is red.',
                '',
                "Checked at (local): $nowLocal",
                "Checked at (UTC):   $nowUtc",
                "Base URL:           $BaseUrl",
                "Last probe URL:     $probeUrl",
                "HTTP status:        $httpDisplay",
                "Detail:             $detail",
                "Consecutive fails:  $($state['consecutiveFailures'])",
                '',
                ("Run, from {0}:" -f $repoRoot),
                '  docker compose --project-name almahmoud-erp-prod --env-file .local/pilot.env -f deploy/docker-compose.prod.yml ps',
                '  docker compose --project-name almahmoud-erp-prod --env-file .local/pilot.env -f deploy/docker-compose.prod.yml up -d --wait --wait-timeout 300',
                '',
                'Runbook: docs/10-runbooks/backup-restore.md (step 4, health watch).',
                "Watch log: $LogPath"
            ) -join [Environment]::NewLine
            $mailArgs = @{
                To         = $alertConfig.MAIL_TO
                From       = $alertConfig.MAIL_FROM
                Subject    = $subject
                Body       = $body
                SmtpServer = $alertConfig.SMTP_HOST
                Port       = $gate.Port
                Encoding   = [System.Text.Encoding]::UTF8
                ErrorAction = 'Stop'
            }
            # 587/STARTTLS is the supported path; 465 is likely implicit TLS and
            # untested, so it is left to fail as send-failed rather than guessed.
            if ($gate.Port -in @(465, 587)) { $mailArgs['UseSsl'] = $true }
            if (-not [string]::IsNullOrWhiteSpace($alertConfig.SMTP_USER)) {
                $secure = ConvertTo-SecureString -String $alertConfig.SMTP_PASSWORD -AsPlainText -Force
                $mailArgs['Credential'] = [System.Management.Automation.PSCredential]::new($alertConfig.SMTP_USER, $secure)
            }
            try {
                Send-MailMessage @mailArgs
                $alertResult = 'sent'
                $state['lastAlertUtc'] = $nowUtc
                $state['lastAlertResult'] = 'sent'
                Write-WatchLog ("{0} check={1} url={2} http={3} detail={4} failures={5} alert=sent to={6}" -f $nowLocal, $status, $probeUrl, $httpDisplay, $detail, $state['consecutiveFailures'], $alertConfig.MAIL_TO)
            }
            catch {
                $alertResult = 'send-failed'
                $state['lastAlertUtc'] = $nowUtc
                $state['lastAlertResult'] = 'send-failed'
                Write-WatchLog ("{0} check={1} url={2} http={3} detail={4} failures={5} alert=send-failed error={6}" -f $nowLocal, $status, $probeUrl, $httpDisplay, $detail, $state['consecutiveFailures'], $_.Exception.Message)
            }
        }
    }
}
else {
    Write-WatchLog ("{0} check=green url={1} http={2} detail={3}" -f $nowLocal, $probeUrl, $httpStatus, $detail)
    Write-Host ("health watch: green ({0} -> {1})" -f $BaseUrl, $httpStatus)
}

$stateDirectory = Split-Path -Parent $StatePath
if ($stateDirectory -and -not (Test-Path -LiteralPath $stateDirectory)) {
    New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
}
$state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StatePath -Encoding utf8

if ($status -ne 'green') {
    Write-Host ("health watch: RED - {0} (http {1}); alert={2}" -f $detail, $httpDisplay, $alertResult)
}
exit $exitCode
