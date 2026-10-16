[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$env:YOMIRA_DATA_ROOT=Join-Path $projectRoot 'user-data\AppData\Yomira\YomiraData'
$env:YOMIRA_LIBRARY_ROOT=Join-Path $projectRoot 'user-data\libraries\Mangas'
$env:YOMIRA_PYTHON_EXE=Join-Path $projectRoot 'runtime\python-3.14.7\python.exe'
$env:YOMIRA_NODE_EXE=Join-Path $projectRoot 'runtime\node-24.21.0\node.exe'
$env:YOMIRA_NPM_CMD=Join-Path $projectRoot 'runtime\node-24.21.0\npm.cmd'
$env:YOMIRA_FMD2_EXECUTABLE=Join-Path $projectRoot 'user-data\fmd2-runtime\fmd.exe'
$env:YOMIRA_API_HOST='127.0.0.1'
$env:YOMIRA_API_PORT='5173'
$env:YOMIRA_APP_ENV='prod'
& (Join-Path $PSScriptRoot 'start-yomira.ps1')
if($LASTEXITCODE -ne 0){throw "Yomira terminó con código $LASTEXITCODE."}