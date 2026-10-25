[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true)][string]$MotorDistribution,
    [Parameter(Mandatory = $true)][string]$ISCCPath,
    [string]$Python = '',
    [string]$NpmCmd = ''
)
$ErrorActionPreference = 'Stop'
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $Python) { $Python = Join-Path $repository 'runtime/python-3.14.7/python.exe' }
if (-not $NpmCmd) { $NpmCmd = Join-Path $repository 'runtime/node-24.21.0/npm.cmd' }
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
$novelcool = Join-Path $repository 'third_party/fmd2/lua/templates/NovelCool.lua'
if ((Get-FileHash -LiteralPath $novelcool -Algorithm SHA256).Hash.ToLowerInvariant() -ne $provenance.novelcool_source_sha256) {
    throw 'The NovelCool module does not match the verified source.'
}
foreach ($moduleName in @($provenance.additional_modules_sha256.PSObject.Properties.Name)) {
    if ($moduleName -notmatch "^[A-Za-z][A-Za-z0-9]{0,79}$") { throw "Invalid module name." }
    $moduleFile = Join-Path $repository "third_party/fmd2/lua/modules/$moduleName.lua"
    if ((Get-FileHash -LiteralPath $moduleFile -Algorithm SHA256).Hash.ToLowerInvariant() -ne $provenance.additional_modules_sha256.$moduleName) { throw "The $moduleName module does not match the verified source." }
}
foreach ($template in @($provenance.supporting_templates_sha256.PSObject.Properties)) {
    if ($template.Name -notmatch '^[A-Za-z][A-Za-z0-9]{0,79}$') { throw 'Invalid template name.' }
    if ((Get-FileHash -LiteralPath (Join-Path $repository "third_party/fmd2/lua/templates/$($template.Name).lua") -Algorithm SHA256).Hash.ToLowerInvariant() -ne $template.Value) { throw 'The shared template does not match the verified source.' }
}
foreach ($catalog in @($provenance.public_catalogs_sha256.PSObject.Properties)) {
    if ($catalog.Name -notmatch '^[a-f0-9]{32}$') { throw 'Invalid catalog id.' }
    if ((Get-FileHash -LiteralPath (Join-Path $repository "third_party/fmd2/catalogs/$($catalog.Name).db") -Algorithm SHA256).Hash.ToLowerInvariant() -ne $catalog.Value) { throw 'The public catalog does not match the verified source.' }
}
& $NpmCmd --prefix (Join-Path $repository 'frontend') run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
New-Item -ItemType Directory -Path $output | Out-Null
& (Join-Path $repository 'backend/build_windows.ps1') -OutputDirectory (Join-Path $output 'bundle') -Python $Python
$bundle = Join-Path $output 'bundle/Yomira'
foreach ($folder in @('Motor', 'Licencias')) {
    Copy-Item -LiteralPath (Join-Path $distribution $folder) -Destination $bundle -Recurse
}
Copy-Item -LiteralPath $novelcool -Destination (Join-Path $bundle 'Motor/lua/templates/NovelCool.lua')
foreach ($moduleName in @($provenance.additional_modules_sha256.PSObject.Properties.Name)) {
    if ($moduleName -notmatch "^[A-Za-z][A-Za-z0-9]{0,79}$") { throw "Invalid module name." }
    Copy-Item -LiteralPath (Join-Path $repository "third_party/fmd2/lua/modules/$moduleName.lua") -Destination (Join-Path $bundle "Motor/lua/modules/$moduleName.lua")
}
foreach ($template in @($provenance.supporting_templates_sha256.PSObject.Properties)) {
    Copy-Item -LiteralPath (Join-Path $repository "third_party/fmd2/lua/templates/$($template.Name).lua") -Destination (Join-Path $bundle "Motor/lua/templates/$($template.Name).lua")
}
New-Item -ItemType Directory -Path (Join-Path $bundle 'Motor/data') -Force | Out-Null
foreach ($catalog in @($provenance.public_catalogs_sha256.PSObject.Properties)) {
    Copy-Item -LiteralPath (Join-Path $repository "third_party/fmd2/catalogs/$($catalog.Name).db") -Destination (Join-Path $bundle "Motor/data/$($catalog.Name).db")
}
foreach ($file in @('LEEME.txt', 'Abrir Yomira.cmd', 'Iniciar Yomira.ps1')) {
    Copy-Item -LiteralPath (Join-Path $repository "installer/$file") -Destination $bundle
}
& (Join-Path $repository 'installer/build_installer.ps1') -BundleDirectory $bundle -ISCCPath $ISCCPath -OutputDirectory (Join-Path $output 'installer')
$versionSource = Get-Content (Join-Path $repository 'backend/app/version.py') -Raw
if ($versionSource -notmatch 'APP_VERSION = "([0-9.]+)"') { throw 'Missing app version.' }
$version = $Matches[1]
$releaseZip = Join-Path $output "Yomira-Windows-x64-$version.zip"
Compress-Archive -Path (Join-Path $bundle '*') -DestinationPath $releaseZip
$installerFiles = @(Get-ChildItem -LiteralPath (Join-Path $output 'installer') -File -Recurse -Filter '*.exe' -ErrorAction SilentlyContinue)
if (-not (Test-Path -LiteralPath $releaseZip -PathType Leaf) -or (Get-Item -LiteralPath $releaseZip).Length -eq 0 -or $installerFiles.Count -eq 0) {
    throw 'The release ZIP or installer is missing; no version backup was created.'
}
& (Join-Path $repository 'scripts/backup-release.ps1') -ReleaseOutput $output -Version $version
Write-Output "Release files: $output"

