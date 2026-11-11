param(
    [Parameter(Mandatory = $true)]
    [string]$PythonRoot
)
$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path $PSScriptRoot -Parent
$buildRoot = Join-Path $pluginRoot ('.build\binding-' + [guid]::NewGuid().ToString('N'))
$payload = Join-Path $buildRoot 'payload'
$release = Join-Path $pluginRoot 'releases'
New-Item -ItemType Directory -Path $payload, $release -Force | Out-Null
function Copy-Payload([string]$source, [string]$relative) {
    $target = Join-Path $payload $relative
    New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $target
    if ((Get-FileHash -LiteralPath $source).Hash -ne (Get-FileHash -LiteralPath $target).Hash) { throw '复制校验失败' }
}
foreach ($name in @('python.exe', 'python313.dll', 'python3.dll', 'python313.zip', 'LICENSE.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $PythonRoot $name))) { throw ('缺少便携 Python：' + $name) }
}
foreach ($file in Get-ChildItem -LiteralPath $PythonRoot -File) {
    if ($file.Extension -in @('.exe', '.dll', '.pyd', '.zip') -or $file.Name -eq 'LICENSE.txt') {
        Copy-Payload $file.FullName ('python\' + $file.Name)
    }
}
[IO.File]::WriteAllText((Join-Path $payload 'python\python313._pth'), "python313.zip`n.`nLib/site-packages`n../backend`n", [Text.UTF8Encoding]::new($false))
$site = Join-Path $PythonRoot 'Lib\site-packages'
foreach ($directory in Get-ChildItem -LiteralPath $site -Directory) {
    if ($directory.Name -notin @('PIL', 'Crypto', 'qrcode', 'colorama') -and $directory.Name -notmatch '^(pillow|pycryptodome|qrcode|colorama)-.*\.dist-info$') { continue }
    foreach ($file in Get-ChildItem -LiteralPath $directory.FullName -File -Recurse) {
        if ($file.FullName -match '[\\/](__pycache__|SelfTest)[\\/]') { continue }
        Copy-Payload $file.FullName ('python\Lib\site-packages\' + $file.FullName.Substring($site.Length + 1))
    }
}
foreach ($name in @('__init__.py', 'errors.py', 'protocol.py', 'game_profile.py', 'statistics.py', 'LICENSE', 'UPSTREAM.md')) {
    Copy-Payload (Join-Path $pluginRoot ('gacha\account_protocol\' + $name)) ('backend\account_protocol\' + $name)
}
foreach ($name in @('game-protocol.json', 'game-protocol-uo.json')) {
    Copy-Payload (Join-Path $pluginRoot ('private\' + $name)) ('backend\' + $name)
}
Copy-Payload (Join-Path $PSScriptRoot 'local_login.py') 'backend\local_login.py'
Copy-Payload (Join-Path $PSScriptRoot 'import-public.pem') 'backend\import-public.pem'
Copy-Payload (Join-Path $PSScriptRoot '使用说明.md') '使用说明.md'
foreach ($file in Get-ChildItem -LiteralPath $payload -File -Recurse) {
    if ($file.Name -match '\.dpapi$|\.fgobind$|import-private|credentials') { throw '禁止打包私钥或用户凭据' }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = Join-Path $buildRoot 'payload.zip'
[IO.Compression.ZipFile]::CreateFromDirectory($payload, $zip, [IO.Compression.CompressionLevel]::Optimal, $false)
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$exe = Join-Path $release 'FGO账号绑定工具-便携版.exe'
if (Test-Path -LiteralPath $exe) { Copy-Item -LiteralPath $exe -Destination (Join-Path $buildRoot 'previous.exe') }
& $compiler /nologo /target:winexe /platform:x64 /optimize+ ("/out:" + $exe) ('/win32manifest:' + (Join-Path $PSScriptRoot 'app.manifest')) /r:System.dll /r:System.Core.dll /r:System.Drawing.dll /r:System.Windows.Forms.dll /r:System.Web.Extensions.dll /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll ("/resource:" + $zip + ',payload.zip') (Join-Path $PSScriptRoot 'BindingTool.cs')
if ($LASTEXITCODE -ne 0) { throw '编译失败' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot '使用说明.md') -Destination (Join-Path $release '使用说明.md')
Write-Output ('EXE: ' + $exe)
Write-Output ('Payload: ' + $payload)
Write-Output ('SHA256: ' + (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash)
