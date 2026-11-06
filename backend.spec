# Electron 的独立 Python 后台进程；标准输入输出用于宿主协议，启动时由 Electron 隐藏控制台。
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

# 资源与后台二进制一起分发，用户无需安装 Python、uv 或 Node.js。
root = Path(SPECPATH)
a = Analysis(
    [str(root / "backend_entry.py")],
    pathex=[str(root / "src")], binaries=[],
    # Windows没有系统IANA数据库，发布包必须包含tzdata，不能只在开发环境中通过时区计算。
    datas=[(str(root / "frontend/dist"), "frontend/dist"), (str(root / "harness.example.toml"), "."), (str(root / "examples/skills"), "examples/skills")] + collect_data_files("tzdata"),
    hiddenimports=["uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="harness-backend", debug=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="harness-backend")
