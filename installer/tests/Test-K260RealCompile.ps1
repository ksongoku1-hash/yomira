[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$BundleDirectory,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ISCCPath,

    [Parameter()]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$buildScript = Join-Path $repositoryRoot "installer\build_installer.ps1"
$versionSource = Get-Content -LiteralPath (Join-Path $repositoryRoot "backend\app\version.py") -Raw
if ($versionSource -notmatch '(?m)^APP_VERSION\s*=\s*"(?<version>\d+\.\d+\.\d+)"\s*$') {
    throw "Backend version is not semantic; real compile test cannot determine the expected artifact name."
}
$appVersion = $Matches.version

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("yomira-k260-real-compile-" + [guid]::NewGuid().ToString("N"))
}
$outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $outputPath) {
    throw "Output path already exists; the real compiler test never overwrites artifacts: $outputPath"
}

try {
    & $buildScript -BundleDirectory $BundleDirectory -ISCCPath $ISCCPath -OutputDirectory $outputPath
} catch {
    Write-Error "Real Inno Setup compile failed. Any partial artifacts are preserved at '$outputPath'."
    throw
}

$expectedInstaller = Join-Path $outputPath "Yomira-Setup-$appVersion.exe"
if (-not (Test-Path -LiteralPath $expectedInstaller -PathType Leaf)) {
    throw "Real compiler reported success but the expected installer is missing: $expectedInstaller"
}
if ((Get-Item -LiteralPath $expectedInstaller).Length -le 0) {
    throw "Real compiler produced an empty installer: $expectedInstaller"
}

Write-Output "K260 real Inno Setup compile passed: $expectedInstaller"
