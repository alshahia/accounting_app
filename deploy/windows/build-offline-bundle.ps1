# Build the offline Windows installation bundle for the v1 release.
#
# Run on an internet-connected build host that already has the images:
#   docker build -f deploy/Dockerfile -t almahmoud-erp-api:prod .
#   docker pull postgres:18; docker pull nginx:1.29-alpine
#   pwsh -File deploy/windows/build-offline-bundle.ps1
#
# The output directory is self-contained: it carries the three images as one
# tar, the compose files, the TLS certificate generator, the backup scripts and
# the runbooks. Copy it to the offline Windows host and follow
# docs/10-runbooks/offline-windows-install.md.
[CmdletBinding()]
param(
    [string]$OutputDirectory = "build/windows-bundle",
    [string]$ApiImage = "almahmoud-erp-api:prod",
    [string]$PostgresImage = "postgres:18",
    [string]$GatewayImage = "nginx:1.29-alpine"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$output = Join-Path $root $OutputDirectory

function Assert-Image([string]$Name) {
    & docker image inspect $Name 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "image $Name is missing; build or pull it before bundling"
    }
    $id = (& docker image inspect --format "{{.Id}}" $Name).Trim()
    Write-Host "image $Name = $id"
    return $id
}

$images = [ordered]@{}
$images[$ApiImage] = Assert-Image $ApiImage
$images[$PostgresImage] = Assert-Image $PostgresImage
$images[$GatewayImage] = Assert-Image $GatewayImage

if (Test-Path $output) { Remove-Item $output -Recurse -Force }
New-Item -ItemType Directory -Force -Path $output | Out-Null

Write-Host "saving images to images.tar (this takes a minute)"
& docker save -o (Join-Path $output "images.tar") $ApiImage $PostgresImage $GatewayImage
if ($LASTEXITCODE -ne 0) { throw "docker save failed" }

$copy = @(
    "deploy/docker-compose.prod.yml",
    "deploy/docker-compose.offline.yml",
    "deploy/nginx.conf",
    "deploy/generate-certs.ps1",
    ".env.example",
    "deploy/backup/common.ps1",
    "deploy/backup/backup.ps1",
    "deploy/backup/restore-drill.ps1",
    "deploy/backup/register-scheduled-backup.ps1",
    "docs/10-runbooks/release.md",
    "docs/10-runbooks/incident.md",
    "docs/10-runbooks/tenant-provisioning.md",
    "docs/10-runbooks/backup-restore.md",
    "docs/10-runbooks/offline-windows-install.md"
)
foreach ($relative in $copy) {
    $source = Join-Path $root $relative
    if (-not (Test-Path $source)) { throw "missing bundle input: $relative" }
    $target = Join-Path $output $relative
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Copy-Item $source $target
}

$version = (& git -C $root describe --tags --always 2>$null)
if ([string]::IsNullOrWhiteSpace($version)) { $version = "unknown" }
$manifest = @{
    built_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    git_revision = $version.Trim()
    images = $images
}
$manifest | ConvertTo-Json | Set-Content (Join-Path $output "manifest.json") -Encoding utf8

Get-ChildItem $output -Recurse -File |
    Where-Object { $_.Name -ne "SHA256SUMS" } |
    ForEach-Object {
        $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $relative = [System.IO.Path]::GetRelativePath($output, $_.FullName).Replace([char]92, [char]47)
        "$hash  $relative"
    } | Set-Content (Join-Path $output "SHA256SUMS") -Encoding utf8

$sizeMb = [math]::Round((Get-ChildItem $output -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "bundle ready: $output ($sizeMb MB)"
Write-Host "copy the directory to the offline host and follow docs/10-runbooks/offline-windows-install.md"
