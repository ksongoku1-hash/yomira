[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$NodeExe = "",
    [string]$NpmCmd = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimePins = Get-Content -Raw -LiteralPath (Join-Path $projectRoot "runtime-versions.json") |
    ConvertFrom-Json

function Resolve-Executable {
    param(
        [string]$Candidate,
        [string[]]$CommandNames
    )

    if ($Candidate) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }

        $resolved = Get-Command -Name $Candidate -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($resolved) {
            return $resolved.Path
        }
        throw "No se encontró el ejecutable solicitado: $Candidate"
    }

    foreach ($commandName in $CommandNames) {
        $resolved = Get-Command -Name $commandName -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($resolved) {
            return $resolved.Path
        }
    }
    $joinedNames = $CommandNames -join ", "
    throw "No se encontró ninguno de estos ejecutables: $joinedNames"
}

function Invoke-Check {
    param(
        [string]$Label,
        [string]$Executable,
        [string[]]$Arguments
    )

    Write-Host ""
    Write-Host ("[{0}]" -f $Label)
    & $Executable @Arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw ("{0} falló con código {1}." -f $Label, $exitCode)
    }
}

try {
    if (-not $PythonExe -and $env:YOMIRA_PYTHON_EXE) {
        $PythonExe = $env:YOMIRA_PYTHON_EXE
    }
    if (-not $PythonExe) {
        $venvPython = Join-Path $projectRoot "backend/.venv/Scripts/python.exe"
        if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
            $PythonExe = $venvPython
        }
    }
    if (-not $NodeExe -and $env:YOMIRA_NODE_EXE) {
        $NodeExe = $env:YOMIRA_NODE_EXE
    }
    if (-not $NpmCmd -and $env:YOMIRA_NPM_CMD) {
        $NpmCmd = $env:YOMIRA_NPM_CMD
    }

    $PythonExe = Resolve-Executable -Candidate $PythonExe -CommandNames @("python.exe", "python")
    $NodeExe = Resolve-Executable -Candidate $NodeExe -CommandNames @("node.exe", "node")
    $NpmCmd = Resolve-Executable -Candidate $NpmCmd -CommandNames @("npm.cmd", "npm")
    $nodeDirectory = Split-Path -Parent $NodeExe
    $env:Path = "$nodeDirectory;$env:Path"

    $pythonVersion = (& $PythonExe --version | Out-String).Trim()
    $sqliteVersion = (& $PythonExe -c "import sqlite3; print(sqlite3.sqlite_version)" | Out-String).Trim()
    $nodeVersion = (& $NodeExe --version | Out-String).Trim()
    $npmVersion = (& $NpmCmd --version | Out-String).Trim()
    if ($pythonVersion -ne ("Python {0}" -f $runtimePins.python.version)) {
        throw ("Python fijado {0}; detectado {1}." -f $runtimePins.python.version, $pythonVersion)
    }
    if ($sqliteVersion -ne $runtimePins.sqlite.version) {
        throw ("SQLite fijado {0}; detectado {1}." -f $runtimePins.sqlite.version, $sqliteVersion)
    }
    if ($nodeVersion -ne ("v{0}" -f $runtimePins.node.version)) {
        throw ("Node fijado {0}; detectado {1}." -f $runtimePins.node.version, $nodeVersion)
    }
    if ($npmVersion -ne $runtimePins.node.npm_version) {
        throw ("npm fijado {0}; detectado {1}." -f $runtimePins.node.npm_version, $npmVersion)
    }

    Write-Host ("Python: {0}" -f $pythonVersion)
    Write-Host ("SQLite: {0}" -f $sqliteVersion)
    Write-Host ("Node: {0}; npm: {1}" -f $nodeVersion, $npmVersion)

    Push-Location (Join-Path $projectRoot "frontend")
    try {
        Invoke-Check "Dependencias frontend instaladas" $NpmCmd @("ls", "--depth=0")
        Invoke-Check "TypeScript" $NpmCmd @("run", "typecheck")
        Invoke-Check "ESLint" $NpmCmd @("run", "lint")
        Invoke-Check "Vitest" $NpmCmd @("run", "test")
        Invoke-Check "Build frontend" $NpmCmd @("run", "build")
    }
    finally {
        Pop-Location
    }

    Push-Location (Join-Path $projectRoot "backend")
    try {
        Invoke-Check "Consistencia de paquetes Python" $PythonExe @("-m", "pip", "check")
        Invoke-Check "Pytest" $PythonExe @("-m", "pytest")
        Invoke-Check "Ruff" $PythonExe @("-m", "ruff", "check", "app", "tests")
        Invoke-Check "Pyright" $PythonExe @(
            "-m",
            "pyright",
            "--pythonpath",
            $PythonExe,
            "app",
            "tests"
        )
    }
    finally {
        Pop-Location
    }

    $smokeScript = Join-Path $PSScriptRoot "smoke-integration.ps1"
    Write-Host ""
    Write-Host "[Smoke integration: arranque, proxy API y cierre]"
    & $smokeScript -PythonExe $PythonExe -NodeExe $NodeExe

    Write-Host ""
    Write-Host "QUALITY GATE: PASS"
}
catch {
    Write-Error ("QUALITY GATE: FAIL. " + $_.Exception.Message)
    exit 1
}
