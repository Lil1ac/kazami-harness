"""校验命令参数与工作目录，将本地程序交给统一进程执行器。"""

import os
from pathlib import Path
import shutil
import sys

from pydantic import Field, model_validator

from ..processes import ProcessRunner
from ..schemas import DataModel
from ..workspace import Workspace, WorkspaceError


# 命令参数必须显式列出 argv；不接收自动交给 Shell 解析的命令字符串。
class RunCommandArguments(DataModel):
    argv: list[str] = Field(min_length=1, description="程序及参数数组。执行 Python 时可使用 python，系统会使用当前项目解释器。")
    cwd: str = Field(default=".", min_length=1, description="工作区内的相对工作目录。")

    @model_validator(mode="after")
    def validate_command(self) -> "RunCommandArguments":
        """拒绝空程序、空字节和绝对工作目录；普通参数允许空字符串。"""
        if not self.argv[0].strip() or any("\x00" in item for item in self.argv) or "\x00" in self.cwd:
            raise ValueError("程序和路径格式无效")
        if Path(self.cwd).anchor or Path(self.cwd).drive:
            raise ValueError("cwd 必须是工作区内的相对路径")
        return self


async def run_command(
    workspace: Path, runs_root: Path, arguments: RunCommandArguments, process_runner: ProcessRunner
) -> dict:
    """解析受限工作目录并运行命令；解释器脚本必须由显式 argv 启动。"""
    cwd = Workspace(workspace, runs_root).resolve(arguments.cwd, must_exist=True)
    if not cwd.is_dir():
        raise WorkspaceError("invalid_cwd", "命令工作目录必须是工作区内已存在的目录。")
    argv = list(arguments.argv)
    if argv[0] in {"python", "python3"}:
        argv[0] = sys.executable
    if os.name == "nt":
        candidate = Path(argv[0])
        if not candidate.is_absolute() and ("/" in argv[0] or "\\" in argv[0]):
            candidate = cwd / candidate
        found = str(candidate) if candidate.is_absolute() else shutil.which(argv[0])
        if found and Path(found).suffix.lower() not in {".exe", ".com"}:
            raise WorkspaceError("command_interpreter_required", "Windows 脚本需显式指定 Python 或其他可执行解释器。")
        if found:
            argv[0] = str(Path(found).resolve())
    return await process_runner.run(argv, cwd)
