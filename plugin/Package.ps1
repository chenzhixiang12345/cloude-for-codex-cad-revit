param(
    [Parameter(Mandatory)]
    [ValidateRange(2020, 2027)]
    [int]$RevitVersion,
    [string]$EngineExe = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist/tumo-engine.exe'),
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist')
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

[void][IO.Directory]::CreateDirectory($OutputDirectory)
$output = (Resolve-Path -LiteralPath $OutputDirectory).Path
$stage = Join-Path $env:TEMP "tumo-revit-$RevitVersion-$([Guid]::NewGuid().ToString('N'))"
[void][IO.Directory]::CreateDirectory($stage)
try {
    $payload = Join-Path $stage 'TuMo'
    dotnet build $project -c $configuration --output $payload --nologo
    if ($LASTEXITCODE -ne 0) { throw "Revit $RevitVersion build failed." }

    Copy-Item -LiteralPath $engine -Destination (Join-Path $payload 'tumo-engine.exe')
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
