<#
.SYNOPSIS
  Install and control the Al-Mahmoud ERP production stack on Windows.
.DESCRIPTION
  One entry point for the pilot installation: checks the machine, generates the
  secrets and the TLS certificate, loads the bundled images, starts the
  containers, waits for health and prints exactly what to open next. Every
  failure is printed as an actionable message, and the whole run is written to
  a log file so the transcript can be attached to the acceptance record.

  Use install.bat (double-click) or run this file directly:
      pwsh -File install.ps1 -Action check
      pwsh -File install.ps1 -Action install
      pwsh -File install.ps1 -Action status
      pwsh -File install.ps1 -Action logs
      pwsh -File install.ps1 -Action stop
.PARAMETER Action
  install (default), start, stop, status, check or logs.
.PARAMETER InstallPath
  Where the runtime files, secrets and backups live. Default C:\AlMahmoud.
.PARAMETER ProjectName
  Docker Compose project name. Empty uses the name in the compose file.
.PARAMETER NoBrowser
  Do not open the browser after a successful install.
.EXAMPLE
  pwsh -File install.ps1 -Action install -HttpsPort 8443
#>
[CmdletBinding()]
param(
    [ValidateSet('install', 'start', 'stop', 'status', 'check', 'logs')]
    [string] $Action = 'install',
    [string] $InstallPath = 'C:\AlMahmoud',
    [string] $ProjectName = '',
    [int] $HttpPort = 8080,
    [int] $HttpsPort = 8443,
    [switch] $NoBrowser,
    [switch] $Elevated
)

$ErrorActionPreference = 'Stop'

$script:StartedAt = Get-Date
$script:DeploySource = Split-Path -Parent $PSScriptRoot
$script:BundleRoot = Split-Path -Parent $script:DeploySource
$script:InstallDeploy = Join-Path $InstallPath 'deploy'
$script:ComposeFile = Join-Path $script:InstallDeploy 'docker-compose.prod.yml'
$script:EnvFile = Join-Path $InstallPath '.env'
$script:CredentialFile = Join-Path $InstallPath 'credentials.txt'
$script:LogFile = $null

function Write-Step([string] $Text) {
    Write-Host ''
    Write-Host ('[ ' + $Text + ' ]') -ForegroundColor Cyan
}
function Write-Ok([string] $Text) { Write-Host ('   OK   ' + $Text) -ForegroundColor Green }
function Write-Info([string] $Text) { Write-Host ('        ' + $Text) -ForegroundColor Gray }
function Write-Warn([string] $Text) { Write-Host ('   WARN ' + $Text) -ForegroundColor Yellow }

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Restart-Elevated {
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"' + $PSCommandPath + '"'),
        '-Action', $Action,
        '-InstallPath', ('"' + $InstallPath + '"'),
        '-HttpPort', [string]$HttpPort,
        '-HttpsPort', [string]$HttpsPort
    )
    if ($ProjectName -ne '') { $arguments += @('-ProjectName', $ProjectName) }
    if ($NoBrowser) { $arguments += '-NoBrowser' }
    $arguments += '-Elevated'
    $processPath = (Get-Process -Id $PID).Path
    Write-Host 'Administrator rights are required to write the installation files and open the ports.' -ForegroundColor Yellow
    Write-Host 'Windows will ask for permission in the next window.' -ForegroundColor Yellow
    Start-Process -FilePath $processPath -Verb RunAs -ArgumentList $arguments
    exit 0
}

function Invoke-Docker([string[]] $Arguments) {
    & docker @Arguments
    return $LASTEXITCODE
}

function Invoke-Compose([string[]] $Arguments, [switch] $AllowFailure) {
    $composeArguments = @('compose')
    if ($ProjectName -ne '') { $composeArguments += @('-p', $ProjectName) }
    $composeArguments += @('--env-file', $script:EnvFile, '-f', $script:ComposeFile)
    $composeArguments += $Arguments
    & docker @composeArguments
    $code = $LASTEXITCODE
    if ($code -ne 0 -and -not $AllowFailure) {
        throw ('docker compose ' + ($Arguments -join ' ') + ' failed with exit code ' + $code + '. Read the lines above; the log has the full output.')
    }
    return $code
}

function Test-Image([string] $Image) {
    & docker image inspect $Image 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Get-ImageId([string] $Image) {
    $id = & docker image inspect --format '{{.Id}}' $Image 2>$null
    if ($LASTEXITCODE -ne 0) { return 'missing' }
    return $id.Trim()
}

function Test-PortFree([int] $Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($null -eq $listener) { return $null }
    $owner = Get-Process -Id $listener[0].OwningProcess -ErrorAction SilentlyContinue
    if ($null -eq $owner) { return ('another process (pid ' + $listener[0].OwningProcess + ')') }
    return ($owner.ProcessName + ' (pid ' + $owner.Id + ')')
}

function Get-EnvValue([string] $Name) {
    if (-not (Test-Path -LiteralPath $script:EnvFile)) { return $null }
    foreach ($line in Get-Content -LiteralPath $script:EnvFile) {
        if ($line.StartsWith($Name + '=')) { return $line.Substring($Name.Length + 1) }
    }
    return $null
}

function New-RandomSecret([int] $Bytes) {
    $buffer = New-Object byte[] $Bytes
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    return [Convert]::ToBase64String($buffer)
}

function Invoke-WebProbe([string] $Url) {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($null -eq $curl) { return 'no-curl' }
    $code = & curl.exe -k -s -o NUL -w '%{http_code}' --max-time 10 $Url
    return $code
}

function Wait-Healthy([int] $TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $attempt = 0
    $lastCode = 'none'
    while ((Get-Date) -lt $deadline) {
        $attempt++
        $lastCode = Invoke-WebProbe ('https://127.0.0.1:' + $HttpsPort + '/api/v1/health')
        if ($lastCode -eq '200') { return $true }
        if ($attempt % 4 -eq 0) { Write-Info ('still starting, health probe returned ' + $lastCode + ' ...') }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Assert-Preflight {
    $checks = New-Object System.Collections.Generic.List[object]
    function Add-Check([string] $Name, [bool] $Passed, [string] $Detail) {
        $checks.Add([pscustomobject]@{ Check = $Name; Passed = $Passed; Detail = $Detail })
    }

    Add-Check 'PowerShell' ($PSVersionTable.PSVersion.Major -ge 5) ('PowerShell ' + $PSVersionTable.PSVersion)
    Add-Check 'Windows version' ([Environment]::OSVersion.Version.Build -ge 19044) ('build ' + [Environment]::OSVersion.Version.Build + ', needs Windows 10 21H2 or newer')

    $docker = Get-Command docker -ErrorAction SilentlyContinue
    Add-Check 'Docker CLI' ($null -ne $docker) ($(if ($null -ne $docker) { (docker --version) } else { 'not on PATH' }))

    $daemon = 'not running'
    if ($null -ne $docker) {
        $version = & docker info --format '{{.ServerVersion}}' 2>$null
        if ($LASTEXITCODE -ne 0) {
            $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
            if (Test-Path -LiteralPath $desktop) {
                Write-Info 'Docker Desktop is installed but not running; starting it...'
                Start-Process -FilePath $desktop | Out-Null
                $deadline = (Get-Date).AddSeconds(150)
                while ((Get-Date) -lt $deadline) {
                    Start-Sleep -Seconds 5
                    $version = & docker info --format '{{.ServerVersion}}' 2>$null
                    if ($LASTEXITCODE -eq 0) { break }
                }
            }
        }
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($version)) {
            $daemon = $version.Trim()
        }
    }
    Add-Check 'Docker engine' ($daemon -ne 'not running') ('server ' + $daemon)

    $composeVersion = 'not available'
    if ($null -ne $docker) {
        $composeVersion = (& docker compose version 2>$null | Select-Object -First 1)
    }
    Add-Check 'Docker Compose v2' ($composeVersion -match 'v?2\.') ($composeVersion)

    Add-Check 'curl.exe' ($null -ne (Get-Command curl.exe -ErrorAction SilentlyContinue)) 'used for the health probes'

    foreach ($pair in @(@('HTTP port', $HttpPort), @('HTTPS port', $HttpsPort))) {
        $owner = Test-PortFree ([int]$pair[1])
        $detail = 'port ' + $pair[1] + ' is free'
        if ($null -ne $owner) { $detail = 'port ' + $pair[1] + ' is used by ' + $owner }
        Add-Check ([string]$pair[0]) ($null -eq $owner) $detail
    }

    $freeGb = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
    Add-Check 'Disk space' ($freeGb -ge 3) ($freeGb.ToString() + ' GB free on C:')

    foreach ($file in @('docker-compose.prod.yml', 'nginx.conf', 'generate-certs.ps1')) {
        $path = Join-Path $script:DeploySource $file
        Add-Check ('bundle: ' + $file) (Test-Path -LiteralPath $path) $path
    }
    $imagePresent = $null -ne $docker -and (Test-Image 'almahmoud-erp-api:prod')
    $tar = Join-Path $script:BundleRoot 'images.tar'
    Add-Check 'API image' ($imagePresent -or (Test-Path -LiteralPath $tar)) $(if ($imagePresent) { 'almahmoud-erp-api:prod present' } else { 'images.tar at ' + $tar })

    Write-Step 'Machine check'
    $checks | Format-Table -AutoSize | Out-String | Write-Host
    $failed = @($checks | Where-Object { -not $_.Passed })
    if ($failed.Count -gt 0) {
        $names = ($failed | ForEach-Object { $_.Check }) -join ', '
        throw ('The machine is not ready (' + $names + '). Fix the failed rows above and run the check again.')
    }
    Write-Ok 'all prerequisites are present'
}

function Copy-DeployAssets {
    $sourcePath = (Resolve-Path -LiteralPath $script:DeploySource).Path.TrimEnd([char]92)
    $targetPath = [System.IO.Path]::GetFullPath($script:InstallDeploy).TrimEnd([char]92)
    if ($sourcePath -eq $targetPath) {
        Write-Info 'running from the installed copy, no files to copy'
        return
    }
    New-Item -ItemType Directory -Force -Path $script:InstallDeploy | Out-Null
    Get-ChildItem -LiteralPath $script:DeploySource -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $script:InstallDeploy $_.Name) -Force
    }
    $backupSource = Join-Path $script:DeploySource 'backup'
    if (Test-Path -LiteralPath $backupSource) {
        $backupTarget = Join-Path $script:InstallDeploy 'backup'
        New-Item -ItemType Directory -Force -Path $backupTarget | Out-Null
        Copy-Item -Path (Join-Path $backupSource '*') -Destination $backupTarget -Recurse -Force
    }
    foreach ($sub in @('windows', 'certs')) {
        $source = Join-Path $script:DeploySource $sub
        if (Test-Path -LiteralPath $source) {
            $target = Join-Path $script:InstallDeploy $sub
            New-Item -ItemType Directory -Force -Path $target | Out-Null
            Copy-Item -Path (Join-Path $source '*') -Destination $target -Recurse -Force
        }
    }
    Write-Ok ('deployment files copied to ' + $script:InstallDeploy)
}

function Write-SecretsFile {
    if (Test-Path -LiteralPath $script:EnvFile) {
        Write-Info 'keeping the existing .env (secrets are already set)'
        return
    }
    $dbPassword = New-RandomSecret 24
    $jwtSecret = New-RandomSecret 48
    $bootstrapToken = New-RandomSecret 36
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('# Generated by install.ps1 on ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
    $lines.Add('# Keep this file private. Rotate both secrets after the first administrator exists.')
    $lines.Add('POSTGRES_PASSWORD=' + $dbPassword)
    $lines.Add('MAHMOUD_JWT_SECRET=' + $jwtSecret)
    $lines.Add('MAHMOUD_BOOTSTRAP_TOKEN=' + $bootstrapToken)
    $lines.Add('MAHMOUD_HTTP_PORT=' + [string]$HttpPort)
    $lines.Add('MAHMOUD_HTTPS_PORT=' + [string]$HttpsPort)
    $lines.Add('MAHMOUD_LOG_LEVEL=INFO')
    Set-Content -LiteralPath $script:EnvFile -Value $lines -Encoding ascii
    $credentialLines = New-Object System.Collections.Generic.List[string]
    $credentialLines.Add('Al-Mahmoud ERP installation ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
    $credentialLines.Add('URL: https://127.0.0.1:' + [string]$HttpsPort + '/')
    $credentialLines.Add('Bootstrap token (enter it on the first-run screen): ' + $bootstrapToken)
    $credentialLines.Add('')
    $credentialLines.Add('The token is single use: after the first setup it cannot provision another tenant.')
    $credentialLines.Add('Delete this file after the setup, or move it to the password manager.')
    $credentialLines.Add('The TLS certificate is self-signed; a production install replaces it under deploy\certs.')
    Set-Content -LiteralPath $script:CredentialFile -Value $credentialLines -Encoding utf8
    Write-Ok 'generated .env with new database, JWT and bootstrap secrets'
    Write-Info ('first-run bootstrap token: ' + $bootstrapToken)
    Write-Info ('also written to ' + $script:CredentialFile)
}

function Ensure-Certificate {
    $certFile = Join-Path $script:InstallDeploy 'certs\server.crt'
    $keyFile = Join-Path $script:InstallDeploy 'certs\server.key'
    if ((Test-Path -LiteralPath $certFile) -and (Test-Path -LiteralPath $keyFile)) {
        Write-Info 'TLS certificate already present'
        return
    }
    $generator = Join-Path $script:InstallDeploy 'generate-certs.ps1'
    if (-not (Test-Path -LiteralPath $generator)) { throw ('The certificate generator is missing: ' + $generator) }
    $psExe = (Get-Process -Id $PID).Path
    & $psExe -NoProfile -ExecutionPolicy Bypass -File $generator
    if ($LASTEXITCODE -ne 0) { throw 'Certificate generation failed; run deploy\generate-certs.ps1 by hand to see the error.' }
    Write-Ok 'generated a self-signed TLS certificate for localhost/127.0.0.1'
}

function Ensure-Images {
    if (Test-Image 'almahmoud-erp-api:prod') {
        Write-Info 'API image already loaded'
        return
    }
    $tar = Join-Path $script:BundleRoot 'images.tar'
    if (-not (Test-Path -LiteralPath $tar)) {
        throw ('The API image almahmoud-erp-api:prod is missing and this is not an offline bundle (no ' + $tar + '). On the build host run: docker build -f deploy/Dockerfile -t almahmoud-erp-api:prod . then recreate the bundle with deploy/windows/build-offline-bundle.ps1.')
    }
    Write-Info ('loading images from ' + $tar + ' (this takes a minute)')
    $code = Invoke-Docker @('load', '-i', $tar)
    if ($code -ne 0) { throw ('docker load failed with exit code ' + $code + '. The bundle copy is probably corrupt; compare it against SHA256SUMS.') }
    Write-Ok 'images loaded'
}

function Get-SetupStatus {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($null -eq $curl) { return $null }
    $text = & curl.exe -k -s --max-time 10 ('https://127.0.0.1:' + $HttpsPort + '/api/v1/setup/status')
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($text)) { return $null }
    try { return ($text | ConvertFrom-Json) } catch { return $null }
}

function Write-InstallReport {
    $setup = Get-SetupStatus
    $report = [ordered]@{
        generated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        action = $Action
        hostname = $env:COMPUTERNAME
        os = [Environment]::OSVersion.VersionString
        cpu_cores = (Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum
        memory_mb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB, 0)
        docker = (& docker version --format '{{.Server.Version}}' 2>$null)
        url = 'https://127.0.0.1:' + $HttpsPort + '/'
        http_port = $HttpPort
        https_port = $HttpsPort
        api_image = Get-ImageId 'almahmoud-erp-api:prod'
        postgres_image = Get-ImageId 'postgres:18'
        gateway_image = Get-ImageId 'nginx:1.29-alpine'
        setup_status = $setup
        health = Invoke-WebProbe ('https://127.0.0.1:' + $HttpsPort + '/api/v1/health')
        log_file = $script:LogFile
        duration_seconds = [math]::Round(((Get-Date) - $script:StartedAt).TotalSeconds, 1)
    }
    $reportPath = Join-Path (Join-Path $InstallPath 'logs') ('install-report-' + (Get-Date).ToString('yyyyMMdd-HHmmss') + '.json')
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding utf8
    Write-Info ('installation report: ' + $reportPath)
    return $reportPath
}

function Show-Summary {
    $setup = Get-SetupStatus
    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Green
    Write-Host ' Al-Mahmoud ERP is running' -ForegroundColor Green
    Write-Host '============================================================' -ForegroundColor Green
    Write-Host ('   Open:      https://127.0.0.1:' + $HttpsPort + '/')
    Write-Host ('   API:       https://127.0.0.1:' + $HttpsPort + '/api/v1/health')
    if ($null -ne $setup) {
        if ($setup.needs_setup -eq $true) {
            Write-Host '   First run: open the URL, accept the certificate warning, and enter the bootstrap token'
            Write-Host ('              printed above and stored in ' + $script:CredentialFile)
        }
        else {
            Write-Host '   Status:    this installation is already provisioned; sign in with the administrator account'
        }
    }
    Write-Host '   Control:   install.bat status | logs | stop | start'
    Write-Host ('   Files:     ' + $InstallPath)
    Write-Host ('   Log:       ' + $script:LogFile)
    Write-Host ''
    Write-Host '   The certificate is self-signed: the browser warning is expected on the pilot host.' -ForegroundColor Yellow
    Write-Host '   Delete credentials.txt after the first setup, or move the token to a password manager.' -ForegroundColor Yellow
}

# --------------------------------------------------------------------------

function Test-Writable([string] $Path) {
    try {
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
        $probe = Join-Path $Path ('.write-probe-' + [guid]::NewGuid().ToString('N'))
        Set-Content -LiteralPath $probe -Value 'probe' -Encoding ascii
        Remove-Item -LiteralPath $probe -Force
        return $true
    }
    catch { return $false }
}

if (($Action -eq 'install' -or $Action -eq 'start') -and -not $Elevated) {
    if (-not (Test-Administrator) -and -not (Test-Writable $InstallPath)) { Restart-Elevated }
}

New-Item -ItemType Directory -Force -Path $InstallPath | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallPath 'logs') | Out-Null
$script:LogFile = Join-Path (Join-Path $InstallPath 'logs') ('install-' + (Get-Date).ToString('yyyyMMdd-HHmmss') + '.log')
Start-Transcript -Path $script:LogFile -Append | Out-Null

try {
    Write-Host ''
    Write-Host 'Al-Mahmoud ERP - Windows installer' -ForegroundColor Cyan
    Write-Host ('action: ' + $Action + '   install path: ' + $InstallPath + '   ports: ' + $HttpPort + '/' + $HttpsPort)
    Write-Host ('log: ' + $script:LogFile)

    if ($Action -eq 'check') {
        Assert-Preflight
        Write-Host ''
        Write-Host 'Everything this installer needs is present. Run install.bat (without -Action check) to install.' -ForegroundColor Green
        exit 0
    }

    if ($Action -eq 'status') {
        if (-not (Test-Path -LiteralPath $script:ComposeFile) -or -not (Test-Path -LiteralPath $script:EnvFile)) {
            Write-Warn 'this machine has no installation yet; run install.bat first'
            exit 1
        }
        Write-Step 'stack status'
        Invoke-Compose @('ps') -AllowFailure | Out-Null
        $health = Invoke-WebProbe ('https://127.0.0.1:' + $HttpsPort + '/api/v1/health')
        if ($health -eq '200') { Write-Ok 'gateway and API are healthy' } else { Write-Warn ('health probe returned ' + $health) }
        $setup = Get-SetupStatus
        if ($null -ne $setup) {
            if ($setup.needs_setup -eq $true) { Write-Info 'first-run setup is still pending: open https://127.0.0.1:' + $HttpsPort + '/' }
            else { Write-Info 'installation already provisioned' }
        }
        exit 0
    }

    if ($Action -eq 'logs') {
        if (-not (Test-Path -LiteralPath $script:ComposeFile)) { throw 'this machine has no installation yet; run install.bat first' }
        Invoke-Compose @('logs', '--tail', '200') -AllowFailure | Out-Null
        exit 0
    }

    if ($Action -eq 'stop') {
        if (-not (Test-Path -LiteralPath $script:ComposeFile)) { throw 'this machine has no installation yet' }
        Write-Step 'stopping the stack'
        Invoke-Compose @('down') | Out-Null
        Write-Ok 'stopped; the database volume is kept for the next start'
        exit 0
    }

    if ($Action -eq 'start') {
        if (-not (Test-Path -LiteralPath $script:ComposeFile)) { throw 'this machine has no installation yet; run install.bat first' }
        Write-Step 'starting the stack'
        Invoke-Compose @('up', '-d', '--wait', '--wait-timeout', '240') | Out-Null
        if (-not (Wait-Healthy 180)) { throw 'the stack started but the TLS health endpoint did not answer within 3 minutes; run install.bat logs' }
        Write-Ok 'healthy'
        Show-Summary
        exit 0
    }

    # install
    Assert-Preflight
    Write-Step 'copying deployment files'
    Copy-DeployAssets

    Write-Step 'secrets and certificate'
    Write-SecretsFile
    Ensure-Certificate

    Write-Step 'container images'
    Ensure-Images

    Write-Step 'starting the stack'
    Write-Info 'first start runs the database migrations; this can take up to two minutes'
    Invoke-Compose @('up', '-d', '--wait', '--wait-timeout', '240') | Out-Null
    if (-not (Wait-Healthy 240)) {
        throw 'the containers are up but the API did not become healthy. Run install.bat logs and read the api/migrate lines; the database password in .env must match the one the volume was created with.'
    }
    Write-Ok 'stack is healthy'

    $reportPath = Write-InstallReport
    Show-Summary
    if (-not $NoBrowser) {
        try { Start-Process ('https://127.0.0.1:' + $HttpsPort + '/') } catch { Write-Warn 'could not open the browser automatically; open the URL above' }
    }
    Write-Host ''
    Write-Host ('Installation finished in ' + [math]::Round(((Get-Date) - $script:StartedAt).TotalSeconds, 1) + ' s.') -ForegroundColor Green
    exit 0
}
catch {
    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Red
    Write-Host ' INSTALLATION STOPPED' -ForegroundColor Red
    Write-Host '============================================================' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ''
    Write-Host 'What to try next:'
    $message = $_.Exception.Message
    if ($message -match 'Docker') {
        Write-Host '  1. Start Docker Desktop and wait until it reports "Engine running".'
        Write-Host '  2. Run install.bat again.'
    }
    elseif ($message -match 'port') {
        Write-Host '  1. Close the program that holds the port, or pick different ports:'
        Write-Host '       install.bat install -HttpPort 8081 -HttpsPort 8444'
    }
    elseif ($message -match 'health|migrations|password') {
        Write-Host '  1. Run: install.bat logs'
        Write-Host '  2. If the database volume was created with another password, remove the'
        Write-Host '     volume with: docker compose -p almahmoud-erp-prod down -v'
        Write-Host '     (this deletes the pilot database) and install again.'
    }
    elseif ($message -match 'image|images.tar') {
        Write-Host '  1. Use the offline bundle directory that contains images.tar, or'
        Write-Host '  2. Build the image on this machine: docker build -f deploy/Dockerfile -t almahmoud-erp-api:prod .'
    }
    else {
        Write-Host '  1. Read the full log path printed below.'
        Write-Host '  2. Fix the reported item and run install.bat again; nothing was deleted.'
    }
    Write-Host ''
    Write-Host ('Full log: ' + $script:LogFile) -ForegroundColor Yellow
    exit 1
}
finally {
    try { Stop-Transcript | Out-Null } catch { }
}
