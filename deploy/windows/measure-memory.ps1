<#
.SYNOPSIS
  Sample the memory the Al-Mahmoud ERP stack actually uses, and report the peak.
.DESCRIPTION
  Runs docker stats sampling for a bounded window and writes a JSON report with
  every sample, the peak of the sum of the four containers, the host total and
  the compose limits. Use it to fill the deployment sizing record with measured
  numbers instead of estimates. The numbers describe the machine it runs on:
  a host-class result, not the 2 vCPU / 4 GB target, unless it is run there.
.PARAMETER Minutes
  How long to sample. Default 10.
.EXAMPLE
  pwsh -File deploy/windows/measure-memory.ps1 -Minutes 10
#>
[CmdletBinding()]
param(
    [string] $InstallPath = 'C:\AlMahmoud',
    [string] $ProjectName = '',
    [int] $Minutes = 10,
    [int] $IntervalSeconds = 15,
    [string] $OutputPath = ''
)

$ErrorActionPreference = 'Stop'
$composeFile = Join-Path $InstallPath 'deploy\docker-compose.prod.yml'
$envFile = Join-Path $InstallPath '.env'
if (-not (Test-Path -LiteralPath $composeFile)) { throw ('No installation at ' + $InstallPath + '; pass -InstallPath.') }

$composeArguments = @('compose')
if ($ProjectName -ne '') { $composeArguments += @('-p', $ProjectName) }
$composeArguments += @('--env-file', $envFile, '-f', $composeFile)

function Convert-MemoryToMb([string] $Text) {
    $trimmed = $Text.Trim()
    $match = [regex]::Match($trimmed, '^([0-9.]+)\s*([KMG]i?B|B)$')
    if (-not $match.Success) { return 0 }
    $value = [double]$match.Groups[1].Value
    switch ($match.Groups[2].Value) {
        'B' { return $value / 1MB }
        'KiB' { return $value / 1KB }
        'KB' { return $value / 1KB }
        'MiB' { return $value }
        'MB' { return $value }
        'GiB' { return $value * 1024 }
        'GB' { return $value * 1024 }
    }
    return 0
}

$running = @(& docker @composeArguments ps --status running -q 2>$null)
if ($running.Count -eq 0) { throw 'The stack is not running; start it with install.bat start before sampling.' }

$samples = New-Object System.Collections.Generic.List[object]
$deadline = (Get-Date).AddMinutes($Minutes)
$peak = 0
$peakCpu = 0
$sampleNumber = 0

Write-Host ('sampling every ' + $IntervalSeconds + ' s for ' + $Minutes + ' min; press Ctrl+C to stop early') -ForegroundColor Cyan
while ((Get-Date) -lt $deadline) {
    $sampleNumber++
    $lines = @(& docker stats --no-stream --no-trunc --format '{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}' 2>$null)
    $rows = New-Object System.Collections.Generic.List[object]
    $sum = 0
    $cpuSum = 0
    foreach ($line in $lines) {
        $parts = $line.Split('|')
        if ($parts.Count -lt 3) { continue }
        $name = $parts[0]
        if ($name -notlike '*almahmoud*' -and $name -notlike '*installer-test*') { continue }
        $usage = $parts[1].Split('/')[0]
        $mb = Convert-MemoryToMb $usage
        $cpu = [double]($parts[2].Replace('%', '').Trim())
        $sum += $mb
        $cpuSum += $cpu
        $rows.Add([pscustomobject]@{ name = $name; memory_mb = [math]::Round($mb, 1); cpu_percent = $cpu })
    }
    if ($rows.Count -gt 0) {
        $samples.Add([pscustomobject]@{ at = (Get-Date).ToString('HH:mm:ss'); total_mb = [math]::Round($sum, 1); containers = $rows })
        if ($sum -gt $peak) { $peak = $sum }
        if ($cpuSum -gt $peakCpu) { $peakCpu = $cpuSum }
        Write-Host ('  sample ' + $sampleNumber + ': ' + [math]::Round($sum, 0) + ' MB total, ' + [math]::Round($cpuSum, 1) + ' % cpu')
    }
    if ((Get-Date).AddSeconds($IntervalSeconds) -lt $deadline) { Start-Sleep -Seconds $IntervalSeconds }
}

if ($samples.Count -eq 0) { throw 'No samples were collected; are the containers named like the compose project?' }

$limits = @()
foreach ($service in @('db', 'api', 'gateway', 'migrate')) {
    $id = @(& docker @composeArguments ps -q $service 2>$null)
    if ($id.Count -eq 0) { continue }
    $limitBytes = & docker inspect --format '{{.HostConfig.Memory}}' $id[0] 2>$null
    if ($LASTEXITCODE -eq 0 -and [int64]$limitBytes -gt 0) {
        $limits += [pscustomobject]@{ service = $service; limit_mb = [math]::Round([int64]$limitBytes / 1MB, 0) }
    }
}

$hostMb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB, 0)
$report = [ordered]@{
    generated_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    hostname = $env:COMPUTERNAME
    host_total_mb = $hostMb
    host_cpu_cores = (Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum
    sample_minutes = $Minutes
    sample_interval_seconds = $IntervalSeconds
    samples_collected = $samples.Count
    stack_peak_mb = [math]::Round($peak, 1)
    stack_peak_cpu_percent = [math]::Round($peakCpu, 1)
    container_limits = $limits
    note = 'Measured on this machine. On a 2 vCPU / 4 GB target the compose limits (2.0 cpu / 2g db, 1g api) are the binding ceiling; run the same script there for the deployment record.'
    samples = $samples
}
if ($OutputPath -eq '') {
    $logDir = Join-Path $InstallPath 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $OutputPath = Join-Path $logDir ('memory-' + (Get-Date).ToString('yyyyMMdd-HHmmss') + '.json')
}
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputPath -Encoding utf8

Write-Host ''
Write-Host '============================================================' -ForegroundColor Green
Write-Host (' measured peak: ' + $report.stack_peak_mb + ' MB total across the stack') -ForegroundColor Green
Write-Host (' host: ' + $report.host_cpu_cores + ' cores / ' + $hostMb + ' MB RAM, ' + $samples.Count + ' samples')
Write-Host (' report: ' + $OutputPath)
Write-Host '============================================================' -ForegroundColor Green
