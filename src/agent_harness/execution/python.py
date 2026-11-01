"""生成并核验当轮 Python 源码目录，防止命令导入宿主另一份可编辑安装。"""

from pathlib import Path, PurePosixPath, PureWindowsPath
import os
import shutil

from agent_harness.schemas import PythonExecution


class PythonExecutionError(ValueError):
    """表示执行快照无效或目录在接纳后变化；消息可直接用于本地受控错误展示。"""


def prepare_python_execution(
    workspace: Path, python_paths: list[str] | None = None, *, python_executable: str | None = None,
) -> PythonExecution:
    """固定 workspace、项目解释器和源码目录，返回当轮执行快照，不启动进程。

    python_paths 为相对工作区的宿主配置；None 自动选择根目录及已存在的 src，
    [] 仅选择根目录。显式目录必须存在且留在工作区内；重复项按首次出现保留。
    python_executable 非空时按项目路径或 PATH 解析，None 按 PATH 查找 python/python3；
    PATH 无 Python 时记录 None，不阻止其他语言任务，调用 Python 命令时才明确拒绝。
    路径无效、无法读取或越界时抛 PythonExecutionError，不回退到宿主已安装包。
    """
    try:
        base = workspace.resolve(strict=True)
        if not base.is_dir():
            raise ValueError("not a directory")
        relative = python_paths if python_paths is not None else (["src"] if (base / "src").exists() else [])
        paths = [base]
        for value in relative:
            posix = PurePosixPath(value.replace("\\", "/"))
            windows = PureWindowsPath(value)
            if (not value.strip() or "\x00" in value or os.pathsep in value
                    or posix.is_absolute() or windows.drive or windows.root or ".." in posix.parts):
                raise ValueError("invalid source path")
            path = (base / value).resolve(strict=True)
            if not path.is_dir() or not path.is_relative_to(base):
                raise ValueError("source outside workspace")
            if path not in paths:
                paths.append(path)
        snapshot = PythonExecution(workspace=base, executable=resolve_python_executable(base, python_executable), python_paths=paths)
        validate_python_execution(snapshot)
        return snapshot
    except PythonExecutionError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise PythonExecutionError("Python 源码目录必须存在并位于实际工作区内，请核对项目配置。") from None


def validate_python_execution(snapshot: PythonExecution) -> None:
    """在子进程启动前重新核验 snapshot；返回 None，不重新发现或替换已经固定的目录。

    根目录、源码目录或解释器消失、改为其他路径或越界时抛 PythonExecutionError。
    只核对当前执行，不在读取历史记录时调用，避免将当前环境伪装成旧轮快照。
    """
    try:
        base = snapshot.workspace
        if base.resolve(strict=True) != base or not base.is_dir():
            raise ValueError("workspace changed")
        if snapshot.executable is not None and (snapshot.executable.resolve(strict=True) != snapshot.executable
                or not snapshot.executable.is_file()):
            raise ValueError("interpreter changed")
        for directory in snapshot.python_paths:
            if (directory.resolve(strict=True) != directory or not directory.is_dir()
                    or not directory.is_relative_to(base)):
                raise ValueError("source directory changed")
    except (OSError, RuntimeError, ValueError):
        raise PythonExecutionError("Python 执行目录或解释器已变化，请核对项目后重新发起运行。") from None


def resolve_python_executable(workspace: Path, requested: str | None) -> Path | None:
    """解析 workspace 的 requested 解释器并返回绝对入口；None/空白按 PATH 查找。

    显式相对路径以实际工作区解释；显式入口缺失、不可执行或为脚本时抛受控错误。
    默认 PATH 没有 Python 返回 None，不使用 Harness 自身解释器补位，不启动程序。
    """
    explicit = requested is not None and bool(requested.strip())
    program = requested.strip() if explicit else (shutil.which("python") or shutil.which("python3"))
    if not program:
        return None
    try:
        if "\x00" in program:
            raise ValueError("invalid interpreter")
        if Path(program).is_absolute() or "/" in program or "\\" in program:
            executable = (workspace / program).resolve(strict=True)
        else:
            found = shutil.which(program)
            if found is None:
                raise ValueError("missing interpreter")
            executable = Path(found).resolve(strict=True)
        if not executable.is_file() or (os.name != "nt" and not os.access(executable, os.X_OK)):
            raise ValueError("not executable")
        if os.name == "nt" and executable.suffix.lower() not in {".exe", ".com"}:
            raise ValueError("explicit interpreter required")
        return executable
    except (OSError, RuntimeError, ValueError):
        raise PythonExecutionError("Python 解释器无效，请核对项目设置中的程序路径和执行权限。") from None


def python_command(argv: list[str], execution: PythonExecution) -> list[str]:
    """返回采用 execution 的命令副本；仅映射裸 python/python3，其他程序和参数原样保留。

    当轮未找到 Python 却调用其别名时抛 PythonExecutionError，不修改调用方 argv，
    不把 uv/node 等入口改写为 Python，也不启动进程。
    """
    result = list(argv)
    if result and result[0] in {"python", "python3"}:
        if execution.executable is None:
            raise PythonExecutionError("PATH 中未找到 Python，请配置项目解释器或使用明确的项目命令。")
        result[0] = str(execution.executable)
    return result
