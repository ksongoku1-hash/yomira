[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$support = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../third_party/fmd2'))
$pin = Get-Content -Raw -LiteralPath (Join-Path $support 'upstream.json') | ConvertFrom-Json
$target = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $target) { throw 'Elige una carpeta nueva; no se sobrescribe código existente.' }
$parent = Split-Path -Parent $target
if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'La carpeta superior debe existir.' }
$patch = Join-Path $support 'yomira.patch'
if ((Get-FileHash -LiteralPath $patch -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.patch_sha256) { throw 'El parche no coincide con su hash.' }
$api = Join-Path $support 'baseunits/uYomiraApi.pas'
if ((Get-FileHash -LiteralPath $api -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.api_source_sha256) { throw 'La unidad de API no coincide con su hash.' }
$readiness = Join-Path $support 'baseunits/uYomiraPageReadiness.pas'
if ((Get-FileHash -LiteralPath $readiness -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.readiness_source_sha256) { throw 'La unidad de páginas listas no coincide con su hash.' }
& git init --quiet $target
if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear la carpeta de código.' }
& git -C $target remote add origin $pin.repository
if ($LASTEXITCODE -ne 0) { throw 'No se pudo configurar el origen de FMD2.' }
& git -C $target fetch --depth 1 origin $pin.commit
if ($LASTEXITCODE -ne 0) { throw 'No se pudo recuperar FMD2.' }
& git -C $target checkout --detach $pin.commit
if ($LASTEXITCODE -ne 0) { throw 'No se pudo seleccionar la revisión de FMD2.' }
& git -C $target submodule update --init --recursive
if ($LASTEXITCODE -ne 0) { throw 'No se pudieron recuperar los submódulos de FMD2.' }
& git -C $target apply --check $patch
if ($LASTEXITCODE -ne 0) { throw 'El parche no se puede aplicar a la revisión indicada.' }
& git -C $target apply $patch
if ($LASTEXITCODE -ne 0) { throw 'No se pudo aplicar el parche.' }
Copy-Item -LiteralPath $api -Destination (Join-Path $target 'baseunits/uYomiraApi.pas')
Copy-Item -LiteralPath $readiness -Destination (Join-Path $target 'baseunits/uYomiraPageReadiness.pas')
Write-Output "Código de FMD2 preparado: $target"
