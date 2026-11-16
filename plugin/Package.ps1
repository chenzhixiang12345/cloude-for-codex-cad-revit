param(
    [Parameter(Mandatory)]
    [ValidateRange(2020, 2027)]
    [int]$RevitVersion,
    [string]$EngineExe = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist/tumo-engine.exe'),
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist'),
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$SigningThumbprint,
    [switch]$AllowLocalTestCertificate
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$project = Join-Path $PSScriptRoot 'src/TuMo.Revit/TuMo.Revit.csproj'
$versionTag = $RevitVersion - 2000
$configuration = "Release R$versionTag"
$engine = (Resolve-Path -LiteralPath $EngineExe).Path
if ((Get-Item -LiteralPath $engine).Length -eq 0) {
    throw "Engine executable is empty: $engine"
}

if ($AllowLocalTestCertificate -and !$SigningThumbprint) {
    throw 'Specify the local test certificate with -SigningThumbprint.'
}
$certificate = $null
if ($SigningThumbprint) {
    $now = Get-Date
    $certificate = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object {
        $_.Thumbprint -eq $SigningThumbprint -and $_.HasPrivateKey -and
        $_.NotBefore -le $now -and $_.NotAfter -gt $now
    } | Select-Object -First 1
    if (!$certificate) { throw 'The specified valid code-signing certificate and private key were not found.' }
    if ($AllowLocalTestCertificate -and $certificate.Subject -cne $certificate.Issuer) {
        throw '-AllowLocalTestCertificate is only for a self-signed local test certificate.'
    }
}

[void][IO.Directory]::CreateDirectory($OutputDirectory)
$output = (Resolve-Path -LiteralPath $OutputDirectory).Path
$stage = Join-Path $env:TEMP "tumo-revit-$RevitVersion-$([Guid]::NewGuid().ToString('N'))"
[void][IO.Directory]::CreateDirectory($stage)
try {
    $payload = Join-Path $stage 'TuMo'
    dotnet build $project -c $configuration --output $payload --nologo
    if ($LASTEXITCODE -ne 0) { throw "Revit $RevitVersion build failed." }

    Copy-Item -LiteralPath $engine -Destination (Join-Path $payload 'tumo-engine.exe')
    if ($certificate) {
        $untrustedRoot = [ComponentModel.Win32Exception]::new([int]0x800B0109).Message
        foreach ($file in 'TuMo.Revit.dll', 'TuMo.Core.dll', 'tumo-engine.exe') {
            $path = Join-Path $payload $file
            Set-AuthenticodeSignature -LiteralPath $path -Certificate $certificate -HashAlgorithm SHA256 |
                Out-Null
            $signature = Get-AuthenticodeSignature -LiteralPath $path
            # A local package can be prepared before its signer is trusted on this computer.
            $locallyUntrusted = $AllowLocalTestCertificate -and
                $signature.Status -eq 'UnknownError' -and $signature.StatusMessage -ceq $untrustedRoot
            if ($signature.SignatureType -ne 'Authenticode' -or
                $signature.SignerCertificate.Thumbprint -ne $certificate.Thumbprint -or
                ($signature.Status -ne 'Valid' -and !$locallyUntrusted)) {
                throw "Signing $file failed verification: $($signature.StatusMessage)"
            }
        }
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'src/TuMo.Revit/TuMo.Revit.addin') -Destination $stage
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Install.ps1') -Destination $stage
    Copy-Item -LiteralPath (Join-Path $root 'docs/test-point-1.md') -Destination $stage
    Copy-Item -LiteralPath (Join-Path $root 'docs/revit-review.md') -Destination $stage
    Set-Content -LiteralPath (Join-Path $stage 'RevitVersion.txt') -Value $RevitVersion -Encoding ASCII
    $included = @(
        (Join-Path $stage 'Install.ps1'),
        (Join-Path $stage 'TuMo.Revit.addin'),
        (Join-Path $stage 'RevitVersion.txt'),
        (Join-Path $stage 'test-point-1.md'),
        (Join-Path $stage 'revit-review.md'),
        $payload
    )
    $zip = Join-Path $output "TuMo-Revit-$RevitVersion.zip"
    Compress-Archive -LiteralPath $included -DestinationPath $zip -Force
    Write-Output $zip
}
finally {
    $resolvedStage = (Resolve-Path -LiteralPath $stage).Path
    $resolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path
    if ([IO.Path]::GetDirectoryName($resolvedStage) -eq $resolvedTemp -and
        [IO.Path]::GetFileName($resolvedStage) -like "tumo-revit-$RevitVersion-*") {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
