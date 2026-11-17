# Shared helpers for the backup and restore-drill scripts.
# Dot-source this file; it defines Find-PgBin, Invoke-Psql, ConvertFrom-MahmoudUrl,
# Protect-File, Unprotect-File and Write-AccessLog.
Set-StrictMode -Version Latest

function Find-PgBin {
    param([string]$Explicit)
    if ($Explicit) {
        if (Test-Path (Join-Path $Explicit 'pg_dump.exe')) { return $Explicit }
        throw "MAHMOUD_PG_BIN points at a directory without pg_dump.exe: $Explicit"
    }
    $candidates = @()
    if ($env:MAHMOUD_PG_BIN) { $candidates += $env:MAHMOUD_PG_BIN }
    $candidates += (Get-ChildItem 'C:\Program Files\PostgreSQL' -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'bin' })
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path (Join-Path $candidate 'pg_dump.exe'))) { return $candidate }
    }
    $onPath = Get-Command pg_dump.exe -ErrorAction SilentlyContinue
    if ($onPath) { return (Split-Path -Parent $onPath.Source) }
    throw 'pg_dump.exe not found: set MAHMOUD_PG_BIN to the PostgreSQL bin directory'
}

function ConvertFrom-MahmoudUrl {
    param([string]$Url)
    if (-not $Url) { throw 'database URL is empty; set MAHMOUD_BACKUP_DATABASE_URL or pass -DatabaseUrl' }
    $clean = $Url -replace '^postgresql\+[a-z0-9]+://', 'postgresql://'
    $uri = [Uri]$clean
    $userInfo = $uri.UserInfo
    $user = ($userInfo -split ':', 2)[0]
    $password = if ($userInfo -contains ':') { ($userInfo -split ':', 2)[1] } else { '' }
    return [pscustomobject]@{
        Uri = $clean
        User = [Uri]::UnescapeDataString($user)
        Password = [Uri]::UnescapeDataString($password)
        Host = $uri.Host
        Port = if ($uri.Port -gt 0) { $uri.Port } else { 5432 }
        Database = $uri.AbsolutePath.Trim('/')
    }
}

function Get-AdminUrl {
    param($Connection, [string]$AdminDatabase = 'postgres')
    $password = [Uri]::EscapeDataString($Connection.Password)
    return "postgresql://$($Connection.User):$password@$($Connection.Host):$($Connection.Port)/$AdminDatabase"
}

function Invoke-Psql {
    param(
        [string]$PgBin,
        [string]$Url,
        [string]$Sql,
        [switch]$TuplesOnly
    )
    $psql = Join-Path $PgBin 'psql.exe'
    $arguments = @('-X', '-q', '-v', 'ON_ERROR_STOP=1', '-d', $Url, '-c', $Sql)
    if ($TuplesOnly) { $arguments = @('-X', '-q', '-t', '-A', '-v', 'ON_ERROR_STOP=1', '-d', $Url, '-c', $Sql) }
    $output = & $psql @arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "psql failed ($LASTEXITCODE): $($output -join [Environment]::NewLine)"
    }
    return ($output -join [Environment]::NewLine).Trim()
}

function Read-KeyMaterial {
    param([string]$KeyFile)
    if (-not $KeyFile -or -not (Test-Path $KeyFile)) {
        throw "backup key file not found: $KeyFile. Generate one with: openssl rand -base64 48 > key file. The key never lives in the repository."
    }
    $bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $KeyFile))
    if ($bytes.Length -lt 32) { throw 'backup key must be at least 32 bytes' }
    return $bytes
}

function New-RandomBytes {
    param([int]$Count)
    $bytes = [byte[]]::new($Count)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return $bytes
}

function Protect-File {
    param([string]$PlainPath, [string]$EncryptedPath, [byte[]]$KeyMaterial)
    $salt = New-RandomBytes 16
    $nonce = New-RandomBytes 12
    $derive = [System.Security.Cryptography.Rfc2898DeriveBytes]::new(
        $KeyMaterial, $salt, 200000, [System.Security.Cryptography.HashAlgorithmName]::SHA256)
    $key = $derive.GetBytes(32)
    $plain = [System.IO.File]::ReadAllBytes($PlainPath)
    $cipher = [byte[]]::new($plain.Length)
    $tag = [byte[]]::new(16)
    $aes = [System.Security.Cryptography.AesGcm]::new($key, 16)
    try {
        $aes.Encrypt($nonce, $plain, $cipher, $tag)
    }
    finally {
        $aes.Dispose()
        [array]::Clear($key, 0, $key.Length)
    }
    # AMEENBK1 is a persisted file-format marker from before the product rename;
# it is deliberately unchanged so every earlier backup still decrypts (decision 0009).
$magic = [Text.Encoding]::ASCII.GetBytes('AMEENBK1')
    $stream = [System.IO.File]::Create($EncryptedPath)
    try {
        $stream.Write($magic, 0, $magic.Length)
        $stream.Write($salt, 0, $salt.Length)
        $stream.Write($nonce, 0, $nonce.Length)
        $stream.Write($tag, 0, $tag.Length)
        $stream.Write($cipher, 0, $cipher.Length)
    }
    finally {
        $stream.Dispose()
    }
    return (Get-FileHash -Path $EncryptedPath -Algorithm SHA256).Hash
}

function Unprotect-File {
    param([string]$EncryptedPath, [string]$PlainPath, [byte[]]$KeyMaterial)
    $bytes = [System.IO.File]::ReadAllBytes($EncryptedPath)
    $magic = [Text.Encoding]::ASCII.GetString($bytes, 0, 8)
    if ($magic -ne 'AMEENBK1') { throw "not an Al-Mahmoud backup file: $EncryptedPath" }
    $salt = $bytes[8..23]
    $nonce = $bytes[24..35]
    $tag = $bytes[36..51]
    $cipher = $bytes[52..($bytes.Length - 1)]
    $derive = [System.Security.Cryptography.Rfc2898DeriveBytes]::new(
        $KeyMaterial, $salt, 200000, [System.Security.Cryptography.HashAlgorithmName]::SHA256)
    $key = $derive.GetBytes(32)
    $plain = [byte[]]::new($cipher.Length)
    $aes = [System.Security.Cryptography.AesGcm]::new($key, 16)
    try {
        $aes.Decrypt($nonce, $cipher, $tag, $plain)
    }
    finally {
        $aes.Dispose()
        [array]::Clear($key, 0, $key.Length)
    }
    [System.IO.File]::WriteAllBytes($PlainPath, $plain)
    return (Get-FileHash -Path $PlainPath -Algorithm SHA256).Hash
}

function Write-AccessLog {
    param([string]$LogPath, [hashtable]$Entry)
    $Entry['timestamp'] = (Get-Date).ToUniversalTime().ToString('o')
    $line = ($Entry | ConvertTo-Json -Compress)
    Add-Content -Path $LogPath -Value $line -Encoding utf8
}
