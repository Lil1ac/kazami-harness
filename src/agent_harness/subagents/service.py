"""受控委派：只读共享目录与隔离开发，共享父预算、动作审批与生命周期。"""

import asyncio
from copy import copy
import codecs
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import Field

from agent_harness.providers.base import ModelError
from agent_harness.schemas import DataModel, DelegationOrigin, RunManifest, RunResult, SubagentRecord
from agent_harness.storage.runs import RunHistory, RunStore, StorageError
from agent_harness.subagents.profiles import PROFILES
from agent_harness.subagents.reports import finalize_report
from agent_harness.hooks.controller import HookPending
from agent_harness.work.tasks import TaskError

if TYPE_CHECKING:
    from agent_harness.context.builder import ContextBuilder
    from agent_harness.providers.base import ModelClient
    from agent_harness.runtime.runner import RunContext
    from agent_harness.tools.registry import ToolExecutor


class DelegateReadonly(DataModel):
    """主 Agent 明确指定的子研究范围和预算；上限仍由父级剩余额度裁剪。"""

    title: str = Field(min_length=1, max_length=120, description="简短非空委派标题。")
    objective: str = Field(min_length=1, max_length=8000, description="完整研究问题、范围与必要背景；子执行不会看到父聊天历史。")
    role: Literal["explore", "review"] = Field(default="explore", description="explore探索调用关系；review审阅风险，均为只读。")
    task_id: UUID | None = Field(default=None, description="可选同会话工作项编号；仅关联，不认领或完成该项。")
    skill_names: list[str] = Field(default_factory=list, max_length=8, description="明确委派给子角色的技能名，只能选父本轮已冻结技能；默认空，不自动继承。")
    max_model_turns: int = Field(default=4, ge=1, le=12, description="子执行最多模型轮数，默认4，包含摘要，不增加父额度。")
    max_tool_calls: int = Field(default=8, ge=1, le=30, description="子执行最多工具次数，默认8，非法调用也占用。")
    background: bool = Field(default=False, description="False等待本次报告；True异步启动，最多两个活动子任务，随后用wait_subagents等待。")
    timeout_seconds: float = Field(default=60.0, gt=0, le=180, allow_inf_nan=False, description="子执行总秒数，默认60；父级截止时间优先。")


class DelegateDevelop(DelegateReadonly):
    """批准方案内的隔离开发工作项；任务、写入范围及完成条件由宿主冻结核验。"""

    role: Literal["develop"] = "develop"  # 开发角色固定，不接受只读角色伪装。
    task_id: UUID = Field(description="当前父会话内可执行的工作项编号，必须有非空完成条件。")
    write_scope: list[str] = Field(min_length=1, max_length=32, description="可修改的仓库相对文件或目录前缀，不支持通配符、控制目录及父级穿越。")
    max_model_turns: int = Field(default=8, ge=1, le=12, description="开发子项轮数上限，默认8，含总结且共享父预算。")
    max_tool_calls: int = Field(default=20, ge=1, le=30, description="开发子项工具上限，默认20，含失败调用。")
    timeout_seconds: float = Field(default=180.0, gt=0, le=180, allow_inf_nan=False, description="子执行活动秒数上限，默认180，父剩余时限优先。")


class ReadSubagent(DataModel):
    """只读当前父运行已登记的子报告；不回读整个子对话或启动新执行。"""

    run_id: UUID = Field(description="delegate_readonly返回的子运行编号，只能属于当前父运行。")
    offset: int = Field(default=0, ge=0, description="报告UTF-8字节偏移；首次0，其后使用next_offset。")
    max_bytes: int = Field(default=8192, ge=4, le=8192, description="每页最大UTF-8字节数，默认8192，最小4避免字符内死循环。")


class WaitSubagents(DataModel):
    """选择直属子项等待；超时仅返回进度，不取消仍在研究的子项。"""

    run_ids: list[UUID] = Field(default_factory=list, max_length=8, description="待观察子编号，默认空表示本轮全部；跨父编号拒绝。")
    mode: Literal["all", "any"] = Field(default="all", description="all等全部终态，any等任一终态。")
    timeout_seconds: float = Field(default=30.0, ge=0, le=60, allow_inf_nan=False, description="等待秒数；0仅查询，最长60，父总截止优先。")


class CancelSubagent(DataModel):
    """停止一个直属子任务，重复停止返回原终态，不影响其他子项。"""

    run_id: UUID = Field(description="本轮登记的直属子编号。")


SUBAGENT_TOOLS = {
    "delegate_develop": (DelegateDevelop, "在已批准方案内委派当前会话工作项进行隔离开发。必须有完成条件和明确write_scope；固定父实际代码，子Agent独立修改、执行受审批命令并交付快照。最多两个活动子项，共享父预算；成果须经人工审阅集成，报告返回不代表任务完成或测试通过。"),
    "delegate_readonly": (DelegateReadonly, "委派独立只读探索/审阅；background=true异步启动并返回编号，最多两个活动子任务；技能须显式选择父级快照。返回精简结构报告和核验来源，partial表示未完整交接。共用原预算并为父保留收尾额度；返回后同批剩余调用不执行，须阅读结果后重新决定。报告不代表父任务完成或验收通过。"),
    "wait_subagents": (WaitSubagents, "查询或等待直属子项，返回各自真实状态和已固定报告；等待超时不取消研究。"),
    "cancel_subagent": (CancelSubagent, "单独停止一个直属子任务并等待封存，不影响其他子任务；不能继续或重新执行。"),
    "read_subagent": (ReadSubagent, "只读当前Run内已登记子报告，按UTF-8字节分页；失败/中断不视为成功。不会重新运行子Agent或注入完整子对话。"),
}


class SubagentError(Exception):
    """向工具层返回受控委派错误，不包含供应商或存储异常正文。"""

    def __init__(self, code: str, message: str):
        """保存稳定code和中文message；返回None，不创建子执行。"""
        super().__init__(message)
        self.code = code  # 工具可识别的原因码。
        self.message = message  # 可直接展示的说明。


def read_child(root: Path, parent_id: UUID, child_id: UUID) -> RunHistory:
    """只读root中parent_id直属的child_id日志；返回可信前缀，归属或路径异常抛StorageError。

    root为真实运行根，不接受任意子路径；不追随指向父运行外部的日志目录替换。
    此函数不恢复执行、不打开项目文件，也不把子执行登记为独立会话。
    """
    parent = root.resolve() / str(parent_id)
    children = parent / "subagents"
    try:
        if children.resolve() != children or (children / str(child_id)).resolve() != children / str(child_id):
            raise StorageError("子执行记录目录身份发生变化。")
        history = RunStore.read(children, str(child_id))
        if history.manifest.delegation is None or history.manifest.delegation.parent_run_id != parent_id:
            raise StorageError("子执行不属于当前父运行。")
        return history
    except (OSError, RuntimeError):
        raise StorageError("子执行记录不可用。") from None


def report_page(history: RunHistory, offset: int, max_bytes: int) -> dict:
    """从history终态报告返回UTF-8安全页；offset为字节偏移，max_bytes至少4。

    报告未完成时返回空正文及真实状态；偏移越界或落在字符内部抛SubagentError，不重放工具。
    """
    report = history.result.subagent_report if history.result else None
    raw = report.model_dump_json().encode("utf-8") if report is not None else b""
    if offset > len(raw):
        raise SubagentError("subagent_invalid_offset", "报告偏移超过末尾，请使用上一页next_offset。")
    try:
        raw[:offset].decode("utf-8")
    except UnicodeDecodeError:
        raise SubagentError("subagent_invalid_offset", "报告偏移位于字符内部，请使用上一页next_offset。") from None
    decoder = codecs.getincrementaldecoder("utf-8")()
    text = decoder.decode(raw[offset:offset + max_bytes], final=offset + max_bytes >= len(raw))
    end = offset + len(text.encode("utf-8"))
    return {"run_id": str(history.manifest.run_id), "status": history.result.execution_status if history.result else "interrupted",
            "content": text, "offset": offset, "next_offset": None if end == len(raw) else end,
            "eof": end == len(raw), "total_bytes": len(raw)}


class SubagentController:
    """父Run内的唯一委派入口；最多两个活动子项，所有生命周期变化先落盘再对外返回。"""

    def __init__(self, parent: "RunContext", client: "ModelClient", executor: "ToolExecutor"):
        """绑定parent预算、已开启client和父executor；返回None，不立即调用模型。"""
        self.parent = parent  # 共享预算与取消归属，委派不另占队列槽。
        self.client = client  # 复用同一供应商连接及模型配置，不新增凭据来源。
        self.executor = executor  # 读取父级工具白名单及当前规划、任务权限。
        self.jobs: dict[UUID, asyncio.Task] = {}  # 本轮已创建任务；终态仍保留以便幂等查询和检查异常。
        self.pending_reports: set[UUID] = set()  # 异步报告尚未通过等待/读取回执交给父模型，防止提前声称完成。
        self.cancel_reasons: dict[UUID, str] = {}  # 只有明确单项停止时使用user_cancelled，其余为parent_stopped。
        self.pause_requested = False  # 子审批出现后其他子项在模型/工具边界安全暂停。
        self.attention = asyncio.Event()  # 子审批打断all等待，避免被兄弟长请求隐藏。
        self.team = None  # 根执行绑定持久团队控制器，普通C07委派不使用团队身份。
        self.inputs: "ContextBuilder | None" = None  # 首次模型输入准备后绑定固定约定、技能和记忆。
        self.hook_lifecycle: set[tuple[str, str]] = set()  # 当段已发布的子执行起止事件，进度刷新不重复触发。

    @property
    def active(self) -> bool:
        """返回本轮是否还有未结束子协程；不读取磁盘或改变状态。"""
        return any(not task.done() for task in self.jobs.values())

    @property
    def readers_active(self) -> bool:
        """返回是否仍有读取父目录的只读子项；隔离开发不会阻止父目录编辑。"""
        return any(item.role != "develop" and item.run_id in self.jobs and not self.jobs[item.run_id].done()
                   for item in self.parent.result.subagents)

    def check_errors(self, *, waiting: bool = True) -> None:
        """检查jobs致命异常；waiting=True在父执行边界传播审批等待，网页停止查询用False；返回None。"""
        for task in self.jobs.values():
            if task.done() and not task.cancelled() and task.exception() is not None:
                raise task.exception()
        pending = next((item.pending_action for item in self.parent.result.subagents if item.pending_action is not None), None)
        if pending is not None and waiting:
            from agent_harness.runtime.runner import AgentStopped
            self.pause_requested = True
            self.executor.permissions.state.pending = pending.copy()
            self.parent.result.permissions = self.executor.permissions.snapshot()
            self.parent.emit("permission_requested", {"action": pending})
            raise AgentStopped("waiting_input", "action_approval")

    async def wait_all(self) -> list[dict]:
        """等待本轮所有jobs并返回固定快照；父取消通过外层close收拢，不丢子终态。"""
        await self._wait_jobs(list(self.jobs.values()))
        self.check_errors()
        self.pending_reports.clear()
        return [item.model_dump(mode="json") for item in self.parent.result.subagents]

    async def _wait_jobs(self, jobs: list[asyncio.Task], timeout_seconds: float | None = None, *, any_completed: bool = False) -> None:
        """等待jobs完成或审批事件；timeout_seconds=None不限额、0只查询，any_completed=True任一完成即返回。

        不取消子执行；审批在安全边界立即交回父生命周期，超时仅返回当前状态。
        """
        if any_completed and any(task.done() for task in jobs):
            return
        pending = {task for task in jobs if not task.done()}
        if not pending or timeout_seconds == 0:
            return
        deadline = None if timeout_seconds is None else asyncio.get_running_loop().time() + timeout_seconds
        attention = asyncio.create_task(self.attention.wait())
        try:
            while pending:
                timeout = None if deadline is None else max(0, deadline - asyncio.get_running_loop().time())
                done, _ = await asyncio.wait([*pending, attention], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    return
                if attention in done:
                    self.check_errors()
                pending.difference_update(done)
                if any_completed:
                    return
        finally:
            attention.cancel()
            await asyncio.gather(attention, return_exceptions=True)

    async def close(self) -> None:
        """取消并等待所有未完成子项，返回None；在父终态及客户端关闭之前封存，存储错误上抛。"""
        pausing = self.pause_requested and not asyncio.current_task().cancelling()
        for task in self.jobs.values():
            if not task.done() and not pausing:
                task.cancel()
        cancelled = None  # 重复取消只记录来源，仍须等待全部子进程和证据收拢。
        if self.jobs:
            draining = asyncio.gather(*self.jobs.values(), return_exceptions=True)
            while not draining.done():
                try:
                    await asyncio.shield(draining)
                except asyncio.CancelledError as error:
                    cancelled, pausing = error, False
                    for task in self.jobs.values():
                        if not task.done():
                            task.cancel()
        if not pausing:
            from agent_harness.permissions.store import PermissionStore
            permissions = PermissionStore(self.parent.runs_root)
            for record in self.parent.result.subagents:
                if record.pending_action is not None:
                    value = permissions.get(record.pending_action["id"])
                    if value["status"] == "pending":
                        value["status"] = "cancelled"
                        permissions.save(value)
                    record.pending_action, record.status, record.stop_reason = None, "cancelled", "parent_stopped"
                    if record.team_id is not None:
                        self.team.cancelled(record)
                    self.parent.emit("subagent_snapshot", {"subagent": record.model_dump(mode="json")})
        # 收尾不再次抛等待异常；致命存储错误仍必须向上传播。
        for task in self.jobs.values():
            if task.done() and not task.cancelled() and task.exception() is not None:
                raise task.exception()
        if cancelled is not None:
            raise cancelled

    async def cancel(self, run_id: UUID, received: bool = False) -> dict:
        """停止直属run_id并返回真实快照；received只在模型工具已接收报告时为True。

        默认False用于网页停止，仍须向父模型交接结果；未知编号抛SubagentError，终态不增加事件。
        """
        task = self.jobs.get(run_id)
        if task is None:
            raise SubagentError("subagent_not_found", "该子执行不属于当前运行。")
        if not task.done():
            self.cancel_reasons[run_id] = "user_cancelled"
            task.cancel()
        await asyncio.shield(task)
        self.check_errors(waiting=received)
        if received:
            self.pending_reports.discard(run_id)
        return {"subagent": next(item.model_dump(mode="json") for item in self.parent.result.subagents if item.run_id == run_id)}

    def bind_inputs(self, inputs: "ContextBuilder") -> None:
        """固定父级inputs引用供后续委派继承；返回None，不继承消息或子历史。"""
        self.inputs = inputs

    def _publish(self, record: SubagentRecord, child: "RunContext") -> None:
        """把child已提交计数写入record并发布父快照；返回None，存储错误立即上抛。"""
        record.model_turns, record.tool_calls, record.http_attempts = child.result.model_turns, child.result.tool_calls, child.result.http_attempts
        record.usage, record.usage_complete = child.result.usage.model_dump(), child.result.usage_complete
        record.elapsed_seconds, record.event_count = child.active_duration(), child.event_count
        record.cleanup_status = child.result.cleanup_status
        self.parent.emit("subagent_snapshot", {"subagent": record.model_dump(mode="json")})
        event = "SubagentStart" if record.status == "running" else "SubagentStop"
        identity = (str(record.run_id), event)
        if self.parent.hooks is not None and identity not in self.hook_lifecycle:
            self.hook_lifecycle.add(identity)
            self.parent.hooks.enqueue(event, str(record.run_id), {"subagent": record.model_dump(mode="json")})

    async def execute(self, name: str, arguments, *, team_assignment: dict | None = None) -> dict:
        """处理已校验name/arguments并返回委派回执；无效归属抛SubagentError。

        delegate_readonly按background选择等待或异步启动；wait_subagents查询/等待，read_subagent分页读报告。
        子超时或模型失败回传明确错误状态；父取消、父总超时和存储故障继续向上传播。
        """
        parent = self.parent
        self.check_errors()
        if name == "cancel_subagent":
            return await self.cancel(arguments.run_id, received=True)
        if name == "wait_subagents":
            identifiers = list(dict.fromkeys(arguments.run_ids or [item.run_id for item in parent.result.subagents]))
            if any(not any(item.run_id == identifier for item in parent.result.subagents) for identifier in identifiers):
                raise SubagentError("subagent_not_found", "等待集合包含不属于当前运行的子执行。")
            selected_jobs = [self.jobs[identifier] for identifier in identifiers if identifier in self.jobs]
            ready = any(identifier not in self.jobs or self.jobs[identifier].done() for identifier in identifiers)
            if selected_jobs and not (arguments.mode == "any" and ready):
                await self._wait_jobs(selected_jobs, arguments.timeout_seconds, any_completed=arguments.mode == "any")
            self.check_errors()
            self.pending_reports.difference_update(identifier for identifier in identifiers if (identifier not in self.jobs or self.jobs[identifier].done()))
            return {"items": [item.model_dump(mode="json") for item in parent.result.subagents if item.run_id in identifiers],
                    "finished": all((identifier not in self.jobs or self.jobs[identifier].done()) for identifier in identifiers)}
        if name == "read_subagent":
            if not any(item.run_id == arguments.run_id for item in parent.result.subagents):
                raise SubagentError("subagent_not_found", "该子执行不属于当前运行。")
            record = next(item for item in parent.result.subagents if item.run_id == arguments.run_id)
            page = report_page(read_child(parent.runs_root, record.log_parent_id or parent.manifest.run_id, arguments.run_id), arguments.offset, arguments.max_bytes)
            page["status"] = next(item.status for item in parent.result.subagents if item.run_id == arguments.run_id)
            if (arguments.run_id not in self.jobs or self.jobs[arguments.run_id].done()) and page["eof"]:
                self.pending_reports.discard(arguments.run_id)
            return page
        if self.inputs is None or not arguments.title.strip() or not arguments.objective.strip():
            raise SubagentError("invalid_arguments", "委派标题和研究范围不能为空。")
        if sum(not task.done() for task in self.jobs.values()) >= 2:
            raise SubagentError("subagent_concurrency_limit", "最多两个活动子任务，请先等待或停止已有子项。")
        selected = set(arguments.skill_names)
        if len(selected) != len(arguments.skill_names) or not selected.issubset({item.name for item in self.inputs.skills}):
            raise SubagentError("subagent_skill_unavailable", "子角色只能显式选择父本轮已冻结的技能，名称不能重复。")
        if len(parent.result.subagents) >= 8:
            raise SubagentError("subagent_limit", "单轮最多八次委派，请使用已有报告。")
        linked_task = None  # 只把同会话工作项范围交给子模型，不授予认领或更新权限。
        if arguments.task_id is not None:
            if self.executor.tasks is None:
                raise SubagentError("subagent_task_unavailable", "当前运行没有可关联的任务清单。")
            linked_task = self.executor.tasks.service.get(str(arguments.task_id))  # 同会话领域服务拒绝其他清单编号。
        developing = name == "delegate_develop"
        scope, review = [], None
        planning = self.executor.planning.state if self.executor.planning is not None else parent.manifest.planning
        if developing:
            from agent_harness.subagents.workspaces import scope_paths, intersect_scope
            from agent_harness.work.tasks import TaskError
            if planning.phase == "planning" or not planning.plans or planning.plans[-1].status != "approved":
                raise SubagentError("subagent_plan_required", "请先批准开发方案，再委派写入子任务。")
            policy = parent.manifest.config.permission_policy
            if self.executor.permissions.decision("write_file") == "deny" or policy.read != "allow" or "." not in policy.read_roots:
                raise SubagentError("permission_denied", "隔离开发需要完整项目读取和父级写入授权。")
            if linked_task is None or not linked_task["acceptance"]:
                raise SubagentError("subagent_task_invalid", "开发工作项必须有明确完成条件。")
            try:
                scope = intersect_scope(scope_paths(arguments.write_scope), policy.write_roots)
            except (ValueError, TaskError) as error:
                raise SubagentError(getattr(error, "code", "subagent_scope_invalid"), getattr(error, "message", "委派路径范围无效。")) from None
        limits = parent.manifest.config.limits
        available_seconds = max(0.0, parent.deadline - asyncio.get_running_loop().time())
        reserve = {"model_turns": 2.0, "tool_calls": 1.0 if {"write_file", "edit_file", "run_command"}.intersection(self.executor._enabled_names()) else 0.0,
                   "seconds": min(10.0, available_seconds / 4)}  # 保留父执行/总结与一次实际修改空间；不是延长总预算。
        turns = min(arguments.max_model_turns, parent.available("model_turns") - int(reserve["model_turns"]))
        calls = min(arguments.max_tool_calls, parent.available("tool_calls") - int(reserve["tool_calls"]))
        seconds = min(arguments.timeout_seconds, available_seconds - reserve["seconds"])
        if turns < 1 or calls < 1 or seconds <= 0:
            raise SubagentError("subagent_budget_exhausted", "为父任务保留收尾额度后剩余预算不足，未创建子执行。")
        from agent_harness.runtime.runner import RunContext
        from agent_harness.tools.registry import ToolExecutor

        profile = PROFILES[arguments.role]
        allowed = profile.tools.intersection(self.executor._enabled_names())
        allowed = allowed.union(name for name in self.executor._enabled_names() if name.startswith("mcp_") and self.executor.mcp is not None and self.executor.mcp.research(name))  # 只继承父已选且人工审核的研究工具。
        if team_assignment is not None:
            from agent_harness.teams.models import MEMBER_TOOLS
            allowed = allowed.union(MEMBER_TOOLS)
        config = parent.manifest.config.model_copy(deep=True)
        config.tools = [tool for tool in config.tools if tool in allowed]
        config.mcp_tools = [item for item in config.mcp_tools if item.name in allowed]
        config.skills = list(arguments.skill_names)
        config.memory.capture, config.auto_repair = False, False
        config.limits = limits.model_copy(update={"max_model_turns": turns, "max_tool_calls": calls, "run_timeout_seconds": seconds})
        config.system_prompt = profile.system_prompt
        planning = self.executor.planning.state if self.executor.planning is not None else parent.manifest.planning
        prompt = json.dumps({"父轮要求": parent.manifest.task.prompt, "批准范围": [plan.model_dump(mode="json") for plan in planning.plans if plan.status == "approved"],
                             "关联工作项": {key: linked_task[key] for key in ("id", "subject", "description", "acceptance")} if linked_task else None,
                             "研究任务": arguments.objective, "成员历史交接摘要": team_assignment.get("member_summary", "") if team_assignment else None}, ensure_ascii=False)
        child_id = uuid4()
        if team_assignment is not None:
            self.team.preparing(team_assignment, child_id)
        workspace = parent.manifest.task.workspace
        execution = parent.manifest.task.python_execution
        if developing:
            from agent_harness.subagents.workspaces import prepare_workspace
            from agent_harness.runtime.runner import _file_operation
            from agent_harness.execution.python import prepare_python_execution
            from agent_harness.work.tasks import TaskError
            from agent_harness.projects.manager import ProjectError
            try:
                self.executor.tasks.service.claim_delegation(str(arguments.task_id), str(child_id), team_claim=team_assignment)
                if team_assignment is not None:
                    stage = self.executor.project_manager.verify_workspace(UUID(team_assignment["stage_run_id"]))
                    workspace = Path(stage.worktree)
                review = await _file_operation(prepare_workspace, self.executor.project_manager, workspace, child_id, parent.manifest.run_id, scope)
                if team_assignment is not None:
                    from agent_harness.projects.integration_files import identity
                    projects = self.executor.project_manager
                    owned = projects._ownership(child_id)
                    owned.update(team_id=team_assignment["team_id"], team_task_id=team_assignment["task_id"],
                                 delegation_parent_path=str(parent.manifest.task.workspace), delegation_parent_identity=identity(parent.manifest.task.workspace))
                    projects._write_json(projects._metadata_dir(child_id) / "ownership.json", owned)
                workspace = Path(review.worktree)
                python_paths = [str(path.relative_to(parent.manifest.task.workspace)) for path in execution.python_paths] if execution else None
                execution = prepare_python_execution(workspace, python_executable=str(execution.executable) if execution and execution.executable else None, python_paths=python_paths)
                config.permission_policy.write_roots = scope
            except asyncio.CancelledError:
                await self._abandon_workspace(child_id)
                self.executor.tasks.service.release_delegation(str(child_id), "子工作区准备已取消，未启动模型。")
                raise
            except (TaskError, ProjectError, ValueError, OSError) as error:
                await self._abandon_workspace(child_id)
                self.executor.tasks.service.release_delegation(str(child_id), "子工作区准备失败，未启动模型。")
                raise SubagentError(getattr(error, "code", "subagent_workspace_failed"), getattr(error, "message", "无法准备隔离开发环境。")) from None
        manifest = RunManifest(run_id=child_id, session_id=parent.manifest.session_id or parent.manifest.run_id,
                               task=parent.manifest.task.model_copy(update={"prompt": prompt, "verifier": None, "workspace": workspace, "python_execution": execution}),
                               config=config, origin=parent.manifest.origin, automation=parent.manifest.automation, planning=planning.model_copy(deep=True),
                               delegation=DelegationOrigin(parent_run_id=parent.manifest.run_id, role=arguments.role, task_id=arguments.task_id, write_scope=scope,
                                   team_id=team_assignment["team_id"] if team_assignment else None, team_member_id=team_assignment["member_id"] if team_assignment else None))
        record = SubagentRecord(run_id=manifest.run_id, title=arguments.title.strip(), objective=arguments.objective,
                                role=arguments.role, task_id=arguments.task_id, skill_names=config.skills, parent_reserve=reserve,
                                log_parent_id=parent.manifest.run_id, workspace=str(workspace) if developing else None, write_scope=scope,
                                max_model_turns=turns, max_tool_calls=calls, timeout_seconds=seconds)
        if team_assignment is not None:
            record.team_id, record.team_member_id = team_assignment["team_id"], team_assignment["member_id"]
            self.team.started(team_assignment, record)
        await self._launch(manifest, record, allowed, self.inputs, background=arguments.background)
        return {"subagent": record.model_dump(mode="json"), "accepted": arguments.background,
                "notice": "子用量已计入父总数；读取报告和实际代码成果后再决定后续操作，开发成果须经人工采纳。"}

    async def _abandon_workspace(self, identifier: UUID) -> None:
        """回收identifier尚未启动模型的工作树；只操作已核验宿主归属目录，失败保留路径供核对。"""
        from agent_harness.runtime.runner import _file_operation
        projects = self.executor.project_manager
        if projects.get(identifier) is not None:
            try:
                await _file_operation(projects.capture, identifier, finalizing=True)
                await _file_operation(projects.decide, identifier, "discard", finalizing=True)
            except Exception:
                self.parent.emit("subagent_prepare_cleanup_failed", {"run_id": str(identifier), "workspace": str(projects.worktrees_root / str(identifier))})

    async def _launch(self, manifest, record, allowed, inputs, *, background: bool = True, spent=None) -> None:
        """按固定manifest/record创建子日志并启动；allowed限制工具，inputs继承冻结输入，返回None。

        background=False等待本子项，True由父后续查询；spent=None表示首次执行，否则继承安全暂停前的累计权限预算。
        预算预留在首次await前完成，避免重复分配。
        """
        from agent_harness.runtime.runner import RunContext
        parent = self.parent
        child_root = parent.store.run_dir / "subagents"
        if child_root.resolve() != child_root:
            raise StorageError("子执行记录目录身份发生变化。")
        store = RunStore.create(child_root, manifest)
        result = RunResult(run_id=manifest.run_id, execution_status="running", usage_complete=True, artifact_status="complete", planning=manifest.planning)
        if spent is not None:
            result.permissions = spent.model_copy(deep=True)
        now = asyncio.get_running_loop().time()
        child = RunContext(manifest, store, result, min(now + record.timeout_seconds, parent.deadline), started_at=now, record_root=parent.runs_root, budget_parent=parent)
        self._publish(record, child)
        parent.result.subagents.append(record)

        def progress(kind: str, payload: dict) -> None:
            """在子事件kind/payload已落盘后发布计数，返回None，不复制完整子消息。"""
            self._publish(record, child)

        child.on_event = progress
        for field, value in (("model_turns", record.max_model_turns), ("tool_calls", record.max_tool_calls)):
            child.reserved[field] = value
            parent.reserved[field] = parent.reserved.get(field, 0) + value
        inherited = copy(inputs)
        inherited.memory = inputs.memory.fork(manifest, child.emit)
        task = asyncio.create_task(self._run_child(child, record, allowed, inherited))
        self.jobs[manifest.run_id] = task
        if background:
            self.pending_reports.add(manifest.run_id)
        await asyncio.sleep(0)  # 先让子协程进入try/finally，立即取消也能封存日志。
        if not background:
            await asyncio.shield(task)
            self.check_errors()

    async def resume_children(self) -> list[dict]:
        """从父清单绑定的具体审批或安全暂停恢复子上下文并返回报告；拒绝时零模型、零工具。

        保存兄弟终态与待审批动作，只接续已批准子项及安全暂停子项；旧工具回执只读继承。
        父与子均扣除各自累计预算，旧工作树复用，结果未知或环境变化不自动重放。
        """
        from agent_harness.permissions.models import PermissionResume
        from agent_harness.runtime.runner import AgentStopped, _file_operation
        from agent_harness.projects.models import ProjectPreset
        from agent_harness.work.tasks import TaskError
        parent, resume = self.parent, self.parent.manifest.permission_resume
        previous = RunStore.read(parent.runs_root, str(parent.manifest.parent_run_id))
        if previous.result is None:
            raise AgentStopped("failed", "subagent_resume_unverified")
        records = [item.model_copy(deep=True) for item in previous.result.subagents]
        for item in records:
            item.log_parent_id = item.log_parent_id or previous.manifest.run_id
        target = None
        if resume is not None:
            target = next((item for item in records if str(item.run_id) == resume.child_run_id
                           and str(item.log_parent_id) == resume.child_parent_id), None)
            if target is None or target.pending_action is None or target.pending_action["id"] != resume.action_id:
                raise AgentStopped("failed", "subagent_resume_unverified")
        elif parent.manifest.execution_resume is None or previous.result.stop_reason != "user_paused" or any(item.pending_action is not None for item in records):
            raise AgentStopped("failed", "subagent_resume_unverified")
        if resume is not None and resume.decision != "approve":
            target.pending_action, target.status, target.stop_reason = None, "cancelled", "permission_" + resume.decision
            parent.result.subagents = records
            if target.team_id is not None:
                self.team.cancelled(target)
            for item in records:
                parent.emit("subagent_snapshot", {"subagent": item.model_dump(mode="json")})
            self.check_errors()
            parent.result.final_answer = "已拒绝或取消子任务动作，未调用模型或执行工具。"
            raise AgentStopped("cancelled", "permission_" + resume.decision)
        pending = []
        for record in records:
            if record is target or (record.status == "waiting_input" and record.pending_action is None):
                pending.append(record)
            else:
                parent.result.subagents.append(record)
                parent.emit("subagent_snapshot", {"subagent": record.model_dump(mode="json")})
        for record in pending:
            history = read_child(parent.runs_root, record.log_parent_id, record.run_id)
            if history.result is None or history.result.cleanup_status not in {"not_needed", "complete"}:
                raise AgentStopped("failed", "subagent_resume_unverified")
            old = history.manifest
            config = old.config.model_copy(deep=True)
            config.permission_policy = parent.manifest.config.permission_policy.model_copy(deep=True)
            config.permission_mode = parent.manifest.config.permission_mode
            from agent_harness.subagents.workspaces import intersect_scope
            developing = record.role == "develop"
            scope = intersect_scope(record.write_scope, config.permission_policy.write_roots) if developing else []
            if developing:
                config.permission_policy.write_roots = scope
            allowed = PROFILES[record.role].tools.intersection(self.executor._enabled_names())
            allowed = allowed.union(name for name in self.executor._enabled_names() if name.startswith("mcp_") and self.executor.mcp is not None and self.executor.mcp.research(name))  # 接续也重新取父级研究能力交集。
            if record.team_id is not None:
                from agent_harness.teams.models import MEMBER_TOOLS
                allowed = allowed.union(MEMBER_TOOLS)
            config.tools = [name for name in config.tools if name in allowed]
            config.mcp_tools = [item for item in config.mcp_tools if item.name in allowed]
            state = history.result.permissions
            spent_turns = state.spent_model_turns + history.result.model_turns
            spent_calls = state.spent_tool_calls + history.result.tool_calls
            spent_seconds = state.spent_seconds + history.result.active_duration_seconds
            turns = min(config.limits.max_model_turns - spent_turns, parent.available("model_turns") - 2)
            calls = min(config.limits.max_tool_calls - spent_calls, parent.available("tool_calls") - 1)
            seconds = min(config.limits.run_timeout_seconds - spent_seconds, parent.deadline - asyncio.get_running_loop().time())
            if turns < 1 or calls < 1 or seconds <= 0:
                raise AgentStopped("limit_exceeded", "subagent_budget_exhausted")
            config.limits = config.limits.model_copy(update={"max_model_turns": spent_turns + turns, "max_tool_calls": spent_calls + calls})
            child_resume = None
            if resume is not None and record is target:
                child_resume = PermissionResume(action_id=resume.action_id, decision="approve",
                    spent_model_turns=spent_turns, spent_tool_calls=spent_calls, spent_seconds=spent_seconds,
                    spent_usage={key: state.spent_usage.get(key, 0) + getattr(history.result.usage, key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                    spent_usage_complete=state.spent_usage_complete and history.result.usage_complete)
            identifier = uuid4()
            if developing:
                preset = ProjectPreset(id="delegation", label="开发子任务", path=Path(self.executor.project_manager.get(record.run_id).repository))
                await _file_operation(self.executor.project_manager.prepare_turn, preset, record.run_id, identifier)
                projects = self.executor.project_manager
                owned = projects._ownership(identifier)
                owned["delegation_parent_id"] = str(parent.manifest.run_id)
                owned["delegation_scope"] = scope
                projects._write_json(projects._metadata_dir(identifier) / "ownership.json", owned)
            manifest = RunManifest(run_id=identifier, session_id=parent.manifest.session_id or parent.manifest.run_id,
                task=old.task, config=config, origin=old.origin, automation=parent.manifest.automation, planning=parent.manifest.planning.model_copy(deep=True),
                permission_resume=child_resume,
                delegation=DelegationOrigin(parent_run_id=parent.manifest.run_id, role=record.role, task_id=record.task_id,
                    write_scope=scope, previous_parent_id=record.log_parent_id, previous_run_id=record.run_id,
                    team_id=record.team_id, team_member_id=record.team_member_id))
            # 安全暂停没有待执行动作，但同样要继承此前预算；单独由启动器初始化，不能伪造审批。
            new_record = record.model_copy(update={"run_id": identifier, "log_parent_id": parent.manifest.run_id,
                "status": "running", "pending_action": None, "report": None, "snapshot_status": "pending", "write_scope": scope,
                "max_model_turns": turns, "max_tool_calls": max(1, calls), "timeout_seconds": seconds})
            assignment = None
            if record.team_id is not None:
                team_state = self.team.store.require(self.team.snapshot())
                item = next(t for t in team_state["tasks"] if t["task_id"] == str(record.task_id))
                assignment = {"team_id": record.team_id, "member_id": record.team_member_id, "task_id": str(record.task_id),
                              "dependency_versions": item["dependency_versions"], "stage_run_id": team_state["stage_run_id"]}
                self.team.started(assignment, new_record)
            if developing:
                self.executor.tasks.service.claim_delegation(str(record.task_id), str(identifier), team_claim=assignment)
            await self._launch(manifest, new_record, allowed, self.inputs, spent=history.result.permissions.model_copy(update={
                "spent_model_turns": spent_turns, "spent_tool_calls": spent_calls, "spent_seconds": spent_seconds,
                "spent_usage": {key: state.spent_usage.get(key, 0) + getattr(history.result.usage, key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                "spent_usage_complete": state.spent_usage_complete and history.result.usage_complete}))
        return await self.wait_all()

    def report_events(self, child) -> list:
        """返回child当前段及可信审批接续链的原始事件；跨成员、跨任务、目录变化或未知结束均拒绝。

        最多十六段且不允许循环。只核验已执行事实，不重放旧工具，也不扩大本段权限或预算。
        """
        manifest = child.manifest
        current = read_child(self.parent.runs_root, manifest.delegation.parent_run_id, manifest.run_id)
        events, seen = list(current.events), {manifest.run_id}
        origin = manifest.delegation
        while origin.previous_run_id is not None:
            if len(seen) >= 16 or origin.previous_run_id in seen:
                raise StorageError("子执行接续来源循环或超过可核验上限。")
            seen.add(origin.previous_run_id)
            history = read_child(self.parent.runs_root, origin.previous_parent_id, origin.previous_run_id)
            previous = history.manifest.delegation
            if (history.result is None or history.result.cleanup_status not in {"not_needed", "complete"} or previous is None
                    or history.manifest.task.workspace != manifest.task.workspace or previous.role != manifest.delegation.role
                    or previous.task_id != manifest.delegation.task_id or previous.team_id != manifest.delegation.team_id
                    or previous.team_member_id != manifest.delegation.team_member_id):
                raise StorageError("子报告证据接续来源无法核验。")
            events = [*history.events, *events]
            origin = previous
        return events

    async def _run_child(self, child, record, allowed, inputs) -> None:
        """用child独立上下文执行record，只授予allowed工具；inputs已固定，返回None并释放未用额度。

        受控失败写终态和部分证据，存储错误上抛，由父生命周期收拢；不进入顶层队列。
        """
        from agent_harness.runtime.agent import run_agent
        from agent_harness.runtime.runner import AgentStopped
        from agent_harness.tools.registry import ToolExecutor
        parent, manifest, result, store = self.parent, child.manifest, child.result, child.store
        config = manifest.config
        try:
            executor = ToolExecutor(manifest.task.workspace, parent.runs_root, config, python_execution=manifest.task.python_execution,
                                    readonly=allowed if record.role != "develop" else None)
            executor.bind_run(store.run_dir)
            executor.delegation_tools = allowed if record.role == "develop" else None
            if record.team_id is not None:
                from agent_harness.teams.controller import TeamController
                executor.team = TeamController(child, self, record.team_member_id, executor=executor)
            async with asyncio.timeout_at(child.deadline):
                await run_agent(child, self.client, executor, inherited_context=inputs)
            result.execution_status = "completed"
        except AgentStopped as error:
            result.execution_status, result.stop_reason = error.status, error.reason
            if error.status == "waiting_input":
                record.pending_action = result.permissions.pending
                self.pause_requested = True
                if record.pending_action is not None:
                    self.attention.set()
        except HookPending:
            result.execution_status, result.stop_reason = "waiting_input", "action_approval"
            record.pending_action = executor.permissions.snapshot().pending
            self.pause_requested = True
            self.attention.set()
        except TaskError as error:
            result.execution_status, result.stop_reason = "failed", error.code
        except TimeoutError:
            result.execution_status, result.stop_reason, result.usage_complete = "timed_out", "subagent_timeout", False
        except asyncio.CancelledError:
            result.execution_status = "timed_out" if parent.timeout_scope is not None and parent.timeout_scope.expired() else "cancelled"
            result.stop_reason, result.usage_complete = self.cancel_reasons.get(manifest.run_id, "parent_stopped"), False
        except ModelError as error:
            result.execution_status, result.stop_reason, result.usage_complete = "failed", error.code, False
            child.emit("model_request_failed", {"code": error.code})
        except StorageError:
            raise
        except Exception:
            result.execution_status, result.stop_reason, result.usage_complete = "failed", "subagent_internal_error", False
        finally:
            for field, value in child.reserved.items():
                parent.reserved[field] -= value
            child.reserved.clear()
            if not result.usage_complete:
                parent.result.usage_complete = False
            if record.role == "develop":
                from agent_harness.runtime.runner import _collect_tool_state, _file_operation
                from agent_harness.subagents.workspaces import in_scope, changed_paths
                if "executor" in locals():
                    _collect_tool_state(result, executor)
                try:
                    review = await _file_operation(self.executor.project_manager.capture, manifest.run_id, finalizing=True)
                    record.snapshot_status = review.snapshot_status
                    record.changed_paths = changed_paths(self.executor.project_manager, review)
                    record.scope_violations = [name for name in record.changed_paths if not in_scope(name, record.write_scope)]
                    from agent_harness.schemas import ArtifactRef
                    from agent_harness.storage.snapshots import SnapshotError
                    for item in review.files:
                        if item.status != "deleted":
                            try:
                                self.executor.project_manager.files.saved_file(manifest.run_id, item.path)
                                reference = ArtifactRef(location="workspace", path=item.path)
                                if reference not in result.artifacts:
                                    result.artifacts.append(reference)
                            except SnapshotError:
                                result.artifact_status = "incomplete"
                                result.artifact_warnings = ["部分子成果超过文件快照限制，固定补丁仍可审阅。"]
                    if record.scope_violations and result.execution_status == "completed":
                        result.execution_status, result.stop_reason = "failed", "subagent_scope_violation"
                except Exception:
                    record.snapshot_status = "error"
                    result.artifact_status, result.artifact_warnings = "incomplete", ["子成果未完整固定，不能以当前工作树冒充历史产物。"]
                    if result.execution_status == "completed":
                        result.execution_status, result.stop_reason = "failed", "subagent_snapshot_failed"
                self.executor.tasks.service.release_delegation(str(manifest.run_id), "子成果待审阅采纳。" if result.execution_status == "completed" else "子执行已停止，请核对证据后接续。")
                rank = {"not_needed": 0, "complete": 1, "unknown": 2, "incomplete": 3}
                if rank[result.cleanup_status] > rank[parent.result.cleanup_status]:
                    parent.result.cleanup_status = result.cleanup_status
            if result.execution_status != "running":
                events = self.report_events(child)
                result.subagent_report = finalize_report(result, events, role=record.role)
                result.active_duration_seconds = child.active_duration()
                store.finalize(result)
                child.event_count += 1
                record.status, record.stop_reason = result.execution_status, result.stop_reason
                record.report = result.subagent_report
                self._publish(record, child)
                if record.team_id is not None:
                    await self.team.finished(record)
