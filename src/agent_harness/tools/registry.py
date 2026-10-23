"""注册实际可用的工具，统一参数校验、错误结果、产物与清理状态。"""

import asyncio
from pathlib import Path

from pydantic import ValidationError

from ..context_archive import ContextArchive, ReadContextArguments
from ..processes import ProcessRunner
from ..schemas import RunConfig
from ..store import StorageError
from ..workspace import WorkspaceError
from .command import RunCommandArguments, run_command
from .files import ReadFileArguments, SearchFilesArguments, WriteFileArguments, read_file, search_files, write_file


_TOOL_SPECS = {
    "read_context": (ReadContextArguments, "只读ctx引用的历史证据，按UTF-8字节分页。使用next_offset继续；不会执行历史命令，也不代表当前文件状态。"),
    "read_file": (ReadFileArguments, "只读工作区 UTF-8 文件；offset/max_bytes 按字节计数。如 eof=false，使用 next_offset 继续读取直到完成。"),
    "search_files": (SearchFilesArguments, "在工作区按字面量搜索 UTF-8 文本，返回相对路径与行号；结果或扫描截断会标记，不能当作完整搜索。"),
    "write_file": (WriteFileArguments, "写入工作区 UTF-8 文件，默认禁止覆盖；修改已有文件必须显式 overwrite=true。写入有大小上限。"),
    "run_command": (RunCommandArguments, "用 argv 数组在工作区运行程序，不自动解析 Shell；使用 python 执行项目解释器，返回退出码、输出、超时与清理状态。"),
}
_CLEANUP_RANK = {"not_needed": 0, "complete": 1, "unknown": 2, "incomplete": 3}


def _error(code: str, message: str) -> dict:
    """构造不含原始输入和宿主异常正文的结构化工具错误。"""
    return {"ok": False, "data": None, "error": {"code": code, "message": message}, "truncated": False}


class ToolExecutor:
    """公开已启用工具，维护本次运行的产物索引与最坏清理状态。"""

    def __init__(self, workspace: Path, runs_root: Path, config: RunConfig) -> None:
        """固定工作区、记录根与配置；命令执行必须稍后绑定真实运行目录。"""
        self.workspace = workspace.resolve()
        self.runs_root = runs_root.resolve()
        self.config = config
        self.run_dir: Path | None = None
        self.process_runner: ProcessRunner | None = None
        self.cleanup_status = "not_needed"
        self.artifact_refs: list[dict] = []
        self.cancelled_result: dict | None = None
        self.context_archive: ContextArchive | None = None  # 压缩启用时绑定本轮证据归档，只开放只读引用。

    def bind_run(self, run_dir: Path) -> None:
        """绑定已创建的运行记录目录，让命令输出仅保存在本次 artifacts 下。"""
        resolved = run_dir.resolve(strict=True)
        if not resolved.is_relative_to(self.runs_root):
            raise StorageError("命令输出目录必须属于运行记录根目录。")
        self.run_dir = resolved
        self.process_runner = ProcessRunner(self.config.limits, resolved / "artifacts")

    def bind_context(self, archive: ContextArchive) -> None:
        """绑定当前运行archive；返回None，不开放任意路径，不改变原有文件或命令权限。"""
        self.context_archive = archive

    def _enabled_names(self) -> list[str]:
        """返回实际工具名；启用压缩时自动提供只读read_context，额外定义参与输入计量。"""
        names = list(self.config.tools)
        if self.config.context.compression != "off":
            names.append("read_context")
        return names

    @property
    def definitions(self) -> list[dict]:
        """只生成本次配置启用的工具声明，参数 Schema 来自同一校验类。"""
        return [{"type": "function", "function": {"name": name, "description": _TOOL_SPECS[name][1], "parameters": _TOOL_SPECS[name][0].model_json_schema()}}
                for name in self._enabled_names() if name in _TOOL_SPECS]

    def _observe(self, result: dict) -> dict:
        """聚合已返回的产物与最坏清理状态，后续正常结果不能覆盖清理问题。"""
        data = result.get("data") or {}
        status = data.get("cleanup_status", "not_needed")
        if status in _CLEANUP_RANK and _CLEANUP_RANK[status] > _CLEANUP_RANK[self.cleanup_status]:
            self.cleanup_status = status
        for reference in result.get("artifact_refs", []):
            if reference not in self.artifact_refs:
                self.artifact_refs.append(reference)
        return result

    def _command_result(self, data: dict) -> dict:
        """把进程结果转成统一工具结果，同时保留取消、超时和原始输出引用。"""
        code = data.get("error_code")
        if data.get("cleanup_status") in {"unknown", "incomplete"}:
            code = code or "command_cleanup_failed"
        if data.get("cancelled"):
            code = code or "command_cancelled"
        if data.get("timed_out"):
            code = code or "command_timeout"
        if data.get("output_limit_exceeded"):
            code = code or "command_output_limit"
        if data.get("exit_code") not in {0, None}:
            code = code or "command_nonzero_exit"
        references = []
        for key, kind in (("stdout_ref", "stdout"), ("stderr_ref", "stderr")):
            if data.get(key):
                references.append({"location": "run", "path": data[key], "kind": kind})
        return self._observe({
            "ok": code is None and data.get("exit_code") == 0,
            "data": data,
            "error": None if code is None else {"code": code, "message": "命令未正常完成，请检查输出、结束原因与清理状态。"},
            "truncated": bool(data.get("truncated")),
            "artifact_refs": references,
        })

    async def execute(self, name: str, arguments_json: str) -> dict:
        """严格校验并执行一个工具；业务错误回传模型，存储错误立即上抛。"""
        self.cancelled_result = None
        if name not in _TOOL_SPECS or name not in self._enabled_names():
            return _error("unknown_tool", "该工具未启用，请使用当前工具声明中的名称。")
        try:
            arguments = _TOOL_SPECS[name][0].model_validate_json(arguments_json)
        except (ValidationError, TypeError, ValueError):
            return _error("invalid_arguments", "工具参数无效，请按工具 Schema 提供 JSON 对象。")
        try:
            if name == "read_context":
                if self.context_archive is None:
                    return _error("context_not_ready", "历史证据尚未绑定。")
                data = self.context_archive.read(arguments)
                return {"ok": True, "data": data, "error": None, "truncated": not data["eof"]}
            if name == "run_command":
                if self.process_runner is None:
                    return _error("command_not_ready", "命令工具尚未绑定运行目录。")
                data = await run_command(self.workspace, self.runs_root, arguments, self.process_runner)
                return self._command_result(data)
            if name == "read_file":
                data = read_file(self.workspace, self.runs_root, arguments, self.config.limits.tool_output_preview_chars)
                return {"ok": True, "data": data, "error": None, "truncated": not data["eof"]}
            if name == "search_files":
                data = search_files(self.workspace, self.runs_root, arguments, self.config.limits.tool_output_preview_chars)
                return {"ok": True, "data": data, "error": None, "truncated": data["truncated"]}
            data = write_file(self.workspace, self.runs_root, arguments, self.config.limits.max_file_write_bytes)
            return self._observe({"ok": True, "data": data, "error": None, "truncated": False,
                                  "artifact_refs": [{"location": "workspace", "path": data["path"], "kind": "file"}]})
        except asyncio.CancelledError as error:
            data = getattr(error, "harness_process_result", None)
            if isinstance(data, dict):
                self.cancelled_result = self._command_result(data)
            raise
        except StorageError:
            raise
        except WorkspaceError as error:
            return _error(error.code, error.message)
        except Exception:
            return _error("tool_execution_error", "工具执行失败，请检查输入与文件状态。")
