"""异步协调原生目录选择进程；取消与超时不产生项目登记。"""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

from agent_harness.execution.processes import _child_environment
from agent_harness.web.project_catalog import CatalogError


def _native_environment() -> dict[str, str]:
    """返回原生选择器专用环境，补齐 Windows Shell 路径变量且不继承凭据。

    无参数，不修改父进程；仅 GUI 子进程使用，Agent 工具和验收命令继续使用
    原有的受限环境。缺失的系统变量保持缺失，不拼接或猜测用户目录。
    """
    desktop_variables = {
        "SYSTEMDRIVE", "PROGRAMDATA", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "HOMEDRIVE", "HOMEPATH",
    }  # Shell 按注册表展开目录时需要，避免把 %SystemDrive% 当成相对路径。
    result = _child_environment()
    result.update({key: value for key, value in os.environ.items() if key.upper() in desktop_variables})
    return result


class FolderPicker:
    """同一服务只允许一个原生窗口，不阻塞 FastAPI 的任务查询与事件。"""

    TIMEOUT_SECONDS = 180.0  # 原生选择最长等待秒数，不随浏览器请求参数变化。

    def __init__(self) -> None:
        """初始化空闲窗口状态；不启动 GUI，不访问用户文件。"""
        self._active = False  # 单个选择窗口的生命周期标志。

    @property
    def available(self) -> bool:
        """返回当前本机是否具备受支持的 Windows/Tk 入口；不创建窗口。"""
        return os.name == "nt" and importlib.util.find_spec("tkinter") is not None

    async def choose(self) -> Path | None:
        """打开选择器并最多等待180秒，返回用户选择的目录或 None；超时/取消清理子进程。"""
        if not self.available:
            raise CatalogError("folder_picker_unavailable", "当前环境无法打开系统选择框，请在运行服务的 Windows 电脑上打开。", 503)
        if self._active:
            raise CatalogError("folder_picker_busy", "文件夹选择框已打开，请先完成或取消选择。", 409)
        self._active = True
        process = None
        try:
            # venv的Windows启动器会再派生Python进程；直接用基础解释器执行仅依赖标准库的脚本，
            # 使取消/超时追踪的PID就是GUI进程，避免只结束启动器而遗留窗口。
            interpreter = Path(sys.base_prefix) / "python.exe"
            if not interpreter.is_file():
                interpreter = Path(sys.executable)
            process = await asyncio.create_subprocess_exec(
                str(interpreter), "-I", "-X", "utf8", str(Path(__file__).with_name("native_dialog.py")),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=_native_environment(),
                cwd=tempfile.gettempdir(),  # 选择器不在项目根目录产生系统缓存。
                creationflags=0x08000000,  # 隐藏辅助控制台，原生文件夹对话框正常显示。
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self.TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise CatalogError("folder_picker_timeout", "选择文件夹超时，尚未打开项目，请重新选择。", 408) from None
            if process.returncode != 0 or len(stdout) > 32768:
                raise CatalogError("folder_picker_failed", "系统文件夹选择框未能正常完成，请重新尝试。", 503)
            payload = json.loads(stdout)
            if not isinstance(payload, dict):
                raise ValueError("invalid picker result")
            value = payload.get("path")
            if value is not None and (not isinstance(value, str) or not value or not Path(value).is_absolute()):
                raise ValueError("invalid picker result")
            return Path(value) if value is not None else None
        except CatalogError:
            raise
        except (OSError, ValueError):
            raise CatalogError("folder_picker_failed", "无法打开系统文件夹选择框，请检查本地桌面环境。", 503) from None
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass  # 自然退出与清理并发时无需再次终止。
                await process.wait()
            self._active = False
