[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ReleaseOutput,
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$BackupRoot='D:\Yomira-Backup'
)
$ErrorActionPreference='Stop'
if($Version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$'){throw 'Invalid release version.'}
$ReleaseOutput=[IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ReleaseOutput).Path).TrimEnd('\')
$BackupRoot=[IO.Path]::GetFullPath($BackupRoot)
$zip=Get-ChildItem -LiteralPath $ReleaseOutput -File -Recurse -Filter "Yomira-Windows-x64-$Version.zip" | Select-Object -First 1
$installers=@(Get-ChildItem -LiteralPath (Join-Path $ReleaseOutput 'installer') -File -Recurse -Filter '*.exe' -ErrorAction SilentlyContinue)
if($null -eq $zip -or $zip.Length -eq 0 -or $installers.Count -ne 1 -or $installers[0].Length -eq 0){throw 'A successful release ZIP and exactly one installer are required before backup.'}
$releaseRoot=Join-Path $BackupRoot 'releases'
New-Item -ItemType Directory -Path $releaseRoot -Force|Out-Null
$final=Join-Path $releaseRoot $Version
if(Test-Path -LiteralPath $final){throw "Version backup already exists; bump APP_VERSION before packaging again: $Version"}
$stage=Join-Path $releaseRoot ('.staging-'+$Version+'-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage|Out-Null
try{
    $artifactRoot=Join-Path $stage 'release-artifacts'
    New-Item -ItemType Directory -Path $artifactRoot|Out-Null
    $inputs=@($zip)+$installers
    foreach($inputFile in $inputs){Copy-Item -LiteralPath $inputFile.FullName -Destination $artifactRoot}
    $metadata=[pscustomobject]@{
        Version=$Version
        CreatedUtc=[DateTime]::UtcNow.ToString('o')
        Artifacts=@($inputs|ForEach-Object {$_.Name})
        Scope='Release ZIP and installer only; no user data, library, database, downloads, runtime, or logs.'
    }
    $metadata|ConvertTo-Json -Depth 4|Set-Content -LiteralPath (Join-Path $stage 'backup-metadata.json') -Encoding UTF8
    $files=@(Get-ChildItem -LiteralPath $stage -File -Recurse -Force)
    $sha=[Security.Cryptography.SHA256]::Create()
    $rows=[Collections.Generic.List[object]]::new()
    foreach($file in $files){
        $stream=[IO.File]::OpenRead($file.FullName)
        try{$hash=[BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','')}finally{$stream.Dispose()}
        $rows.Add([pscustomobject]@{Path=$file.FullName.Substring($stage.Length+1);Bytes=$file.Length;SHA256=$hash;LastWriteUtc=$file.LastWriteTimeUtc.ToString('o');Attributes=$file.Attributes.ToString()})
    }
    $sha.Dispose()
    $manifest=Join-Path $stage 'SHA256-MANIFEST.csv'
    $rows|Export-Csv -LiteralPath $manifest -NoTypeInformation -Encoding UTF8
    foreach($row in $rows){
        $saved=Join-Path $stage $row.Path
        if(-not(Test-Path -LiteralPath $saved -PathType Leaf) -or (Get-Item -LiteralPath $saved).Length -ne [long]$row.Bytes){throw "Release backup self-check failed: $($row.Path)"}
        $actual=(Get-FileHash -LiteralPath $saved -Algorithm SHA256).Hash
        if($actual -ne $row.SHA256){throw "Release backup hash mismatch: $($row.Path)"}
    }
    foreach($inputFile in $inputs){
        $saved=Join-Path $artifactRoot $inputFile.Name
        if((Get-FileHash -LiteralPath $inputFile.FullName -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $saved -Algorithm SHA256).Hash){throw "Release artifact copy mismatch: $($inputFile.Name)"}
    }
    Move-Item -LiteralPath $stage -Destination $final
    $snapshots=@(Get-ChildItem -LiteralPath $releaseRoot -Directory -Force|Where-Object {$_.Name -notlike '.staging-*'}|Sort-Object CreationTimeUtc -Descending)
    foreach($old in ($snapshots|Select-Object -Skip 5)){Remove-Item -LiteralPath $old.FullName -Recurse -Force}
    Write-Output "Verified version-only backup: $final ($($rows.Count) files)"
}catch{
    if(Test-Path -LiteralPath $stage){Remove-Item -LiteralPath $stage -Recurse -Force}
    throw
}
