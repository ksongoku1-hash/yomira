[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true)][string]$MotorDistribution,
    [Parameter(Mandatory = $true)][string]$ISCCPath,
    [string]$Python = 'python',
    [string]$NpmCmd = 'npm.cmd'
)
$ErrorActionPreference = 'Stop'
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$output = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $output) { throw 'Choose a new output directory.' }
if ($output.StartsWith($repository + '\', [StringComparison]::OrdinalIgnoreCase) -or $output -eq $repository) {
    throw 'Release output must stay outside the source checkout.'
}
$distribution = (Resolve-Path -LiteralPath $MotorDistribution).Path
$motor = Join-Path $distribution 'Motor/fmd.exe'
$provenance = Get-Content (Join-Path $repository 'third_party/fmd2/upstream.json') -Raw | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $motor -Algorithm SHA256).Hash.ToLowerInvariant() -ne $provenance.reference_executable_sha256) {
    throw 'The FMD executable does not match the verified release.'
}
foreach ($required in @('Motor/licenses', 'Licencias')) {
    if (-not (Test-Path -LiteralPath (Join-Path $distribution $required) -PathType Container)) {
        throw "Missing redistribution licenses: $required"
    }
}
& $NpmCmd --prefix (Join-Path $repository 'frontend') run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
New-Item -ItemType Directory -Path $output | Out-Null
& (Join-Path $repository 'backend/build_windows.ps1') -OutputDirectory (Join-Path $output 'bundle') -Python $Python
$bundle = Join-Path $output 'bundle/Yomira'
foreach ($folder in @('Motor', 'Licencias')) {
    Copy-Item -LiteralPath (Join-Path $distribution $folder) -Destination $bundle -Recurse
}
foreach ($file in @('LEEME.txt', 'Abrir Yomira.cmd', 'Iniciar Yomira.ps1')) {
    Copy-Item -LiteralPath (Join-Path $repository "installer/$file") -Destination $bundle
}
& (Join-Path $repository 'installer/build_installer.ps1') -BundleDirectory $bundle -ISCCPath $ISCCPath -OutputDirectory (Join-Path $output 'installer')
$versionSource = Get-Content (Join-Path $repository 'backend/app/version.py') -Raw
if ($versionSource -notmatch 'APP_VERSION = "([0-9.]+)"') { throw 'Missing app version.' }
$version = $Matches[1]
Compress-Archive -Path (Join-Path $bundle '*') -DestinationPath (Join-Path $output "Yomira-Windows-x64-$version.zip")
Write-Output "Release files: $output"
