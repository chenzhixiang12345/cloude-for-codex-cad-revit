param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    & $Python -m PyInstaller --noconfirm --clean --onefile --name tumo-engine `
        --specpath build --collect-data ezdxf --collect-data tumo_engine `
        engine/tumo_engine/__main__.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    & ./dist/tumo-engine.exe --help
    if ($LASTEXITCODE -ne 0) { throw 'Engine executable smoke test failed.' }
}
finally {
    Pop-Location
}
