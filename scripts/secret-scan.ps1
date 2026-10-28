<#
.SYNOPSIS
  Fail when tracked files or git history contain hard-coded credentials.
.DESCRIPTION
  The gate that must pass before this tree or a public snapshot leaves the
  machine: private keys, cloud tokens and database URLs with a real password.
  Placeholders used by tests, examples and documentation are recognised and
  skipped. Exit code 0 means clean; 1 means findings were printed.

  The scan deliberately does not flag the demo tenant credentials that the seed
  script creates: they are published defaults for a local demo database, not a
  credential to any deployed system.
.EXAMPLE
  pwsh -File scripts/secret-scan.ps1
  pwsh -File scripts/secret-scan.ps1 -Root build/public-export -History
#>
[CmdletBinding()]
param(
    [string] $Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [switch] $History
)

$ErrorActionPreference = 'Stop'

$patterns = @(
    @{ Name = 'private key'; Regex = '-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----' },
    @{ Name = 'AWS access key'; Regex = 'AKIA[0-9A-Z]{16}' },
    @{ Name = 'GitHub token'; Regex = 'gh[pousr]_[A-Za-z0-9]{20,}' },
    @{ Name = 'OpenAI-style key'; Regex = 'sk-[A-Za-z0-9]{20,}' },
    @{ Name = 'Slack token'; Regex = 'xox[baprs]-[A-Za-z0-9-]{10,}' },
    @{ Name = 'database URL with password'; Regex = 'postgres(ql)?(\+asyncpg)?://[^''"\s]+:[^''"\s]+@' },
    @{ Name = 'pilot credential'; Regex = ([regex]::Escape('Pilot' + '-Admin-' + '2026')) }
)

$placeholderPrefixes = @('postgres', 'password', 'pass', 'changeme', 'example', 'placeholder', 'test', 'demo', 'topsecret')

function Test-PlaceholderPassword([string] $Password) {
    if ([string]::IsNullOrWhiteSpace($Password)) { return $true }
    if ($Password.Contains('<') -or $Password.Contains('...') -or $Password.Contains('$')) { return $true }
    foreach ($candidate in $placeholderPrefixes) {
        if ($Password -like ($candidate + '*')) { return $true }
    }
    return $false
}

function Get-SecretMatch([string] $Line, [hashtable] $Pattern) {
    $match = [regex]::Match($Line, $Pattern.Regex)
    if (-not $match.Success) { return $null }
    if ($Pattern.Name -eq 'database URL with password') {
        $url = $match.Value
        $authority = $url.Substring($url.IndexOf('://') + 3).Split('/')[0]
        if (-not $authority.Contains(':')) { return $null }
        $password = $authority.Substring($authority.IndexOf(':') + 1)
        if (Test-PlaceholderPassword $password) { return $null }
    }
    return $match.Value
}

$findings = New-Object System.Collections.Generic.List[object]

Push-Location $Root
try {
    $insideRepository = $false
    & git rev-parse --is-inside-work-tree 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $insideRepository = $true }
    $files = @()
    if ($insideRepository) { $files = @(git ls-files) }
    if ($files.Count -eq 0) {
        # a plain folder (a sanitised snapshot before its first commit): walk it
        $files = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force |
            Where-Object { $_.FullName -notmatch '[\\/]\.git[\\/]' } |
            ForEach-Object { $_.FullName.Substring($Root.Length).TrimStart([char]92, [char]47) })
    }
    if ($files.Count -eq 0) { throw "no files to scan under $Root" }

    foreach ($file in $files) {
        $path = Join-Path $Root $file
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        $item = Get-Item -LiteralPath $path
        if ($item.Length -gt 3000000) { continue }
        $bytes = [System.IO.File]::ReadAllBytes($path)
        if ([Array]::IndexOf($bytes, [byte]0) -ge 0) { continue }
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        $lines = $text -split '\r?\n'
        for ($i = 0; $i -lt $lines.Count; $i++) {
            foreach ($pattern in $patterns) {
                $hit = Get-SecretMatch $lines[$i] $pattern
                if ($null -ne $hit) {
                    $findings.Add([pscustomobject]@{
                        Where = ($file + ':' + ($i + 1))
                        Pattern = $pattern.Name
                        Match = $hit.Trim()
                    })
                }
            }
        }
    }

    if ($History) {
        $patch = @(git log --all -p --format='commit %H')
        $commit = 'unknown'
        for ($i = 0; $i -lt $patch.Count; $i++) {
            $line = $patch[$i]
            if ($line -like 'commit *') { $commit = $line.Substring(7).Trim(); continue }
            if (-not $line.StartsWith('+')) { continue }
            foreach ($pattern in $patterns) {
                $hit = Get-SecretMatch $line $pattern
                if ($null -ne $hit) {
                    $findings.Add([pscustomobject]@{
                        Where = ('history ' + $commit.Substring(0, [Math]::Min(12, $commit.Length)))
                        Pattern = $pattern.Name
                        Match = $hit.Trim()
                    })
                }
            }
        }
    }
}
finally {
    Pop-Location
}

if ($findings.Count -eq 0) {
    Write-Host 'secret-scan-ok: no credentials found' -ForegroundColor Green
    exit 0
}

Write-Host ('secret-scan: ' + $findings.Count + ' finding(s)') -ForegroundColor Red
$findings | Select-Object -First 50 | Format-Table -AutoSize
Write-Host 'Remove or replace the value before publishing; rotate it if it was ever real.' -ForegroundColor Yellow
exit 1
