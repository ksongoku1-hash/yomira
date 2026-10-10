[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$BundleDirectory,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ISCCPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$pinnedISCCVersion = "7.1.0"
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$setupScript = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "Yomira.iss"))
$versionPath = Join-Path $repositoryRoot "backend\app\version.py"

if (-not (Test-Path -LiteralPath $BundleDirectory -PathType Container)) {
    throw "K258 bundle directory does not exist: $BundleDirectory"
}
$bundlePath = (Resolve-Path -LiteralPath $BundleDirectory -ErrorAction Stop).Path

if (-not (Test-Path -LiteralPath $ISCCPath -PathType Leaf)) {
    throw "ISCC.exe was not found at the supplied path. Install the pinned compiler separately, then retry."
}
$iscc = (Resolve-Path -LiteralPath $ISCCPath -ErrorAction Stop).Path

$requiredBundleFiles = @(
    (Join-Path $bundlePath "Yomira.exe"),
    (Join-Path $bundlePath "_internal\alembic.ini"),
    (Join-Path $bundlePath "_internal\app\persistence\migrations\env.py"),
    (Join-Path $bundlePath "_internal\app\web\index.html")
)
foreach ($requiredFile in $requiredBundleFiles) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "K258 bundle is incomplete; required file is missing: $requiredFile"
    }
}

$requiredBundleDirectories = @(
    (Join-Path $bundlePath "_internal\app\web\assets"),
    (Join-Path $bundlePath "_internal\app\persistence\migrations\versions")
)
foreach ($requiredDirectory in $requiredBundleDirectories) {
    if (-not (Test-Path -LiteralPath $requiredDirectory -PathType Container)) {
        throw "K258 bundle is incomplete; required directory is missing: $requiredDirectory"
    }
}
$migrationVersions = @(Get-ChildItem -LiteralPath $requiredBundleDirectories[1] -Filter "*.py" -File)
if ($migrationVersions.Count -eq 0) {
    throw "K258 bundle has no Alembic migration revisions."
}

if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
    throw "Backend version source is missing: $versionPath"
}
$versionText = Get-Content -LiteralPath $versionPath -Raw
if ($versionText -notmatch '(?m)^APP_VERSION\s*=\s*"(?<version>\d+\.\d+\.\d+)"\s*$') {
    throw "backend/app/version.py must declare a semantic APP_VERSION."
}
$appVersion = $Matches.version

$outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $outputPath) {
    throw "Output path already exists; choose a new, empty directory. Existing output is never overwritten."
}
$outputParent = [System.IO.Directory]::GetParent($outputPath)
if ($null -eq $outputParent -or -not (Test-Path -LiteralPath $outputParent.FullName -PathType Container)) {
    throw "The parent of the output path must already exist."
}
$repositoryPrefix = $repositoryRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if ($outputPath.Equals($repositoryRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    $outputPath.StartsWith($repositoryPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Installer output must stay outside the source checkout."
}

if (-not (Test-Path -LiteralPath $setupScript -PathType Leaf)) {
    throw "Inno Setup source is missing: $setupScript"
}
$versionOutput = @(& $iscc "--version" 2>&1)
$versionExitCode = $LASTEXITCODE
$reportedVersion = ($versionOutput -join "`n").Trim()
if ($versionExitCode -ne 0 -or $reportedVersion -notmatch "(^|\s)$([regex]::Escape($pinnedISCCVersion))($|\s)") {
    throw "Expected ISCC $pinnedISCCVersion; compiler reported '$reportedVersion' (exit $versionExitCode)."
}

$bundleEnvironmentWasSet = Test-Path -LiteralPath "Env:YOMIRA_BUNDLE_DIR"
$previousBundleEnvironment = [System.Environment]::GetEnvironmentVariable("YOMIRA_BUNDLE_DIR", "Process")
try {
    $env:YOMIRA_BUNDLE_DIR = $bundlePath
    $appVersionDefinition = "--define=AppVersion=$appVersion"
    & $iscc "--output-dir=$outputPath" $appVersionDefinition $setupScript
    $compileExitCode = $LASTEXITCODE
    if ($compileExitCode -ne 0) {
        throw "ISCC failed with exit code $compileExitCode. Any partial compiler output was preserved at $outputPath."
    }
} finally {
    if ($bundleEnvironmentWasSet) {
        [System.Environment]::SetEnvironmentVariable("YOMIRA_BUNDLE_DIR", $previousBundleEnvironment, "Process")
    } else {
        Remove-Item -LiteralPath "Env:YOMIRA_BUNDLE_DIR" -ErrorAction SilentlyContinue
    }
}

$installerPath = Join-Path $outputPath "Yomira-Setup-$appVersion.exe"
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "ISCC reported success but the expected artifact is missing: $installerPath"
}
if ((Get-Item -LiteralPath $installerPath).Length -le 0) {
    throw "ISCC produced an empty installer artifact: $installerPath"
}

Write-Output "Built: $installerPath"
