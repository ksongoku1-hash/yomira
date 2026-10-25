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
$novelcool = Join-Path $support 'lua/templates/NovelCool.lua'
if ((Get-FileHash -LiteralPath $novelcool -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.novelcool_source_sha256) { throw 'El módulo NovelCool no coincide con su hash.' }
foreach ($moduleName in @($pin.additional_modules_sha256.PSObject.Properties.Name)) {
    if ($moduleName -notmatch "^[A-Za-z][A-Za-z0-9]{0,79}$") { throw "Invalid module name." }
    $moduleFile = Join-Path $support "lua/modules/$moduleName.lua"
    if ((Get-FileHash -LiteralPath $moduleFile -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.additional_modules_sha256.$moduleName) { throw "El módulo $moduleName no coincide con su hash." }
}
foreach ($template in @($pin.supporting_templates_sha256.PSObject.Properties)) {
    if ($template.Name -notmatch '^[A-Za-z][A-Za-z0-9]{0,79}$') { throw 'Invalid template name.' }
    if ((Get-FileHash -LiteralPath (Join-Path $support "lua/templates/$($template.Name).lua") -Algorithm SHA256).Hash.ToLowerInvariant() -ne $template.Value) { throw 'The shared template does not match the verified source.' }
}
foreach ($catalog in @($pin.public_catalogs_sha256.PSObject.Properties)) {
    if ($catalog.Name -notmatch '^[a-f0-9]{32}$') { throw 'Invalid catalog id.' }
    if ((Get-FileHash -LiteralPath (Join-Path $support "catalogs/$($catalog.Name).db") -Algorithm SHA256).Hash.ToLowerInvariant() -ne $catalog.Value) { throw 'The public catalog does not match the verified source.' }
}
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
Copy-Item -LiteralPath $novelcool -Destination (Join-Path $target 'lua/templates/NovelCool.lua')
foreach ($moduleName in @($pin.additional_modules_sha256.PSObject.Properties.Name)) {
    if ($moduleName -notmatch "^[A-Za-z][A-Za-z0-9]{0,79}$") { throw "Invalid module name." }
    Copy-Item -LiteralPath (Join-Path $support "lua/modules/$moduleName.lua") -Destination (Join-Path $target "lua/modules/$moduleName.lua")
}
foreach ($template in @($pin.supporting_templates_sha256.PSObject.Properties)) {
    Copy-Item -LiteralPath (Join-Path $support "lua/templates/$($template.Name).lua") -Destination (Join-Path $target "lua/templates/$($template.Name).lua")
}
New-Item -ItemType Directory -Path (Join-Path $target 'data') -Force | Out-Null
foreach ($catalog in @($pin.public_catalogs_sha256.PSObject.Properties)) {
    Copy-Item -LiteralPath (Join-Path $support "catalogs/$($catalog.Name).db") -Destination (Join-Path $target "data/$($catalog.Name).db")
}
Write-Output "Código de FMD2 preparado: $target"
