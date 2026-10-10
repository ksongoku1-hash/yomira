[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$NodeExe
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendRoot = Join-Path $projectRoot "backend"
$frontendRoot = Join-Path $projectRoot "frontend"
$viteEntry = Join-Path $frontendRoot "node_modules/vite/bin/vite.js"
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("yomira-v1-smoke-" + [guid]::NewGuid().ToString("N"))
$apiProcess = $null
$frontendProcess = $null
$completed = $false
$oldPythonPath = $env:PYTHONPATH
$oldProxyTarget = $env:YOMIRA_API_PROXY_TARGET
$oldAppEnv = $env:YOMIRA_APP_ENV
$oldApiHost = $env:YOMIRA_API_HOST
$oldApiPort = $env:YOMIRA_API_PORT
$oldLogLevel = $env:YOMIRA_LOG_LEVEL
$oldDataRoot = $env:YOMIRA_DATA_ROOT

function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Read-SmokeLog {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return (Get-Content -Raw -LiteralPath $Path)
    }
    return "(sin salida)"
}

function Stop-SmokeProcess {
    param([System.Diagnostics.Process]$Process)
    if ($null -eq $Process) {
        return
    }

    $Process.Refresh()
    if (-not $Process.HasExited) {
        $Process.Kill()
        $Process.WaitForExit(5000) | Out-Null
        $Process.Refresh()
    }
    if (-not $Process.HasExited) {
        throw ("No se pudo cerrar el proceso de smoke test {0}." -f $Process.Id)
    }
}

try {
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    $apiPort = Get-FreeLoopbackPort
    $frontendPort = Get-FreeLoopbackPort
    while ($frontendPort -eq $apiPort) {
        $frontendPort = Get-FreeLoopbackPort
    }
    $apiOut = Join-Path $tempRoot "api.stdout.log"
    $apiErr = Join-Path $tempRoot "api.stderr.log"
    $frontendOut = Join-Path $tempRoot "frontend.stdout.log"
    $frontendErr = Join-Path $tempRoot "frontend.stderr.log"

    $env:PYTHONPATH = $backendRoot
    $env:YOMIRA_API_PROXY_TARGET = "http://127.0.0.1:$apiPort"
    $env:YOMIRA_APP_ENV = "test"
    $env:YOMIRA_API_HOST = "127.0.0.1"
    $env:YOMIRA_API_PORT = "$apiPort"
    $env:YOMIRA_LOG_LEVEL = "INFO"
    $env:YOMIRA_DATA_ROOT = Join-Path $tempRoot "YomiraData"
    $apiArguments = @(
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "$apiPort",
        "--log-level",
        "warning"
    )
    $apiStart = @{
        FilePath = $PythonExe
        ArgumentList = $apiArguments
        WorkingDirectory = $backendRoot
        WindowStyle = "Hidden"
        PassThru = $true
        RedirectStandardOutput = $apiOut
        RedirectStandardError = $apiErr
    }
    $apiProcess = Start-Process @apiStart

    $apiBaseDirect = "http://127.0.0.1:$apiPort"
    $apiDeadline = [DateTime]::UtcNow.AddSeconds(20)
    $apiReady = $false
    while ([DateTime]::UtcNow -lt $apiDeadline) {
        $apiProcess.Refresh()
        if ($apiProcess.HasExited) {
            $apiLogs = "stdout: {0}; stderr: {1}" -f (Read-SmokeLog $apiOut), (Read-SmokeLog $apiErr)
            throw ("FastAPI terminó al iniciar. {0}" -f $apiLogs)
        }
        try {
            $directLiveness = Invoke-RestMethod -Uri "$apiBaseDirect/api/v1/system/liveness" -TimeoutSec 2
            if ($directLiveness.status -eq "alive") {
                $apiReady = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $apiReady) {
        $apiError = Read-SmokeLog $apiErr
        throw ("FastAPI no respondió dentro de 20 segundos. stderr: {0}" -f $apiError)
    }

    $viteArguments = '"{0}" --host 127.0.0.1 --port {1} --strictPort' -f $viteEntry, $frontendPort
    $frontendStart = @{
        FilePath = $NodeExe
        ArgumentList = $viteArguments
        WorkingDirectory = $frontendRoot
        WindowStyle = "Hidden"
        PassThru = $true
        RedirectStandardOutput = $frontendOut
        RedirectStandardError = $frontendErr
    }
    $frontendProcess = Start-Process @frontendStart

    $apiBase = "http://127.0.0.1:$frontendPort"
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        $apiProcess.Refresh()
        $frontendProcess.Refresh()
        if ($apiProcess.HasExited) {
            $apiLogs = "stdout: {0}; stderr: {1}" -f (Read-SmokeLog $apiOut), (Read-SmokeLog $apiErr)
            throw ("FastAPI terminó durante el smoke test. {0}" -f $apiLogs)
        }
        if ($frontendProcess.HasExited) {
            $frontendLogs = "stdout: {0}; stderr: {1}" -f (Read-SmokeLog $frontendOut), (Read-SmokeLog $frontendErr)
            throw ("Vite terminó al iniciar. {0}" -f $frontendLogs)
        }
        try {
            $liveness = Invoke-RestMethod -Uri "$apiBase/api/v1/system/liveness" -TimeoutSec 2
            if ($liveness.status -eq "alive") {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $ready) {
        $apiError = Read-SmokeLog $apiErr
        $frontendError = Read-SmokeLog $frontendErr
        $startupError = "El proxy Vite no conectó con FastAPI dentro de 20 segundos. API stderr: {0}; Vite stderr: {1}" -f $apiError, $frontendError
        throw $startupError
    }

    $homeResponse = Invoke-WebRequest -Uri "$apiBase/" -TimeoutSec 5
    if ($homeResponse.StatusCode -ne 200 -or $homeResponse.Content -notmatch 'id="root"') {
        throw "La ruta raíz del frontend no devolvió el documento de la aplicación."
    }

    $libraryResponse = Invoke-WebRequest -Uri "$apiBase/library" -TimeoutSec 5
    if ($libraryResponse.StatusCode -ne 200 -or $libraryResponse.Content -notmatch 'id="root"') {
        throw "La ruta de biblioteca no tuvo fallback al documento SPA."
    }

    $livenessResponse = Invoke-WebRequest -Uri "$apiBase/api/v1/system/liveness" -TimeoutSec 5
    $liveness = $livenessResponse.Content | ConvertFrom-Json
    if ($liveness.status -ne "alive") {
        throw "Liveness no devolvió el estado esperado."
    }

    $traceId = $livenessResponse.Headers["X-Trace-ID"]
    $parsedTraceId = [Guid]::Empty
    if (-not [Guid]::TryParse($traceId, [ref]$parsedTraceId)) {
        throw "La respuesta de liveness no incluyó un X-Trace-ID válido."
    }

    $readiness = Invoke-RestMethod -Uri "$apiBase/api/v1/system/readiness" -TimeoutSec 5
    if ($readiness.status -ne "ready") {
        throw "El servidor respondió antes de completar su startup."
    }

    $libraryApi = Invoke-RestMethod -Uri "$apiBase/api/v1/library" -TimeoutSec 10
    if ($libraryApi.meta.total -ne 0 -or @($libraryApi.data).Count -ne 0) {
        throw "La API de biblioteca no devolvió la base local recién inicializada."
    }

    $version = Invoke-RestMethod -Uri "$apiBase/api/v1/system/version" -TimeoutSec 5
    if ($version.api_version -ne 1 -or [string]::IsNullOrWhiteSpace($version.app_version)) {
        throw "El proxy API devolvió un contrato de versión inesperado."
    }

    $completed = $true
    Write-Host ("Smoke PASS: root, /library, API de biblioteca y endpoints de sistema vía loopback 127.0.0.1:{0}." -f $frontendPort)
}
finally {
    Stop-SmokeProcess $frontendProcess
    Stop-SmokeProcess $apiProcess

    if ($null -eq $oldPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $oldPythonPath
    }
    if ($null -eq $oldProxyTarget) {
        Remove-Item Env:YOMIRA_API_PROXY_TARGET -ErrorAction SilentlyContinue
    }
    else {
        $env:YOMIRA_API_PROXY_TARGET = $oldProxyTarget
    }
    if ($null -eq $oldAppEnv) {
        Remove-Item Env:YOMIRA_APP_ENV -ErrorAction SilentlyContinue
    }
    else {
        $env:YOMIRA_APP_ENV = $oldAppEnv
    }
    if ($null -eq $oldApiHost) {
        Remove-Item Env:YOMIRA_API_HOST -ErrorAction SilentlyContinue
    }
    else {
        $env:YOMIRA_API_HOST = $oldApiHost
    }
    if ($null -eq $oldApiPort) {
        Remove-Item Env:YOMIRA_API_PORT -ErrorAction SilentlyContinue
    }
    else {
        $env:YOMIRA_API_PORT = $oldApiPort
    }
    if ($null -eq $oldLogLevel) {
        Remove-Item Env:YOMIRA_LOG_LEVEL -ErrorAction SilentlyContinue
    }
    else {
        $env:YOMIRA_LOG_LEVEL = $oldLogLevel
    }
    if ($null -eq $oldDataRoot) {
        Remove-Item Env:YOMIRA_DATA_ROOT -ErrorAction SilentlyContinue
    }
    else {
        $env:YOMIRA_DATA_ROOT = $oldDataRoot
    }

    if ($completed -and (Test-Path -LiteralPath $tempRoot)) {
        $fullTempRoot = [System.IO.Path]::GetFullPath($tempRoot)
        $allowedTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if (-not $allowedTempRoot.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
            $allowedTempRoot += [System.IO.Path]::DirectorySeparatorChar
        }
        if ($fullTempRoot.StartsWith($allowedTempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $fullTempRoot -Recurse -Force
        }
    }
    elseif (Test-Path -LiteralPath $tempRoot) {
        Write-Warning ("Logs de smoke conservados para diagnóstico: {0}" -f $tempRoot)
    }
}
