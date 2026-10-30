<# 安装本地依赖并启动 Electron 桌面；构建和下载缓存均位于当前仓库。 #>
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $env:ELECTRON_RUN_AS_NODE = $null
    $env:ELECTRON_CACHE = Join-Path $projectRoot '.runtime/electron-cache'
    $env:ELECTRON_GET_USE_PROXY = 'true'
    & uv --cache-dir .uv-cache sync --frozen
    if ($LASTEXITCODE -ne 0) { throw 'Python 依赖准备失败。' }
    if (-not (Test-Path -LiteralPath 'frontend/node_modules')) {
        & npm.cmd --prefix frontend --cache .runtime/npm-cache ci
        if ($LASTEXITCODE -ne 0) { throw '前端依赖准备失败。' }
    }
    & npm.cmd --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
    if (-not (Test-Path -LiteralPath 'desktop/node_modules')) {
        & npm.cmd --prefix desktop --cache .runtime/npm-cache ci
        if ($LASTEXITCODE -ne 0) { throw 'Electron 依赖准备失败。' }
    }
    & node desktop/node_modules/electron/install.js
    if ($LASTEXITCODE -ne 0) { throw 'Electron 运行时下载失败。' }
    & npm.cmd --prefix desktop run dev
    if ($LASTEXITCODE -ne 0) { throw '桌面程序异常退出，请查看应用数据目录日志。' }
} finally {
    Pop-Location
}
