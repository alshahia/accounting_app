# Development-only PostgreSQL 18 cluster for this workspace.
# It never touches the installed Windows PostgreSQL service and listens on
# 127.0.0.1:55432 only.
#
#   pwsh -File deploy/dev-postgres.ps1 status
#   pwsh -File deploy/dev-postgres.ps1 init
#   pwsh -File deploy/dev-postgres.ps1 start
#   pwsh -File deploy/dev-postgres.ps1 stop
param(
    [ValidateSet('init', 'start', 'stop', 'status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'

$PgBin = 'C:\Program Files\PostgreSQL\18\bin'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $ProjectRoot '.local\pgdata'
$LogFile = Join-Path $ProjectRoot '.local\pg.log'
$Port = 55432
$Databases = @('almahmoud', 'almahmoud_test')
$PgCtl = Join-Path $PgBin 'pg_ctl.exe'
$Psql = Join-Path $PgBin 'psql.exe'
$Createdb = Join-Path $PgBin 'createdb.exe'

function Test-ServerRunning {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    return $null -ne $listener
}

function Invoke-Initdb {
    if (Test-Path $DataDir) {
        Write-Host "cluster already initialised at $DataDir"
        return
    }
    New-Item -ItemType Directory -Force (Split-Path -Parent $DataDir) | Out-Null
    & (Join-Path $PgBin 'initdb.exe') -D $DataDir -U postgres --auth=trust --encoding=UTF8 --locale=C
    if ($LASTEXITCODE -ne 0) { throw 'initdb failed' }
}

function Start-Server {
    if (Test-ServerRunning) {
        Write-Host "PostgreSQL already listening on 127.0.0.1:$Port"
    }
    else {
        Invoke-Initdb
        $arguments = "-D `"$DataDir`" -l `"$LogFile`" -o `"-p $Port -c listen_addresses=127.0.0.1`" -w start"
        # A detached process is required: a foreground pg_ctl keeps the server's
        # inherited handles open and the calling shell would wait forever. Poll
        # the port instead of -Wait: pg_ctl can exit before the port is ready.
        Start-Process -FilePath $PgCtl -ArgumentList $arguments -WindowStyle Hidden | Out-Null
        $deadline = (Get-Date).AddSeconds(30)
        while (-not (Test-ServerRunning) -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 250
        }
        if (-not (Test-ServerRunning)) {
            throw "PostgreSQL did not start within 30 s; see $LogFile"
        }
        Write-Host "PostgreSQL listening on 127.0.0.1:$Port"
    }
    foreach ($database in $Databases) {
        $exists = & $Psql -h 127.0.0.1 -p $Port -U postgres -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$database'"
        if ($exists -notmatch '1') {
            & $Createdb -h 127.0.0.1 -p $Port -U postgres $database
            if ($LASTEXITCODE -ne 0) { throw "createdb $database failed" }
            Write-Host "created database $database"
        }
    }
}

function Stop-Server {
    if (-not (Test-ServerRunning)) {
        Write-Host "PostgreSQL is not running on port $Port"
        return
    }
    & $PgCtl -D $DataDir -m fast -w stop
    if ($LASTEXITCODE -ne 0) { throw 'pg_ctl stop failed' }
}

switch ($Action) {
    'init' { Invoke-Initdb }
    'start' { Start-Server }
    'stop' { Stop-Server }
    'status' {
        if (Test-ServerRunning) {
            Write-Host "running on 127.0.0.1:$Port (data: $DataDir)"
            & $Psql -h 127.0.0.1 -p $Port -U postgres -d postgres -tAc 'SELECT version();'
        }
        else {
            Write-Host "not running (data: $DataDir)"
        }
    }
}
