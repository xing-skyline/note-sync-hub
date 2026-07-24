$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
$iconPath = Join-Path $projectRoot 'assets\app-icon.ico'
$iconImagePath = Join-Path $projectRoot 'assets\app-icon.png'

python -c "import PyInstaller, requests, yaml" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "缺少构建依赖。请先运行：python -m pip install -e '.[build]'"
}

Write-Host 'Running tests...'
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {
    throw 'Tests failed; executable was not built.'
}

Write-Host 'Building windowed executable...'
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name NoteSyncHub `
    --icon $iconPath `
    --add-data "$iconImagePath;assets" `
    --collect-all yaml `
    --specpath build `
    --workpath build\pyinstaller `
    --distpath dist `
    NoteSyncHub.pyw

if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller build failed.'
}

Write-Host ''
Write-Host "Build complete: $projectRoot\dist\NoteSyncHub.exe"
