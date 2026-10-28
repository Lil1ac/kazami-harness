"""校验命令参数与工作目录，将本地程序交给统一进程执行器。"""

import os
from pathlib import Path
import shutil

from pydantic import Field, model_validator

from ..processes import ProcessRunner
from ..schemas import PythonExecution
from ..execution import python_command
from ..schemas import DataModel
from ..workspace import Workspace, WorkspaceError


# 命令参数必须显式列出 argv；不接收自动交给 Shell 解析的命令字符串。
class RunCommandArguments(DataModel):
    argv: list[str] = Field(min_length=1, description="程序及参数数组。执行 Python 时可使用 python，系统使用项目配置或 PATH 解析的本轮解释器，也可显式填写其他可执行程序路径。")
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
    workspace: Path, runs_root: Path, arguments: RunCommandArguments, process_runner: ProcessRunner,
    python_execution: PythonExecution,
) -> dict:
    """在 workspace 内按 arguments.argv/cwd 运行命令，返回进程输出和结束状态。

    runs_root 保持文件边界；process_runner 保存输出；python_execution 是当轮固定的
    源码与默认解释器快照，切换子目录不改变它。非法 cwd 或快照归属抛 WorkspaceError。
    """
    cwd = Workspace(workspace, runs_root).resolve(arguments.cwd, must_exist=True)
    if not cwd.is_dir():
        raise WorkspaceError("invalid_cwd", "命令工作目录必须是工作区内已存在的目录。")
    if python_execution.workspace != workspace.resolve():
        raise WorkspaceError("python_execution_mismatch", "Python 执行快照与当前工作区不一致。")
    argv = python_command(arguments.argv, python_execution)
    if os.name == "nt":
        candidate = Path(argv[0])
        if not candidate.is_absolute() and ("/" in argv[0] or "\\" in argv[0]):
            candidate = cwd / candidate
        found = str(candidate) if candidate.is_absolute() else shutil.which(argv[0])
        if found and Path(found).suffix.lower() not in {".exe", ".com"}:
            raise WorkspaceError("command_interpreter_required", "Windows 脚本需显式指定 Python 或其他可执行解释器。")
        if found:
            argv[0] = str(Path(found).resolve())
    return await process_runner.run(argv, cwd, python_execution=python_execution)
