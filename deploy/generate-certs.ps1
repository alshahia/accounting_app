# Generate the self-signed TLS certificate the production gateway expects.
# Run once on the target host before the first start:
#
#   pwsh -File deploy/generate-certs.ps1 -DnsName erp.example.local
#
# The files land in deploy/certs/ (git-ignored). For a real installation, replace
# them with the organisation's certificate and key under the same file names.
param(
    [string]$DnsName = "localhost",
    [int]$ValidYears = 2
)

$ErrorActionPreference = 'Stop'
$CertDir = Join-Path $PSScriptRoot 'certs'
New-Item -ItemType Directory -Force $CertDir | Out-Null

function ConvertTo-Pem {
    param([string]$Label, [byte[]]$Bytes)
    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.AppendLine("-----BEGIN $Label-----")
    [void]$builder.AppendLine([Convert]::ToBase64String($Bytes, [Base64FormattingOptions]::InsertLineBreaks))
    [void]$builder.AppendLine("-----END $Label-----")
    return $builder.ToString()
}

$rsa = [System.Security.Cryptography.RSA]::Create(2048)
$sanBuilder = [System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
$sanBuilder.AddDnsName($DnsName)
if ($DnsName -ne 'localhost') {
    $sanBuilder.AddDnsName('localhost')
}
$sanBuilder.AddIpAddress([System.Net.IPAddress]::Loopback)

$request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
    "CN=$DnsName",
    $rsa,
    [System.Security.Cryptography.HashAlgorithmName]::SHA256,
    [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
)
$request.CertificateExtensions.Add($sanBuilder.Build())
$request.CertificateExtensions.Add(
    [System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true)
)
$request.CertificateExtensions.Add(
    [System.Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new(
        [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature -bor
        [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::KeyEncipherment,
        $true
    )
)

$certificate = $request.CreateSelfSigned((Get-Date).AddDays(-1), (Get-Date).AddYears($ValidYears))
$certPath = Join-Path $CertDir 'server.crt'
$keyPath = Join-Path $CertDir 'server.key'
[System.IO.File]::WriteAllText($certPath, (ConvertTo-Pem -Label 'CERTIFICATE' -Bytes $certificate.RawData))
[System.IO.File]::WriteAllText($keyPath, (ConvertTo-Pem -Label 'PRIVATE KEY' -Bytes $rsa.ExportPkcs8PrivateKey()))

Write-Host "certificate: $certPath"
Write-Host "private key: $keyPath"
Write-Host "subject: CN=$DnsName (self-signed, valid $ValidYears year(s))"
