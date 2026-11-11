[CmdletBinding()]
param(
    [string]$SourceDir = '',
    [string]$OutFile = '',
    [string]$ReferenceCommit = 'fd123c5e8da3b0d33b03dee514fc457a258094e1'
)
$ErrorActionPreference = 'Stop'
if (-not $OutFile) { $OutFile = Join-Path (Split-Path $PSScriptRoot -Parent) 'res\gacha_metadata_cn.json' }
# Maintenance only. No account data is used or uploaded by this script.
if (-not $SourceDir) {
    $SourceDir = Join-Path ([IO.Path]::GetTempPath()) ('fgo-metadata-' + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $SourceDir | Out-Null
    $urls = @{
        'gachas.json' = 'https://api.atlasacademy.io/repo/CN/master/mstGacha.json'
        'extra.json' = 'https://data.chaldea.center/mstGachaCNExtra.json'
        'servants.json' = 'https://api.atlasacademy.io/export/JP/basic_servant.json'
        'cn-servants.json' = 'https://api.atlasacademy.io/export/CN/basic_servant.json'
        'jp-equips.json' = 'https://api.atlasacademy.io/export/JP/basic_equip.json'
        'cn-equips.json' = 'https://api.atlasacademy.io/export/CN/basic_equip.json'
        'version.json' = 'https://data.chaldea.center/version.json'
        'entities.json' = 'https://data.chaldea.center/entities.json'
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    foreach ($name in $urls.Keys) {
        Invoke-WebRequest -UseBasicParsing -UserAgent 'Mozilla/5.0' -Uri $urls[$name] -OutFile (Join-Path $SourceDir $name) -TimeoutSec 30
    }
    $version = Get-Content (Join-Path $SourceDir 'version.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($file in $version.files.PSObject.Properties.Value | Where-Object key -EQ 'wiki.servants') {
        Invoke-WebRequest -UseBasicParsing -UserAgent 'Mozilla/5.0' -Uri ('https://data.chaldea.center/' + $file.filename) -OutFile (Join-Path $SourceDir $file.filename) -TimeoutSec 30
    }
}
function Read-Data([string]$Name) {
    return (Get-Content (Join-Path $SourceDir $Name) -Raw -Encoding UTF8 | ConvertFrom-Json)
}
$wiki = @{}
$version = Read-Data 'version.json'
foreach ($file in $version.files.PSObject.Properties.Value | Where-Object key -EQ 'wiki.servants') {
    foreach ($svt in (Read-Data $file.filename)) { $wiki[[string]$svt.collectionNo] = $svt }
}
$servants = @{}
foreach ($entity in (Read-Data 'entities.json')) {
    $servants[[string]$entity.id] = [ordered]@{ id = [int]$entity.id; ssr = $false }
}
# JP covers future CN servants; CN also includes region-specific entries.
foreach ($svt in (@(Read-Data 'servants.json') + @(Read-Data 'cn-servants.json'))) {
    $playable = $svt.collectionNo -gt 0 -and $svt.type -in @('normal', 'heroine')
    $extra = $wiki[[string]$svt.collectionNo]
    if ($playable -and -not $extra) { throw ('Missing obtain metadata: ' + $svt.id) }
    $excluded = @($extra.obtains | Where-Object { $_ -in @('eventReward', 'clearReward', 'unavailable') }).Count -gt 0
    $servants[[string]$svt.id] = [ordered]@{
        id = [int]$svt.id
        ssr = [bool]($playable -and $svt.rarity -eq 5 -and -not $excluded -and $svt.id -notin @(800100, 2801200))
    }
}
# Craft essences are present in userSvtCollection too. Include both regions explicitly.
foreach ($equip in (@(Read-Data 'jp-equips.json') + @(Read-Data 'cn-equips.json'))) {
    $key = [string]$equip.id
    if ($servants.ContainsKey($key) -and $servants[$key].ssr) { throw ('Conflicting servant/equip ID: ' + $key) }
    $servants[$key] = [ordered]@{ id = [int]$equip.id; ssr = $false }
}
$gachas = @{}
foreach ($g in @((Read-Data 'gachas.json')) + @((Read-Data 'extra.json'))) {
    $gachas[[string]$g.id] = [ordered]@{ id = [int]$g.id; type = [int]$g.type }
}
if ($servants.Count -lt 100 -or $gachas.Count -lt 100) { throw 'Incomplete public metadata' }
$hashes = [ordered]@{}
foreach ($file in Get-ChildItem $SourceDir -File -Filter *.json | Sort-Object Name) {
    $hashes[$file.Name] = (Get-FileHash $file.FullName -Algorithm SHA256).Hash
}
$result = [ordered]@{
    schemaVersion = 1; region = 'CN'; generatedAt = [DateTime]::UtcNow.ToString('o')
    referenceCommit = $ReferenceCommit
    sources = @('https://api.atlasacademy.io/repo/CN/master/mstGacha.json', 'https://data.chaldea.center/mstGachaCNExtra.json', 'https://api.atlasacademy.io/export/JP/basic_servant.json', 'https://api.atlasacademy.io/export/CN/basic_servant.json', 'https://api.atlasacademy.io/export/JP/basic_equip.json', 'https://api.atlasacademy.io/export/CN/basic_equip.json', 'https://data.chaldea.center/version.json', 'https://data.chaldea.center/wiki.servants.*.json', 'https://data.chaldea.center/entities.json')
    sourceHashes = $hashes
    servants = @($servants.Values | Sort-Object { $_.id })
    gachas = @($gachas.Values | Sort-Object { $_.id })
}
$parent = Split-Path ([IO.Path]::GetFullPath($OutFile)) -Parent
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$temp = $OutFile + '.tmp'
[IO.File]::WriteAllText($temp, ($result | ConvertTo-Json -Depth 8 -Compress), (New-Object Text.UTF8Encoding($false)))
Move-Item -LiteralPath $temp -Destination $OutFile -Force
Write-Output ('Metadata ready: ' + $OutFile)
