"""本地 Git 项目预设与审阅记录；不承载模型凭据或任意网页命令。"""

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from agent_harness.schemas import DataModel


class ProjectPreset(DataModel):
    """宿主登记的 Git 根目录和测试命令，网页只能选择其标识。"""

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")  # 稳定项目标识，供网页引用。
    label: str = Field(min_length=1, max_length=100)  # 面向用户的项目名称。
    path: Path  # 宿主提供的 Git 仓库根目录，加载配置时解析为绝对路径。
    test_argv: list[str] = Field(default_factory=list)  # 用户或宿主登记的测试程序与参数，不经 shell；空列表表示未配置。
    test_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)  # 测试自身上限，单位秒。
    python_executable: str | None = Field(default=None, max_length=4096)  # 显式项目解释器；空值按 PATH 解析，相对路径按实际工作区解释。
    python_paths: list[str] | None = None  # 共享源码目录，相对实际工作区；None 自动根/src，[] 仅根目录。

    @model_validator(mode="after")
    def validate_preset(self) -> "ProjectPreset":
        """校验宿主标识、命令和导入目录；成功返回自身，非法配置抛 ValueError。"""
        if not self.id.strip() or not self.label.strip() or (self.test_argv and not self.test_argv[0].strip()):
            raise ValueError("项目标识、名称和测试程序不能为空白")
        if any("\0" in part for part in self.test_argv):
            raise ValueError("测试命令不能包含空字节")
        for item in self.python_paths or []:
            posix = PurePosixPath(item.replace("\\", "/"))
            windows = PureWindowsPath(item)
            if not item or "\0" in item or os.pathsep in item or posix.is_absolute() or windows.drive or windows.root or ".." in posix.parts:
                raise ValueError("Python 源码目录必须是工作区内的相对目录")
        return self


class ChangedFile(DataModel):
    """快照中一个版本控制变更；重命名目标在 path 中，源路径在补丁中。"""

    path: str  # Git 相对仓库根目录的路径，保留 Unicode。
    status: Literal["added", "modified", "deleted", "renamed"]  # 相对基线提交的变更类型。
    binary: bool  # Git 检测为二进制时为真，补丁仍包含可应用的 binary patch。


class ProjectWorkspace(DataModel):
    """一个隔离会话独占的工作树状态；每轮证据由 ProjectReview 分别固定。"""

    session_id: UUID  # 创建会话及工作树的首轮标识，目录生命周期归此会话所有。
    latest_run_id: UUID  # 当前可审阅/继续的最新轮；旧轮只读，不能删除共享目录。
    worktree: str  # 同一会话持续复用的实际绝对目录。
    status: Literal["pending", "kept", "discarded"] = "pending"  # 最新代码的会话级保留/丢弃状态。


class ProjectReview(DataModel):
    """持久化的任务代码审阅状态；执行和测试结果由运行记录分别保存。"""

    run_id: UUID  # 关联 Runner 的运行标识。
    session_id: UUID  # 共享工作树所属会话；不是每轮重新创建目录。
    project_id: str  # 任务启动时选择的宿主项目标识。
    project_label: str  # 当时的项目显示名，重启后无需依赖配置重建。
    repository: str  # 原始仓库绝对根目录，仅用于归属校验和展示。
    base_ref: str  # 用户选取的原始引用，默认 HEAD。
    base_commit: str  # 已解析并固定的完整提交哈希。
    source_dirty: bool  # 启动时源仓库是否有未提交改动；这些改动不会复制。
    worktree: str  # 本会话共享 detached 工作树路径；历史文件从固定快照读取。
    status: Literal["pending", "kept", "discarded"] = "pending"  # 用户审阅决定；缺失目录不自动视为丢弃。
    snapshot_status: Literal["pending", "ready", "error"] = "pending"  # 终态代码快照是否完整可用。
    files: list[ChangedFile] = Field(default_factory=list)  # 完整快照的变更列表。
    patch_available: bool = False  # 完整补丁存在且通过完整性检查才为真。
    warnings: list[str] = Field(default_factory=list)  # 有界、受控提示，不回传原始 Git 错误。
