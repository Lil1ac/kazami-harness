<# 构建可双击运行的 Windows 发布目录；缓存与打包中间产物保存在当前仓库 D 盘目录。 #>
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $env:ELECTRON_CACHE = Join-Path $projectRoot '.runtime/electron-cache'
    $env:ELECTRON_GET_USE_PROXY = 'true'
    $env:ELECTRON_BUILDER_CACHE = Join-Path $projectRoot '.runtime/electron-builder-cache'
    & uv --cache-dir .uv-cache sync --frozen --group desktop-build
    if ($LASTEXITCODE -ne 0) { throw '桌面依赖安装失败。' }
    # 干净源码检出也能直接打包，避免依赖此前手动启动网页端。
    if (-not (Test-Path -LiteralPath 'frontend/node_modules')) {
        & npm.cmd --prefix frontend --cache .runtime/npm-cache ci
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
    }
    & npm.cmd --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
    & npm.cmd --prefix desktop --cache .runtime/npm-cache ci
    if ($LASTEXITCODE -ne 0) { throw 'Electron 依赖安装失败。' }
    & node desktop/node_modules/electron/install.js
    if ($LASTEXITCODE -ne 0) { throw 'Electron 运行时下载失败。' }
    & uv --cache-dir .uv-cache run --frozen --group desktop-build pyinstaller --noconfirm --workpath .runtime/backend-build --distpath .runtime/backend-dist backend.spec
    if ($LASTEXITCODE -ne 0) { throw 'Python 后台打包失败。' }
    & npm.cmd --prefix desktop run package
    if ($LASTEXITCODE -ne 0) { throw 'Electron 桌面打包失败。' }
    Write-Output (Join-Path $projectRoot 'dist/desktop')
} finally {
    Pop-Location
}
