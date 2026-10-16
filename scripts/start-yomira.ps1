[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$FmdExecutable = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$backend = Join-Path $projectRoot 'backend'
if (-not $Python) {
    $Python = Join-Path $projectRoot 'runtime/python-3.14.7/python.exe'
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        $Python = Join-Path $backend '.venv/Scripts/python.exe'
    }
}
if (-not $env:YOMIRA_DATA_ROOT) { $env:YOMIRA_DATA_ROOT = Join-Path $projectRoot 'user-data/AppData/Yomira/YomiraData' }
if (-not $env:YOMIRA_LIBRARY_ROOT) { $env:YOMIRA_LIBRARY_ROOT = Join-Path $projectRoot 'user-data/libraries/Mangas' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Crea el entorno Python siguiendo README.md o indica -Python.' }
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
if ($FmdExecutable) {
    if (-not (Test-Path -LiteralPath $FmdExecutable -PathType Leaf)) { throw 'No se encontró el ejecutable de FMD2.' }
    $env:YOMIRA_FMD2_EXECUTABLE = (Resolve-Path -LiteralPath $FmdExecutable).Path
}
$env:YOMIRA_APP_ENV = 'prod'
Push-Location $backend
try {
    & $pythonPath -m app.main
    if ($LASTEXITCODE -ne 0) { throw "Yomira terminó con código $LASTEXITCODE." }
} finally { Pop-Location }
