"""注册实际可用的工具，统一参数校验、错误结果、产物与清理状态。"""

import asyncio
import traceback
from pathlib import Path

from pydantic import ValidationError

from agent_harness.context.archive import ContextArchive, ReadContextArguments
from agent_harness.execution.processes import ProcessRunner
from agent_harness.hooks.controller import HookPending
from agent_harness.work.planning import PLANNING_TOOLS, PlanningController, PlanningError
from agent_harness.execution.python import PythonExecutionError, prepare_python_execution, validate_python_execution
from agent_harness.schemas import PythonExecution, RunConfig
from agent_harness.storage.runs import StorageError
from agent_harness.work.tasks import TASK_TOOLS, TaskController, TaskError
from agent_harness.memory.service import MEMORY_TOOLS, MemoryController, MemoryError
from agent_harness.subagents.service import SUBAGENT_TOOLS, SubagentController, SubagentError
from agent_harness.teams.models import TEAM_TOOLS
from agent_harness.subagents.profiles import READONLY_TOOLS
from agent_harness.execution.workspace import WorkspaceError
from agent_harness.tools.command import RunCommandArguments, run_command
from agent_harness.execution.commands import COMMAND_TOOLS
from agent_harness.tools.listing import ListFilesArguments, list_files
from agent_harness.tools.files import (EditFileArguments, ReadFileArguments, SearchFilesArguments, WriteFileArguments,
                    edit_file, read_file, search_files, write_file)


_TOOL_SPECS = {
    **COMMAND_TOOLS,
    **TEAM_TOOLS,
    **SUBAGENT_TOOLS,
    **MEMORY_TOOLS,
    **TASK_TOOLS,
    **PLANNING_TOOLS,
    "list_files": (ListFilesArguments, "只读列举工作区目录的直接子项，无需执行命令。不递归；对子目录继续传directory。offset/limit分页，next_offset为空表示无下一页；truncated或skipped表示并非完整目录。"),
    "read_context": (ReadContextArguments, "只读ctx引用的历史证据，按UTF-8字节分页。使用next_offset继续；不会执行历史命令，也不代表当前文件状态。"),
    "read_file": (ReadFileArguments, "只读工作区UTF-8文件；offset/max_bytes按字节，next_offset用于继续分页。sha256是完整原字节指纹，newline是完整文件的lf/crlf/cr/mixed/none；超出编辑预算时两者为null，不可edit_file。"),
    "search_files": (SearchFilesArguments, "在工作区按字面量搜索 UTF-8 文本，返回相对路径与行号；结果或扫描截断会标记，不能当作完整搜索。"),
    "edit_file": (EditFileArguments, "局部修改已有UTF-8文件：先read_file取得sha256，用expected_sha256校验完整版本。old_text须唯一匹配；统一LF/CRLF等价并保持原格式，mixed/cr须提供原换行。new_text可为空。file_changed或edit_not_found时重读核对，edit_ambiguous时增加上下文；禁止原样盲重试或覆盖绕过冲突。"),
    "write_file": (WriteFileArguments, "写入工作区 UTF-8 文件，默认禁止覆盖；修改已有文件优先使用 edit_file；整文件覆盖必须显式 overwrite=true，不具备版本冲突检查。写入有大小上限。"),
    "run_command": (RunCommandArguments, "用argv数组在工作区运行程序，不自动解析Shell；python/python3使用本轮项目解释器和源码目录。background=true返回命令句柄，用read_command/wait_command/stop_command管理；目录有活动命令时不能写文件或再启动命令。接纳不代表成功，实际结束回执才可作证据。"),
}
_CLEANUP_RANK = {"not_needed": 0, "complete": 1, "unknown": 2, "incomplete": 3}


def _error(code: str, message: str) -> dict:
    """构造不含原始输入和宿主异常正文的结构化工具错误。"""
    return {"ok": False, "data": None, "error": {"code": code, "message": message}, "truncated": False}


class ToolExecutor:
    """公开已启用工具，维护本次运行的产物索引与最坏清理状态。"""

    def __init__(self, workspace: Path, runs_root: Path, config: RunConfig, *, python_execution: PythonExecution | None = None, readonly: frozenset[str] | None = None) -> None:
        """固定 workspace、runs_root、config 和 Python 快照；返回 None，不启动命令。

        python_execution=None 时按现有根目录/src 捕获；非空时沿用宿主当轮快照。
        命令输出必须稍后通过 bind_run 绑定，目录准备错误抛 PythonExecutionError。
        readonly=None为普通执行；非空集合表示子执行宿主白名单，始终与只读工具集合取交集。
        """
        self.workspace = workspace.resolve()
        self.runs_root = runs_root.resolve()
        self.config = config
        self.python_execution = python_execution or prepare_python_execution(self.workspace)  # 命令使用固定源码目录，不从验收规则推断。
        self.run_dir: Path | None = None
        self.process_runner: ProcessRunner | None = None
        self.cleanup_status = "not_needed"
        self.artifact_refs: list[dict] = []
        self.cancelled_result: dict | None = None
        self.context_archive: ContextArchive | None = None  # 压缩启用时绑定本轮证据归档，只开放只读引用。
        self.planning: PlanningController | None = None  # Runner绑定后开放规划工具并执行宿主只读限制。
        self.tasks: TaskController | None = None  # 当前会话工作项及宿主校验入口。
        self.memory: MemoryController | None = None  # 当前项目固定读取集，模型只能提出候选。
        self.memory_phase = "execution"  # 宿主在模型请求前更新，验收修复阶段为 verification。
        self.readonly = None if readonly is None else frozenset(name for name in readonly if name in READONLY_TOOLS or name in {item.name for item in config.mcp_tools})  # 外部研究权限还由本轮控制器核验。
        self.delegation_tools: frozenset[str] | None = None  # 开发子项的宿主工具上限，None为根执行。
        self.project_manager = None  # Runner绑定统一工作树与成果管理器，子执行不自行选择路径。
        self.subagents: SubagentController | None = None  # 仅根Agent绑定；子执行没有委派或管理权限。
        self.team = None  # 根执行绑定团队编排，开发成员仅绑定消息与读取权限。
        self.report_only = False  # 开发报告纠正后永久关闭本子执行工具，不能借格式重试重复修改。
        self.handoff = False  # 宿主末轮交接时关闭全部工具；提示和模型工具声明之外再执行一次宿主拒绝。
        self.permissions = None  # Runner绑定的统一授权控制器；独立工具调用仍由既有边界约束。
        self.before_execute = None  # 参数和权限检查通过后记录真实开始，None表示无需事件。
        self.hooks = None  # Agent 初始化后绑定的生命周期调度器；工具单测及无配置场景不运行脚本。
        self.call_id = ""  # 当前工具编号，仅供动作追溯，不授予权限。
        self.commands = None  # Runner 绑定的统一命令生命周期，独立工具测试为空。
        self.mcp = None  # Agent 绑定的固定 MCP 目录，未选择工具时不连接服务。

    def bind_planning(self, planning: PlanningController) -> None:
        """绑定当前Run的planning权限与事件入口；返回None，不改变已配置的文件工具集合。"""
        self.planning = planning

    def bind_python_execution(self, execution: PythonExecution) -> None:
        """绑定宿主清单中的 execution 快照，返回 None；仅更新执行器配置，不启动命令。

        工作区不一致或目录已变化时抛 PythonExecutionError；保存深副本，避免执行器
        修改自身配置时改变已经固定的运行清单。宿主注入执行器与默认构造共用此入口。
        """
        if execution.workspace != self.workspace:
            raise PythonExecutionError("工具执行器与当轮 Python 工作区不一致。")
        validate_python_execution(execution)
        self.python_execution = execution.model_copy(deep=True)

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
        if self.mcp is not None:
            names.extend(self.mcp.entries)
        if self.handoff:
            return []
        if self.readonly is not None:
            return sorted(self.readonly)
        if self.delegation_tools is not None:
            return sorted(self.delegation_tools | (frozenset(COMMAND_TOOLS) if self.commands is not None and "run_command" in self.delegation_tools else frozenset()))
        if self.commands is not None and "run_command" in names:
            names.extend(COMMAND_TOOLS)
        if self.config.context.compression != "off":
            names.append("read_context")
        if self.planning is not None:
            names.extend(PLANNING_TOOLS)
            if "read_file" in names:
                names.append("list_files")
        if self.tasks is not None:
            names.extend(TASK_TOOLS)
        if self.memory is not None:
            if self.config.memory.use:
                names.extend(["memory_search", "memory_get"])
            if self.config.memory.capture:
                names.append("memory_propose")
        if self.subagents is not None:
            names.extend(SUBAGENT_TOOLS)
        if self.team is not None:
            names.extend(self.team.names)
        return list(dict.fromkeys(names))

    @property
    def definitions(self) -> list[dict]:
        """只生成本次配置启用的工具声明，参数 Schema 来自同一校验类。"""
        enabled = self._enabled_names()
        builtin = [{"type": "function", "function": {"name": name, "description": _TOOL_SPECS[name][1], "parameters": _TOOL_SPECS[name][0].model_json_schema()}}
                   for name in enabled if name in _TOOL_SPECS]
        return builtin + ([item for item in self.mcp.definitions if item["function"]["name"] in enabled] if self.mcp else [])

    def external_title(self, name: str) -> str | None:
        """返回本轮外部 name 的服务/工具名称；内置或未开放工具返回 None，不回查当前目录。"""
        item = self.mcp.entries.get(name) if self.mcp is not None else None
        return f"{item['record']['config']['label']} / {item['tool']['name']}" if item else None

    async def _execute_mcp(self, name: str, arguments_json: str, *, hooked: bool) -> dict:
        """校验本轮外部 name/arguments_json 后统一审批及 Hook；返回回执，未获准前不连接服务。"""
        if self.mcp is None or name not in self._enabled_names() or self.permissions is None:
            return _error("unknown_tool", "该外部工具未在本轮开放。")
        arguments = self.mcp.validate(name, arguments_json)
        if self.permissions.decision(name) == "deny":
            return _error("permission_denied", "宿主、研究、子执行或自动化权限禁止此外部工具。")
        if not self.mcp.research(name) and (self.commands is not None and self.commands.workspace in self.commands.gates or self.subagents is not None and self.subagents.active):
            return _error("workspace_command_active", "请先收拢活动命令和子执行，再使用可能修改现场的外部工具。")
        if self.hooks is not None and self.hooks.definitions and not hooked:
            async def original_operation():
                """重新校验同一外部调用；返回回执，不沿用 Hook 前的授权决定。"""
                return await self._execute_mcp(name, arguments_json, hooked=True)
            return await self.hooks.tool(name, arguments_json, original_operation)
        pending = self.permissions.check(name, arguments, self.call_id, hook_operation=self.hooks.origin if self.hooks else None)
        if pending:
            return {"ok": False, "data": {"action_id": pending["id"]}, "error": {"code": "approval_required", "message": "外部工具尚未调用，等待本次参数批准。"}, "truncated": False}
        if self.team is not None:
            self.team.check_member()
        if self.before_execute:
            self.before_execute()
        if self.hooks:
            self.hooks.started()
        return await self.mcp.execute(name, arguments)

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
        """在任务局部目录权限内执行 name/arguments_json，返回统一回执；不改变其他并发任务范围。"""
        from agent_harness.permissions.policy import FILE_SCOPE
        token = None
        if self.permissions is not None:
            allowed = self.permissions.write_roots if name in {"write_file", "edit_file"} else self.permissions.read_roots
            token = FILE_SCOPE.set((self.workspace, allowed))
        try:
            return await self._execute(name, arguments_json)
        except HookPending as pending:
            action = pending.action
            origin = action["hook"]["origin"]
            _, state = self.hooks.checkpoint(origin)
            return {"ok": False, "data": {"action_id": action["id"], "tool_executed": "receipt" in state}, "error": {"code": "approval_required", "message": "Hook 等待本次命令批准。"}, "truncated": False}
        except TaskError as error:
            result = _error(error.code, error.message)
            receipt = getattr(error, "hook_tool_receipt", None)
            if receipt is not None:
                result["data"] = {"tool_executed": True, "original_result": receipt}
            return result
        finally:
            if token is not None:
                FILE_SCOPE.reset(token)

    async def _execute(self, name: str, arguments_json: str, *, hooked: bool = False) -> dict:
        """严格校验并执行一个工具；业务错误回传模型，存储错误立即上抛。"""
        self.cancelled_result = None
        if self.handoff:
            return _error("handoff_tools_disabled", "收尾交接阶段只允许总结已有证据，不执行任何新工具。")
        if name.startswith("mcp_"):
            return await self._execute_mcp(name, arguments_json, hooked=hooked)
        if self.commands is not None and self.commands.workspace in self.commands.gates and name in {"write_file", "edit_file", "run_command", "delegate_readonly", "delegate_develop", "team_dispatch", "enter_plan_mode", "submit_plan"}:
            return _error("workspace_command_active", "当前工作区有活动命令，请先等待或停止，再修改、检查、委派或切换计划。")
        if self.readonly is not None and name not in self.readonly:
            return _error("subagent_read_only", "子Agent仅允许父级授予的读取和检索，不能修改文件、执行命令或委派。")
        if self.subagents is not None and self.subagents.readers_active and name in {"write_file", "edit_file", "run_command", "memory_propose", "enter_plan_mode", "submit_plan"}:
            return _error("subagent_readers_active", "子研究仍在读取共享目录，请先等待或停止，再执行变更或切换计划。")
        # 计划切换会撤销当前执行依据，必须先收拢使用该依据的开发子项。
        if self.subagents is not None and self.subagents.active and name in {"enter_plan_mode", "submit_plan"}:
            return _error("subagent_developers_active", "开发子项仍在执行，请先等待或停止，再切换计划。")
        if name not in _TOOL_SPECS or name not in self._enabled_names():
            return _error("unknown_tool", "该工具未启用，请使用当前工具声明中的名称。")
        try:
            arguments = _TOOL_SPECS[name][0].model_validate_json(arguments_json)
        except (ValidationError, TypeError, ValueError):
            return _error("invalid_arguments", "工具参数无效，请按工具 Schema 提供 JSON 对象。")
        if self.hooks is not None and self.hooks.definitions and not hooked:
            if self.planning is not None:
                try:
                    self.planning.check_permission(name)
                except PlanningError as error:
                    return _error(error.code, error.message)
            if self.permissions is not None and self.permissions.decision(name) == "deny":
                return _error("permission_denied", "宿主、会话或规划权限禁止此操作。")

            async def original_operation():
                """重新核验并执行当前 name/arguments_json；返回原回执，避免 Hook 后沿用旧目标身份。"""
                return await self._execute(name, arguments_json, hooked=True)

            return await self.hooks.tool(name, arguments_json, original_operation)
        try:
            if self.team is not None:
                self.team.check_member()
            if self.planning is not None:
                self.planning.check_permission(name)
            if self.permissions is not None:
                pending = self.permissions.check(name, arguments.model_dump(mode="json"), self.call_id, hook_operation=self.hooks.origin if self.hooks is not None else None)
                if pending is not None:
                    return {"ok": False, "data": {"action_id": pending["id"]}, "error": {"code": "approval_required", "message": "尚未执行，等待用户批准本次动作。"}, "truncated": False}
            if self.memory is not None and name not in PLANNING_TOOLS:
                phase = "planning" if self.planning is not None and self.planning.state.phase == "planning" else self.memory_phase
                self.memory.before_tool(name, arguments, phase)
            if self.before_execute is not None:
                self.before_execute()
            if self.hooks is not None:
                self.hooks.started()
            if self.planning is not None:
                if name in PLANNING_TOOLS:
                    data = self.planning.execute(name, arguments)
                    return {"ok": True, "data": data, "error": None, "truncated": name == "read_plan" and not data["eof"]}
            if name in TEAM_TOOLS and self.team is not None:
                data = await self.team.execute(name, arguments)
                return {"ok": True, "data": data, "error": None, "truncated": False}
            if name in SUBAGENT_TOOLS and self.subagents is not None:
                data = await self.subagents.execute(name, arguments)
                child = data.get("subagent")
                ok = data.get("accepted", False) or child is None or (child["status"] == "completed" and not child["report"]["partial"])
                return {"ok": ok, "data": data, "error": None if ok else {"code": "subagent_not_completed", "message": "子研究未完成，请核对状态和轨迹。"},
                        "truncated": not data.get("eof", True)}
            if name == "read_context":
                if self.context_archive is None:
                    return _error("context_not_ready", "历史证据尚未绑定。")
                data = self.context_archive.read(arguments)
                return {"ok": True, "data": data, "error": None, "truncated": not data["eof"]}
            if name in MEMORY_TOOLS and self.memory is not None:
                data = self.memory.execute(name, arguments)
                return {"ok": True, "data": data, "error": None, "truncated": name == "memory_get" and not data["eof"]}
            if name in TASK_TOOLS and self.tasks is not None:
                data = self.tasks.execute(name, arguments)
                state = self.tasks.snapshot()
                if name in {"task_create", "task_update"}:
                    self.planning.emit("tasks_snapshot", {"state": state, "planning": self.planning.state.model_dump(mode="json")})
                return {"ok": True, "data": data, "error": None, "truncated": False}
            if name == "list_files":
                data = list_files(self.workspace, self.runs_root, arguments)
                return {"ok": True, "data": data, "error": None, "truncated": data["truncated"]}
            if name == "run_command":
                if self.process_runner is None:
                    return _error("command_not_ready", "命令工具尚未绑定运行目录。")
                if self.commands is not None:
                    data = await self.commands.start(arguments)
                    if data.get("accepted"):
                        return {"ok": True, "data": data, "error": None, "truncated": False}
                else:
                    data = await run_command(self.workspace, self.runs_root, arguments, self.process_runner, self.python_execution)
                return self._command_result(data)
            if name in COMMAND_TOOLS and self.commands is not None:
                if name == "read_command":
                    from agent_harness.execution.commands import output_page
                    record = self.commands.get(arguments.command_id)
                    return {"ok": True, "data": {"command": record, "output": output_page(self.run_dir, record, arguments.stream, arguments.offset, arguments.max_bytes)}, "error": None, "truncated": False}
                data = await (self.commands.wait(arguments.command_id) if name == "wait_command" else self.commands.stop(arguments.command_id))
                return self._command_result(data)
            if name == "read_file":
                data = read_file(self.workspace, self.runs_root, arguments, self.config.limits.tool_output_preview_chars, self.config.limits.max_file_write_bytes)
                return {"ok": True, "data": data, "error": None, "truncated": not data["eof"]}
            if name == "search_files":
                data = search_files(self.workspace, self.runs_root, arguments, self.config.limits.tool_output_preview_chars)
                return {"ok": True, "data": data, "error": None, "truncated": data["truncated"]}
            if name == "edit_file":
                data = edit_file(self.workspace, self.runs_root, arguments, self.config.limits.max_file_write_bytes)
            else:
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
        except HookPending:
            raise
        except SubagentError as error:
            return _error(error.code, error.message)
        except MemoryError as error:
            return _error(error.code, error.message)
        except TaskError as error:
            result = _error(error.code, error.message)
            if error.code == 'task_revision_conflict' and self.tasks is not None:
                current = self.tasks.service.get(arguments.task_id)
                result['data'] = {"current_task": {key: current[key] for key in ('id', 'revision', 'status', 'blocked_reason')}}
            return result
        except PlanningError as error:
            return _error(error.code, error.message)
        except PythonExecutionError as error:
            return _error("python_execution_changed", str(error))
        except WorkspaceError as error:
            return _error(error.code, error.message)
        except Exception as error:
            if self.commands is not None:
                frames = traceback.extract_tb(error.__traceback__)[-6:]
                self.commands.context.emit("tool_runtime_error", {"name": name, "error_type": type(error).__name__,
                    "trace": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno} for frame in frames]})
            return _error("tool_execution_error", "工具执行失败，请检查输入与文件状态。")
