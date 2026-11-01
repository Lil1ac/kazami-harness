"""无需Shell的只读目录发现；分页与扫描上限独立，复用工作区和运行记录保护边界。"""

import os
from pathlib import Path

from pydantic import Field

from agent_harness.schemas import DataModel
from agent_harness.execution.workspace import Workspace, WorkspaceError


class ListFilesArguments(DataModel):
    """列举一个目录的直接子项；不递归，继续发现子目录时显式传入返回的相对路径。"""

    directory: str = Field(default=".", min_length=1, max_length=4096)  # 工作区内目录，默认根目录。
    offset: int = Field(default=0, ge=0, le=10000)  # 名称排序后的零基项偏移，目录变化后应重新从0读取。
    limit: int = Field(default=100, ge=1, le=200)  # 每页最多返回条目数；受全目录10000项扫描上限约束。


def list_files(workspace: Path, runs_root: Path, arguments: ListFilesArguments) -> dict:
    """只读列举arguments目录并返回相对路径、类型和下一页偏移；不读取文件正文或启动命令。

    workspace/runs_root限定公开范围；不跟随链接或junction、不公开运行记录。扫描最多10000项，
    超限或有跳过项时明确标记不完整。目录不存在、越界或不可读抛WorkspaceError。
    """
    boundary = Workspace(workspace, runs_root)
    directory = boundary.resolve(arguments.directory, must_exist=True)
    if not directory.is_dir():
        raise WorkspaceError("not_directory", "只能列举工作区内的目录。")
    names = []  # 有界收集名称，排序后分页；不是跨请求固定的目录快照。
    scan_limited = False  # 达到扫描上限不能冒充完整目录。
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if len(names) >= 10000:
                    scan_limited = True
                    break
                names.append(entry.name)
    except OSError:
        raise WorkspaceError("directory_unreadable", "目录无法读取，请检查访问权限。") from None
    visible = []  # 只返回已核对的普通文件和目录，保护存储与链接目标。
    skipped = 0  # 被边界或文件状态拒绝的条目数量。
    for name in sorted(names):
        path = directory / name
        try:
            if path.is_symlink() or path.is_junction():
                skipped += 1
                continue
            resolved = boundary.resolve(str(path), must_exist=True)
            kind = "directory" if resolved.is_dir() else "file" if resolved.is_file() else None
            if kind is None:
                skipped += 1
                continue
            visible.append({"path": resolved.relative_to(boundary.workspace).as_posix(), "kind": kind})
        except (WorkspaceError, OSError):
            skipped += 1
    end = arguments.offset + arguments.limit
    more = end < len(visible)
    return {"entries": visible[arguments.offset:end], "next_offset": end if more else None,
            "truncated": more or scan_limited or skipped > 0, "scan_limited": scan_limited, "skipped": skipped}
