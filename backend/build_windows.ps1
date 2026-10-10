[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,

    [Parameter()]
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$backendRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $backendRoot ".."))
$outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)
$runtimePin = Get-Content -LiteralPath (Join-Path $repositoryRoot "runtime-versions.json") -Raw |
    ConvertFrom-Json
$expectedPython = [string]$runtimePin.python.version
$actualPython = (& $Python -c "import platform; print(platform.python_version())").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not query the selected Python runtime."
}
if ($actualPython -ne $expectedPython) {
    throw "Build requires pinned CPython $expectedPython; received $actualPython."
}

$expectedPyInstaller = (Select-String -LiteralPath (Join-Path $backendRoot "requirements-package.in") `
    -Pattern '^PyInstaller==([0-9.]+)$').Matches.Groups[1].Value
if (-not $expectedPyInstaller) {
    throw "The PyInstaller version pin is missing."
}
$actualPyInstaller = (& $Python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed in the selected Python environment."
}
if ($actualPyInstaller -ne $expectedPyInstaller) {
    throw "Build requires PyInstaller $expectedPyInstaller; received $actualPyInstaller."
}

$frontendIndex = Join-Path $repositoryRoot "frontend/dist/index.html"
$frontendAssets = Join-Path $repositoryRoot "frontend/dist/assets"
if (-not (Test-Path -LiteralPath $frontendIndex -PathType Leaf) -or
    -not (Test-Path -LiteralPath $frontendAssets -PathType Container)) {
    throw "Build the Vite frontend before packaging the backend."
}

if (Test-Path -LiteralPath $outputPath) {
    throw "Output path already exists; choose a new, empty build destination."
}
$outputParent = [System.IO.Directory]::GetParent($outputPath)
if ($null -eq $outputParent -or -not (Test-Path -LiteralPath $outputParent.FullName -PathType Container)) {
    throw "The parent of the output path must already exist."
}
if ($outputPath.StartsWith($repositoryRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Build output must stay outside the source checkout."
}

$workPath = Join-Path ([System.IO.Path]::GetTempPath()) ("yomira-pyinstaller-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $workPath -ErrorAction Stop | Out-Null

Push-Location $backendRoot
try {
    & $Python -m PyInstaller `
        --noconfirm `
        --distpath $outputPath `
        --workpath $workPath `
        (Join-Path $backendRoot "yomira.spec")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE. Work files were preserved at $workPath."
    }
} finally {
    Pop-Location
}

$exePath = Join-Path $outputPath "Yomira/Yomira.exe"
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "PyInstaller completed without producing Yomira.exe. Work files were preserved at $workPath."
}
Write-Output "Built: $exePath"
Write-Output "PyInstaller work files: $workPath"
