$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
$iconPath = Join-Path $projectRoot 'assets\app-icon.ico'
$iconImagePath = Join-Path $projectRoot 'assets\app-icon.png'
$versionGeneratorPath = Join-Path $projectRoot 'scripts\generate_windows_version_info.py'
$versionInfoPath = Join-Path $projectRoot 'build\windows_version_info.txt'
$exePath = Join-Path $projectRoot 'dist\NoteSyncHub.exe'

python -c "import PyInstaller, requests, yaml" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "缺少构建依赖。请先运行：python -m pip install -e '.[build]'"
}

foreach ($requiredPath in @($iconPath, $iconImagePath, $versionGeneratorPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required build file not found: $requiredPath"
    }
}

Write-Host 'Running tests...'
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {
    throw 'Tests failed; executable was not built.'
}

Write-Host 'Generating Windows version resources from pyproject.toml...'
$versionMetadataJson = python $versionGeneratorPath `
    --pyproject (Join-Path $projectRoot 'pyproject.toml') `
    --output $versionInfoPath
if ($LASTEXITCODE -ne 0) {
    throw 'Windows version resource generation failed.'
}

try {
    $expectedVersionInfo = $versionMetadataJson | ConvertFrom-Json
}
catch {
    throw "Version resource generator returned invalid metadata: $_"
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
    --version-file $versionInfoPath `
    --collect-all yaml `
    --specpath build `
    --workpath build\pyinstaller `
    --distpath dist `
    NoteSyncHub.pyw

if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller build failed.'
}

if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "PyInstaller reported success but the executable was not found: $exePath"
}

$actualVersionInfo = (Get-Item -LiteralPath $exePath).VersionInfo
$requiredVersionFields = [ordered]@{
    ProductName      = $expectedVersionInfo.product_name
    FileDescription  = $expectedVersionInfo.file_description
    CompanyName      = $expectedVersionInfo.company_name
    FileVersion      = $expectedVersionInfo.file_version
    ProductVersion   = $expectedVersionInfo.product_version
    OriginalFilename = $expectedVersionInfo.original_filename
    LegalCopyright   = $expectedVersionInfo.legal_copyright
}

foreach ($field in $requiredVersionFields.GetEnumerator()) {
    $actualValue = $actualVersionInfo.($field.Key)
    if ($actualValue -ne $field.Value) {
        throw (
            "EXE version validation failed for {0}: expected '{1}', got '{2}'" -f `
                $field.Key, $field.Value, $actualValue
        )
    }
}

Write-Host ''
Write-Host 'Validated Windows version resources:'
$actualVersionInfo |
    Format-List FileDescription, ProductName, CompanyName, FileVersion,
        ProductVersion, OriginalFilename, LegalCopyright
Write-Host "Build complete: $exePath"
