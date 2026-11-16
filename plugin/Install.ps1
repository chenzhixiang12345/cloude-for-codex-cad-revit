$ErrorActionPreference = 'Stop'
$version = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'RevitVersion.txt') -Raw).Trim()
if ($version -notmatch '^202[0-7]$') {
    throw "Invalid Revit version: $version"
}

$payload = Join-Path $PSScriptRoot 'TuMo'
$required = 'TuMo.Revit.dll', 'TuMo.Core.dll', 'tumo-engine.exe'
foreach ($file in $required) {
    if (!(Test-Path -LiteralPath (Join-Path $payload $file) -PathType Leaf)) {
        throw "Missing installation file: $file"
    }
}

$yearFolder = if ($version -eq '2027') { "Revit $version" } else { $version }
$addinFolder = Join-Path $env:APPDATA "Autodesk\Revit\Addins\$yearFolder"
$addinPath = Join-Path $addinFolder 'TuMo.Revit.addin'
$installedPayload = Join-Path $addinFolder 'TuMo'
[xml]$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'TuMo.Revit.addin') -Raw -Encoding UTF8
$manifest.RevitAddIns.AddIn.Assembly = Join-Path $installedPayload 'TuMo.Revit.dll'
[void][IO.Directory]::CreateDirectory($installedPayload)
Get-ChildItem -LiteralPath $payload -File | Copy-Item -Destination $installedPayload -Force
$manifest.Save($addinPath)
Write-Output "TuMo for Revit $version installed to $addinFolder"
