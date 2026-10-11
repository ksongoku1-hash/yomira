$ErrorActionPreference = 'Stop'
$application = Join-Path $PSScriptRoot 'Yomira.exe'
if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
    $application = Join-Path $PSScriptRoot 'Yomira/Yomira.exe'
}
if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
    throw 'Faltan archivos de Yomira. Extrae todo el ZIP antes de abrirla.'
}
Push-Location $PSScriptRoot
try {
    & $application
    if ($LASTEXITCODE -ne 0) { throw "Yomira se cerro con un error ($LASTEXITCODE)." }
} finally { Pop-Location }
