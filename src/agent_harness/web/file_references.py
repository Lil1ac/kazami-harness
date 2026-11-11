"""项目文件选择只读取执行来源，入队时核对预览版本并固定模型将收到的资料。"""

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from agent_harness.execution.workspace import Workspace, WorkspaceError
from agent_harness.permissions.policy import FILE_SCOPE, path_allowed, roots
from agent_harness.projects import ProjectError
from agent_harness.schemas import (
    DataModel, MAX_REFERENCE_BYTES, MAX_REFERENCE_TOTAL_BYTES,
    ProjectFileRange, ProjectFileReference, ProjectFileSnapshot, TaskSpec,
)
from agent_harness.storage.sessions import payload_digest
from agent_harness.tools.files import read_file_snapshot
from agent_harness.tools.listing import ListFilesArguments, list_files
from agent_harness.work.tasks import TaskError

if TYPE_CHECKING:
    from fastapi import FastAPI
    from agent_harness.web.service import WebRunManager

# 选择行段时最多扫描此大小的源文件，实际提交给模型的字节另有限额。
MAX_REFERENCE_SOURCE_BYTES = 1024 * 1024


class ReferenceTarget(DataModel):
    """在已登记项目、普通目录或会话最新轮中选择唯一读取来源。"""

    project_id: str | None = Field(default=None, min_length=1, max_length=64)  # 可从提交读取的 Git 项目。
    workspace_id: str | None = Field(default=None, min_length=1, max_length=64)  # 按原目录读取的普通工作区。
    run_id: UUID | None = Field(default=None, strict=False)  # 续轮只按该会话的实际目录读取。
    execution_mode: Literal["local", "isolated"] = "local"  # 新建 Git 任务可选择提交基线。
    base_ref: str = Field(default="HEAD", min_length=1, max_length=200)  # 首轮隔离任务使用的 Git 引用。

    @model_validator(mode="after")
    def validate_source(self) -> "ReferenceTarget":
        """校验三个来源字段恰有一个，基线仅可用于新建 Git 隔离任务；成功返回自身。"""
        if sum(value is not None for value in (self.project_id, self.workspace_id, self.run_id)) != 1:
            raise ValueError("请选择一个项目或会话工作区")
        if self.execution_mode == "isolated" and self.project_id is None:
            raise ValueError("仅 Git 项目支持按基线读取")
        if self.execution_mode == "local" and self.base_ref != "HEAD":
            raise ValueError("原目录读取不指定 Git 基线")
        return self


class ReferenceListing(DataModel):
    """打开文件选择器中的一层目录，按项目相对路径分页。"""

    target: ReferenceTarget  # 实际执行位置，切换项目或基线时重新读取。
    directory: str = Field(default=".", min_length=1, max_length=4096)  # 默认根目录。
    offset: int = Field(default=0, ge=0, le=10000)  # 目录内从零开始的页偏移。


class ReferencePreviewRequest(DataModel):
    """预览指定来源的整文件或行段，生成提交需要核对的版本。"""

    target: ReferenceTarget  # 与文件列表使用同一实际读取范围。
    selection: ProjectFileRange  # 未选择行段时请求整份文本。


class ReferencePreview(DataModel):
    """返回可提交快照和总行数；超出单项预算时提供有界预览供用户选择行段。"""

    snapshot: ProjectFileSnapshot | None  # None 表示当前范围超限，不可提交。
    line_count: int = Field(ge=0)  # 源文件总行数，空文件为零。
    excerpt: str = ""  # 仅超出单项限制时提供当前选段的开头。


def check_reference_budget(task: TaskSpec, max_input_bytes: int) -> None:
    """以真实模型编码检查 task 的新增用户消息；max_input_bytes 为整体预算，超限在入队前拒绝。"""
    if not task.file_references:
        return
    message = {"messages": [{"role": "user", "content": task.user_content()}], "tools": []}
    size = len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > max_input_bytes:
        raise TaskError("reference_input_budget", "任务与文件引用超出模型输入预算，请缩小行段或移除引用。")


class FileReferenceSource:
    """复用宿主文件权限与 Git 读取边界，为一次预览或接纳提供有界的项目文本。"""

    def __init__(self, manager: "WebRunManager", root: Path, source_id: str, commit: str | None = None) -> None:
        """绑定已核对的 root/source_id，commit=None 读当前目录，否则只读该 Git 树。"""
        from agent_harness.web.jobs import directory_identity

        policy = manager.run_config.permission_policy
        if policy.read != "allow" or not policy.read_roots:
            raise TaskError("reference_read_denied", "当前宿主权限不允许读取项目文件。")
        self.root = root  # 真实工作区或 Git 仓库根目录。
        self.commit = commit  # 首轮隔离使用固定树，续轮直接读实际工作树。
        self.manager = manager  # 使用既有的运行记录保护根与有界 Git 调用。
        self.identity = (source_id, str(root), directory_identity(root), commit)  # 防止换项目、工作树或基线后复用旧预览。
        self.workspace = Workspace(root, manager.runs_root)  # 检查本机实际路径和运行记录目录。
        try:
            # 提交树中的路径不应受原目录未提交链接影响，只在工作区读取时解析实体授权根。
            self.allowed = tuple(root / name for name in policy.read_roots) if commit else roots(root, policy.read_roots)
        except ValueError:
            raise TaskError("reference_read_denied", "项目可读目录无法核对。") from None

    def _path(self, name: str, *, directory: bool = False) -> Path:
        """核对 name 的项目相对路径及读取范围；目录可为授权子目录的祖先，返回可读实体路径。"""
        if name != "." or not directory:
            try:
                ProjectFileRange(path=name)
            except ValidationError:
                raise TaskError("reference_invalid_path", "请选择项目内的普通文件或目录。") from None
        target = self.root / name
        if self.commit is None:
            resolved = self.workspace.resolve(name)
            if resolved != target:
                raise TaskError("reference_invalid_path", "项目文件引用不经过链接目录。")
        elif target.is_relative_to(self.workspace.runs_root):
            raise TaskError("reference_invalid_path", "不能引用运行记录目录。")
        if not path_allowed(target, self.allowed, directory=directory):
            raise TaskError("reference_read_denied", "文件不属于当前可读目录。")
        return target

    def _git(self, arguments: list[str], *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        """执行 arguments 描述的只读 Git 对象查询，max_bytes 限制输出；失败使用受控错误。"""
        try:
            return self.manager.projects._git(self.root, arguments, max_bytes=max_bytes)
        except ProjectError as error:
            raise TaskError(error.code, "无法读取所选基线中的项目文件。") from None

    def listing(self, directory: str, offset: int) -> dict:
        """返回 directory 中可浏览的 100 个子项及下一页偏移，offset 从零开始。"""
        try:
            self._path(directory, directory=True)
            if self.commit is None:
                token = FILE_SCOPE.set((self.root, self.allowed))
                try:
                    result = list_files(self.root, self.manager.runs_root, ListFilesArguments(directory=directory, offset=offset))
                finally:
                    FILE_SCOPE.reset(token)
                return {
                    "entries": [item for item in result["entries"] if item["path"].rsplit("/", 1)[-1].casefold() != ".git"],
                    "next_offset": result["next_offset"], "scan_limited": result["scan_limited"],
                }
            tree = self.commit if directory == "." else f"{self.commit}:{directory}"
            entries = []
            for record in self._git(["ls-tree", "-z", tree]).split(b"\0"):
                if not record:
                    continue
                metadata, filename = record.split(b"\t", 1)
                mode, kind, _ = metadata.split()
                if mode not in {b"040000", b"100644", b"100755"}:
                    continue
                try:
                    name = filename.decode("utf-8")
                    path = name if directory == "." else f"{directory}/{name}"
                    is_directory = kind == b"tree"
                    self._path(path, directory=is_directory)
                except (UnicodeDecodeError, TaskError):
                    continue
                entries.append({"path": path, "kind": "directory" if is_directory else "file"})
            entries.sort(key=lambda item: item["path"])
            end = offset + 100
            return {"entries": entries[offset:end], "next_offset": end if end < len(entries) else None, "scan_limited": False}
        except WorkspaceError as error:
            raise TaskError(error.code, error.message) from None

    def _read(self, path: str) -> bytes:
        """从已核对来源读取 path 的完整有界字节；普通文件和 Git blob 使用相同大小限制。"""
        self._path(path)
        if self.commit is None:
            token = FILE_SCOPE.set((self.root, self.allowed))
            try:
                return read_file_snapshot(self.root, self.manager.runs_root, path, MAX_REFERENCE_SOURCE_BYTES)
            finally:
                FILE_SCOPE.reset(token)
        entry = self._git(["ls-tree", "-z", "--full-tree", self.commit, "--", f":(literal){path}"]).split(b"\0")[0]
        if not entry:
            raise TaskError("reference_not_found", "所选 Git 提交中没有此文件。")
        metadata, _ = entry.split(b"\t", 1)
        mode, kind, identifier = metadata.decode("ascii").split()
        if mode not in {"100644", "100755"} or kind != "blob":
            raise TaskError("reference_invalid_path", "请选择 Git 提交中的普通文本文件。")
        if int(self._git(["cat-file", "-s", identifier])) > MAX_REFERENCE_SOURCE_BYTES:
            raise TaskError("reference_source_too_large", "源文件超过项目引用的读取上限。")
        return self._git(["cat-file", "blob", identifier], max_bytes=MAX_REFERENCE_SOURCE_BYTES + 1)

    def preview(self, selection: ProjectFileRange) -> ReferencePreview:
        """读取 selection 并返回完整行段或有界预览；版本绑定同一份字节、行段和执行来源。"""
        try:
            raw = self._read(selection.path)
            content = raw.decode("utf-8")
        except WorkspaceError as error:
            raise TaskError(error.code, error.message) from None
        except UnicodeDecodeError:
            raise TaskError("reference_invalid_encoding", "仅可引用 UTF-8 文本文件。") from None
        if "\0" in content:
            raise TaskError("reference_invalid_encoding", "仅可引用 UTF-8 文本文件。")
        # 只以 CR、LF 和 CRLF 计行，末尾换行不凭空产生一行；保留行段的原始换行字节。
        lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+", content)
        count = len(lines)
        if selection.start_line is not None:
            if selection.end_line > count:
                raise TaskError("reference_invalid_range", f"文件共有 {count} 行，请调整引用范围。")
            content = "".join(lines[selection.start_line - 1:selection.end_line])
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_REFERENCE_BYTES:
            return ReferencePreview(snapshot=None, line_count=count, excerpt=encoded[:MAX_REFERENCE_BYTES].decode("utf-8", errors="ignore"))
        fields = selection.model_dump(include={"path", "start_line", "end_line"})
        version = payload_digest([self.identity, fields, hashlib.sha256(raw).hexdigest()])
        return ReferencePreview(
            snapshot=ProjectFileSnapshot(**fields, version=version, content=content, commit=self.commit),
            line_count=count,
        )

    def freeze(self, references: list[ProjectFileReference]) -> list[ProjectFileSnapshot]:
        """重新核对 references 的预览版本并返回本轮快照；任何一项失败时不保存作业。"""
        snapshots = []
        total = 0
        for reference in references:
            snapshot = self.preview(reference).snapshot
            if snapshot is None or snapshot.version != reference.version:
                raise TaskError("reference_changed", "项目文件或执行位置已变化，请重新预览引用。")
            total += len(snapshot.content.encode("utf-8"))
            if total > MAX_REFERENCE_TOTAL_BYTES:
                raise TaskError("reference_total_size", "本轮文件引用总大小超过 64 KiB，请缩小行段或移除引用。")
            snapshots.append(snapshot)
        return snapshots


    def check_access(self, snapshots: list[ProjectFileSnapshot]) -> None:
        """作业出队时复核当前授权能否使用 snapshots；只核对范围，正文继续使用接纳时版本。"""
        for snapshot in snapshots:
            try:
                self._path(snapshot.path)
            except WorkspaceError as error:
                raise TaskError(error.code, error.message) from None


async def reference_source(manager: "WebRunManager", target: ReferenceTarget) -> FileReferenceSource:
    """将预览请求 target 解析为当前登记目录、固定提交或最新轮实际工作区；不创建工作树。"""
    if target.run_id is not None:
        view = manager.get_run(target.run_id)
        session = view.session_id or target.run_id
        manager.management.require_active(str(session))
        if view.active or manager.conversations.store.read(session).run_ids[-1] != target.run_id:
            raise TaskError("reference_stale_run", "请在最新一轮结束后添加项目文件引用。")
        history = manager._read_history(target.run_id)
        manager.conversations._ready(history)
        return FileReferenceSource(manager, history.manifest.task.workspace, str(target.run_id))

    identifier = target.project_id or target.workspace_id
    sources = manager.catalog.project_presets() if target.project_id else manager.catalog.workspace_presets()
    selected = next((item for item in sources if item.id == identifier), None)
    if selected is None:
        raise TaskError("unknown_workspace", "请选择已登记的项目目录。")
    root, commit = selected.path, None
    if target.execution_mode == "isolated":
        try:
            root, commit = await asyncio.to_thread(manager.projects._resolve_base, selected, target.base_ref)
        except ProjectError as error:
            raise TaskError(error.code, error.message) from None
    return FileReferenceSource(manager, root, identifier, commit)


def install_file_reference_routes(app: "FastAPI", manager: "WebRunManager") -> None:
    """为 app 注册引用选择和历史预览；manager 始终核对同一会话和运行读取权限。"""

    @app.post("/api/file-references/list")
    async def list_references(body: ReferenceListing) -> dict:
        """列出 body 指定执行来源的一页文件；Git 与磁盘读取在线程中执行。"""
        source = await reference_source(manager, body.target)
        return await asyncio.to_thread(source.listing, body.directory, body.offset)

    @app.post("/api/file-references/preview", response_model=ReferencePreview)
    async def preview_reference(body: ReferencePreviewRequest) -> ReferencePreview:
        """从 body 指定的来源预览，返回按行或整文件的核对版本；不写项目文件。"""
        source = await reference_source(manager, body.target)
        return await asyncio.to_thread(source.preview, body.selection)

    @app.get("/api/runs/{run_id}/file-references/{index}", response_model=ProjectFileSnapshot)
    async def historical_reference(run_id: UUID, index: int) -> ProjectFileSnapshot:
        """返回 run_id 保存的第 index 项引用，index 从零开始；不存在或已删除拒绝。"""
        from agent_harness.web.service import WebServiceError

        manager.get_run(run_id)
        snapshots = manager._read_history(run_id).manifest.task.file_references
        if not 0 <= index < len(snapshots):
            raise WebServiceError(404, "reference_not_found", "这一轮没有该项目文件引用。")
        return snapshots[index]
