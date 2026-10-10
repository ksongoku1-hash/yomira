param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$application = Join-Path $PSScriptRoot 'Yomira/Yomira.exe'
$motor = Join-Path $PSScriptRoot 'Motor/fmd.exe'
foreach ($required in @($application, $motor)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw 'Faltan archivos de Yomira. Extrae todo el ZIP antes de abrirla.'
    }
}
$env:YOMIRA_APP_ENV = 'prod'
$env:YOMIRA_API_HOST = '127.0.0.1'
$env:YOMIRA_FMD2_EXECUTABLE = [IO.Path]::GetFullPath($motor)
if (-not $env:YOMIRA_API_PORT) { $env:YOMIRA_API_PORT = '8000' }
$port = 0
if (-not [int]::TryParse($env:YOMIRA_API_PORT, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
    throw 'El puerto de Yomira no es valido.'
}
if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
    throw "El puerto $port ya esta ocupado. Cierra la otra instancia antes de abrir Yomira."
}
$url = "http://127.0.0.1:$port"
$browserJob = $null
if (-not $NoBrowser) {
    $browserJob = Start-Job -ArgumentList $url -ScriptBlock {
        param($origin)
        $deadline = (Get-Date).AddSeconds(90)
        while ((Get-Date) -lt $deadline) {
            try {
                $ready = Invoke-RestMethod "$origin/api/v1/system/readiness" -TimeoutSec 2
                if ($ready.status -eq 'ready') {
                    Start-Process "$origin/library"
                    return
                }
            } catch { }
            Start-Sleep -Milliseconds 300
        }
        Write-Warning "No se pudo abrir el navegador. Revisa los mensajes de Yomira."
    }
}
Write-Host 'Yomira esta iniciando. Puedes minimizar esta ventana.'
Write-Host "La pagina estara en $url/library"
Write-Host 'Para cerrar Yomira, vuelve a esta ventana y pulsa Ctrl+C.'
Push-Location $PSScriptRoot
try {
    & $application
    if ($LASTEXITCODE -ne 0) { throw "Yomira se cerro con un error ($LASTEXITCODE)." }
} finally {
    Pop-Location
    if ($null -ne $browserJob) {
        Stop-Job $browserJob
        Receive-Job $browserJob
        Remove-Job $browserJob
    }
}
