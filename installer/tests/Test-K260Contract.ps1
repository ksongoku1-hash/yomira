[CmdletBinding()]
param(
    [Parameter()]
    [string]$RepositoryRoot,

    [Parameter()]
    [string]$K258BundleDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Assert-True {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (-not $Condition) {
        throw "Assertion failed: $Message"
    }
}

function Invoke-ExpectedFailure {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    try {
        & $Action
    } catch {
        return
    }

    throw "Expected failure did not occur: $Message"
}

$installerRoot = Join-Path $RepositoryRoot "installer"
$buildScript = Join-Path $installerRoot "build_installer.ps1"
$setupScript = Join-Path $installerRoot "Yomira.iss"
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("yomira-k260-contract-" + [guid]::NewGuid().ToString("N"))
$previousTestVersion = $env:YOMIRA_TEST_ISCC_VERSION
$previousTestOutput = $env:YOMIRA_TEST_OUTPUT_DIR
$previousTestLog = $env:YOMIRA_TEST_CALL_LOG
$previousTestAppVersion = $env:YOMIRA_TEST_APP_VERSION

New-Item -ItemType Directory -Path $tempRoot -ErrorAction Stop | Out-Null

try {
    Assert-True (Test-Path -LiteralPath $buildScript -PathType Leaf) "the reproducible build script exists"
    Assert-True (Test-Path -LiteralPath $setupScript -PathType Leaf) "the Inno Setup source exists"
    $versionSource = Get-Content -LiteralPath (Join-Path $RepositoryRoot "backend\app\version.py") -Raw
    Assert-True ($versionSource -match '(?m)^APP_VERSION\s*=\s*"(?<version>\d+\.\d+\.\d+)"\s*$') "backend version is semantic"
    $appVersion = $Matches.version

    $setup = Get-Content -LiteralPath $setupScript -Raw
    Assert-True ($setup -match '(?m)^DefaultDirName=\{localappdata\}\\Programs\\Yomira\s*$') "install target belongs to the current user"
    Assert-True ($setup -match '(?m)^PrivilegesRequired=lowest\s*$') "installation does not require an administrator"
    Assert-True ($setup -match 'Name: "\{autodesktop\}\\Yomira"') "the desktop shortcut exists"
    Assert-True ($setup -match '(?m)^\[Run\]\s*$') "setup offers to open Yomira"
    Assert-True ($setup -match '(?m)^ArchitecturesAllowed=x64os\s*$') "installer is limited to the x64 Windows platform declared by the SPEC"
    Assert-True ($setup -match '(?m)^ArchitecturesInstallIn64BitMode=x64os\s*$') "installer uses 64-bit install mode on x64 Windows"
    Assert-True ($setup -match '(?m)^AppMutex=Local\\Yomira\.V1\.Backend\s*$') "setup uses the backend mutex"
    Assert-True ($setup -match '(?m)^CloseApplications=no\s*$') "setup never forcibly closes the application"
    Assert-True ($setup -notmatch '(?im)^\s*\[(InstallDelete|UninstallDelete)\]\s*$') "setup has no install or uninstall deletion section"
    Assert-True ($setup -notmatch '(?i)\b(DelTree|DeleteFile|RemoveDir|Remove-Item)\b') "setup has no scripted deletion"
    $setupCode = ($setup -split "`r?`n" | Where-Object { $_ -notmatch '^\s*;' }) -join "`n"
    Assert-True ($setupCode -notmatch '(?i)YOMIRA_DATA_ROOT|library_root|YomiraData') "setup does not claim or manipulate mutable data roots"

    $filesMatch = [regex]::Match($setup, '(?ims)^\[Files\]\s*(?<body>.*?)(?=^\[|\z)')
    Assert-True $filesMatch.Success "setup declares its payload files"
    $fileEntries = @($filesMatch.Groups["body"].Value -split "`r?`n" | Where-Object { $_ -match '^\s*Source\s*:' })
    Assert-True ($fileEntries.Count -eq 1) "the application payload has exactly one recursive source mapping"
    Assert-True ($fileEntries[0] -match 'Source:\s*"\{#BundleDir\}\\\*";\s*DestDir:\s*"\{app\}"') "the only payload mapping is the K258 bundle into the application directory"

    $bundle = Join-Path $tempRoot "bundle with spaces"
    $dataRoot = Join-Path $tempRoot "user data root"
    $libraryRoot = Join-Path $tempRoot "portable library"
    $outputDirectory = Join-Path $tempRoot "installer output"
    $versionsDirectory = Join-Path $bundle "_internal\app\persistence\migrations\versions"
    New-Item -ItemType Directory -Path $versionsDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $bundle "_internal\app\web\assets") -Force | Out-Null
    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $libraryRoot -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $bundle "Yomira.exe") -Value "fixture executable"
    Set-Content -LiteralPath (Join-Path $bundle "_internal\alembic.ini") -Value "fixture alembic"
    Set-Content -LiteralPath (Join-Path $bundle "_internal\app\persistence\migrations\env.py") -Value "fixture migration env"
    Set-Content -LiteralPath (Join-Path $versionsDirectory "0001_fixture.py") -Value "fixture revision"
    Set-Content -LiteralPath (Join-Path $bundle "_internal\app\web\index.html") -Value "fixture frontend"
    $dataSentinel = Join-Path $dataRoot "database-sentinel.bin"
    $librarySentinel = Join-Path $libraryRoot "chapter.cbz"
    [System.IO.File]::WriteAllBytes($dataSentinel, [byte[]](1, 2, 3, 4))
    [System.IO.File]::WriteAllBytes($librarySentinel, [byte[]](5, 6, 7, 8))

    $compiler = Join-Path $tempRoot "ISCC fake.cmd"
    $callLog = Join-Path $tempRoot "compiler-calls.txt"
    $compilerBody = @'
@echo off
if "%~1"=="--version" (
  echo %YOMIRA_TEST_ISCC_VERSION%
  exit /b 0
)
>>"%YOMIRA_TEST_CALL_LOG%" echo %*
if not exist "%YOMIRA_TEST_OUTPUT_DIR%" mkdir "%YOMIRA_TEST_OUTPUT_DIR%"
>"%YOMIRA_TEST_OUTPUT_DIR%\Yomira-Setup-%YOMIRA_TEST_APP_VERSION%.exe" echo test-fixture-only
exit /b 0
'@
    Set-Content -LiteralPath $compiler -Value $compilerBody -Encoding Ascii

    $env:YOMIRA_TEST_ISCC_VERSION = "7.1.0"
    $env:YOMIRA_TEST_OUTPUT_DIR = $outputDirectory
    $env:YOMIRA_TEST_CALL_LOG = $callLog
    $env:YOMIRA_TEST_APP_VERSION = $appVersion
    & $buildScript -BundleDirectory $bundle -ISCCPath $compiler -OutputDirectory $outputDirectory
    Assert-True ($LASTEXITCODE -eq 0) "the build script accepts the pinned fake compiler and fixture bundle"
    Assert-True (Test-Path -LiteralPath (Join-Path $outputDirectory "Yomira-Setup-$appVersion.exe") -PathType Leaf) "the fake compiler wrote only its fixture artifact"
    Assert-True (([System.IO.File]::ReadAllBytes($dataSentinel) -join ",") -eq "1,2,3,4") "build left the external data-root fixture unchanged"
    Assert-True (([System.IO.File]::ReadAllBytes($librarySentinel) -join ",") -eq "5,6,7,8") "build left the independent library fixture unchanged"

    $callsBeforeInvalidOutput = (Get-Content -LiteralPath $callLog -Raw).Trim()
    Invoke-ExpectedFailure {
        & $buildScript -BundleDirectory $bundle -ISCCPath $compiler -OutputDirectory $outputDirectory
    } "an existing output directory must be refused"
    Assert-True ((Get-Content -LiteralPath $callLog -Raw).Trim() -eq $callsBeforeInvalidOutput) "existing output is rejected before compiling"

    $incompleteBundle = Join-Path $tempRoot "incomplete bundle"
    Copy-Item -LiteralPath $bundle -Destination $incompleteBundle -Recurse
    Remove-Item -LiteralPath (Join-Path $incompleteBundle "_internal\app\web\index.html")
    $incompleteOutput = Join-Path $tempRoot "incomplete output"
    $callsBeforeIncompleteBundle = (Get-Content -LiteralPath $callLog -Raw).Trim()
    Invoke-ExpectedFailure {
        & $buildScript -BundleDirectory $incompleteBundle -ISCCPath $compiler -OutputDirectory $incompleteOutput
    } "a bundle missing the frontend must be rejected"
    Assert-True (-not (Test-Path -LiteralPath $incompleteOutput)) "bundle preflight failure does not create an output directory"
    Assert-True ((Get-Content -LiteralPath $callLog -Raw).Trim() -eq $callsBeforeIncompleteBundle) "invalid bundle is rejected before compiling"

    $env:YOMIRA_TEST_ISCC_VERSION = "7.0.0"
    $versionMismatchOutput = Join-Path $tempRoot "version mismatch output"
    $callsBeforeVersionMismatch = (Get-Content -LiteralPath $callLog -Raw).Trim()
    Invoke-ExpectedFailure {
        & $buildScript -BundleDirectory $bundle -ISCCPath $compiler -OutputDirectory $versionMismatchOutput
    } "an unpinned Inno Setup compiler version must be rejected"
    Assert-True (-not (Test-Path -LiteralPath $versionMismatchOutput)) "compiler-version failure does not create output"
    Assert-True ((Get-Content -LiteralPath $callLog -Raw).Trim() -eq $callsBeforeVersionMismatch) "version mismatch is rejected before compiling"

    if (-not [string]::IsNullOrWhiteSpace($K258BundleDirectory)) {
        $realBundleOutput = Join-Path $tempRoot "verified K258 bundle output"
        $env:YOMIRA_TEST_ISCC_VERSION = "7.1.0"
        $env:YOMIRA_TEST_OUTPUT_DIR = $realBundleOutput
        & $buildScript -BundleDirectory $K258BundleDirectory -ISCCPath $compiler -OutputDirectory $realBundleOutput
        Assert-True ($LASTEXITCODE -eq 0) "the retained K258 bundle passes installer bundle preflight"
        Assert-True (Test-Path -LiteralPath (Join-Path $realBundleOutput "Yomira-Setup-$appVersion.exe") -PathType Leaf) "the mock compiler emitted a temp-only artifact for the retained K258 bundle"
    }

    Write-Output "K260 static/contract tests passed (temporary fixture; no Inno Setup install was executed)."
} finally {
    if ($null -eq $previousTestVersion) { Remove-Item Env:YOMIRA_TEST_ISCC_VERSION -ErrorAction SilentlyContinue }
    else { $env:YOMIRA_TEST_ISCC_VERSION = $previousTestVersion }
    if ($null -eq $previousTestOutput) { Remove-Item Env:YOMIRA_TEST_OUTPUT_DIR -ErrorAction SilentlyContinue }
    else { $env:YOMIRA_TEST_OUTPUT_DIR = $previousTestOutput }
    if ($null -eq $previousTestLog) { Remove-Item Env:YOMIRA_TEST_CALL_LOG -ErrorAction SilentlyContinue }
    else { $env:YOMIRA_TEST_CALL_LOG = $previousTestLog }
    if ($null -eq $previousTestAppVersion) { Remove-Item Env:YOMIRA_TEST_APP_VERSION -ErrorAction SilentlyContinue }
    else { $env:YOMIRA_TEST_APP_VERSION = $previousTestAppVersion }
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
