"""单次运行的生命周期与计数；超时和取消统一在这里保存终态。"""

import asyncio
from collections.abc import Callable
import os
import traceback
import time
from dataclasses import dataclass, field
from contextvars import ContextVar
from pathlib import Path

import httpx

from agent_harness.execution.python import PythonExecutionError, prepare_python_execution, validate_python_execution
from agent_harness.config import ConfigurationError
from agent_harness.model_selection import valid_model_key
from agent_harness.schemas import ArtifactRef, RunConfig, RunManifest, RunResult, VerificationResult
from agent_harness.providers.base import ModelClient, ModelDelta, ModelError
from agent_harness.providers.chat import ChatCompletionsClient
from agent_harness.providers.factory import create_model_client
from agent_harness.storage.runs import RunStore, StorageError
from agent_harness.tools.registry import ToolExecutor
from agent_harness.execution.local import LocalChangeStore
from agent_harness.projects import ProjectError, ProjectManager
from agent_harness.storage.snapshots import SnapshotError
from agent_harness.work.planning import PlanningController
from agent_harness.work.tasks import WorkStore, TaskController, ExecutionLease, TaskError
from agent_harness.hooks.controller import HookPending
from agent_harness.runtime.verifier import Verifier


# 请求归属随asyncio任务隔离；共享HTTP客户端的回调不能依赖全局活动子项。
REQUEST_CONTEXT = ContextVar("request_context", default=None)


@dataclass
class CancellationState:
    """记录宿主确认的取消来源，区分CLI信号、网页操作和服务关闭。"""

    user_requested: bool = False  # CLI实际收到SIGINT；初始False，不由异常类型推测。
    api_requested: bool = False  # 网页取消接口已明确请求；与CLI信号来源分别记录。
    shutdown_requested: bool = False  # 服务关闭要求收尾；初始False，不冒充用户操作。


@dataclass
class RunOutcome:
    """交回 CLI 的运行结果，包含状态、记录目录和已提交事件数量。"""

    result: RunResult
    run_dir: Path
    event_count: int


@dataclass
class RunContext:
    """供 Agent 循环共享的运行上下文，汇集清单、日志、可变结果和总截止时间。"""

    manifest: RunManifest
    store: RunStore
    result: RunResult
    deadline: float  # 事件循环单调时钟上的绝对时间，不是 UTC 时间戳。
    event_count: int = 1
    phase: str = "starting"
    request_purpose: str = "execution"  # 当前请求用途；内部摘要不进入用户对话流。
    started_at: float = 0.0  # 本次运行的单调起点，用于计量实际执行时长。
    record_root: Path | None = None  # 子日志嵌套保存时仍使用真实运行根保护文件和记忆边界；None取日志父目录。
    budget_parent: "RunContext | None" = None  # 子执行的共享预算归属，根运行为None。
    reserved: dict[str, int] = field(default_factory=dict)  # 根记录未消费的子预算；子记录自己的未消费分配。
    subagent_controller: object | None = None  # 根运行的生命周期控制器，未绑定时为空。
    on_event: Callable[[str, dict], None] | None = None  # 子事件落盘后通知父级进度，None不发布额外快照。
    timeout_scope: asyncio.Timeout | None = None  # 根运行实际超时管理器；None为尚未进入，避免Windows时钟粒度误判取消来源。
    delegation_used: bool = False  # 曾尝试委派后启用父级末轮交接；失败接纳也须为父保留总结机会。
    request_turn: int = 0  # 当前自身请求启动时的轮号；父总计数可能被并行子项推进，不能拿响应时总数配对。
    runtime_control: object | None = None  # 当前执行段安全边界及用户输入；子段读取根收件箱。
    command_controller: object | None = None  # 当前段命令句柄，未绑定工具执行器时为 None。
    command_workspaces: dict[str, str] = field(default_factory=dict)  # 根段共享的实际目录占用，活动命令期间禁止其他写入。
    command_controllers: dict[str, object] = field(default_factory=dict)  # 根段聚合父子命令控制器，用于观察及统一停止。
    hooks: object | None = None  # 当前 Agent 的生命周期扩展调度器；无工具执行器时为空。
    mcp_credential_provider: object | None = None  # 异步桌面秘密读取器；仅内存使用，不写入清单或事件。
    role_client: Callable[[RunConfig], ChatCompletionsClient] | None = None  # 独立角色客户端入口，仅根运行持有。

    @property
    def runs_root(self) -> Path:
        """返回受保护的实际运行根目录；不把嵌套子日志目录误作整个数据边界。"""
        return self.record_root or self.store.run_dir.parent

    def consume(self, kind: str) -> None:
        """接纳一次model或tool调用并累计父子计数；超出任一级限额时先抛AgentStopped。

        kind仅由执行核心传入model/tool；返回None，不发送请求或执行工具。
        子预算是父预算的子集，检查、消费预留和累计无await，同一事件循环不会在其间穿插其他调用。
        """
        field, limit = ("model_turns", "max_model_turns") if kind == "model" else ("tool_calls", "max_tool_calls")
        if self.budget_parent is None and self.available(field) <= 0:
            raise AgentStopped("limit_exceeded", limit)
        contexts = [self] if self.budget_parent is None else [self, self.budget_parent]
        for item in contexts:
            spent = getattr(item.result.permissions, "spent_" + field)
            if getattr(item.result, field) + spent >= getattr(item.manifest.config.limits, limit):
                raise AgentStopped("limit_exceeded", limit)
        for item in contexts:
            setattr(item.result, field, getattr(item.result, field) + 1)
        if self.budget_parent is not None and self.reserved.get(field, 0):
            self.reserved[field] -= 1
            self.budget_parent.reserved[field] -= 1

    def available(self, field: str) -> int:
        """返回field模型/工具未使用且未预留次数；不改变预算，子运行以自身上限计算。"""
        limit = "max_model_turns" if field == "model_turns" else "max_tool_calls"
        held = self.reserved.get(field, 0) if self.budget_parent is None else 0
        spent = getattr(self.result.permissions, "spent_" + field)
        return max(0, getattr(self.manifest.config.limits, limit) - getattr(self.result, field) - held - spent)

    def active_duration(self) -> float:
        """返回本次运行耗时秒数；使用单调时间，不受系统时间校准影响。"""
        return max(0.0, time.monotonic() - self.started_at)


    def emit(self, kind: str, payload: dict | None = None) -> None:
        """提交 kind 事件，成功后更新计数与阶段，返回 None。

        payload 默认为 None，表示无额外载荷；事件写入失败抛 StorageError，
        保留原阶段与计数，禁止后续按“已开始”调度副作用。
        """
        self.event_count = self.store.append(kind, payload).seq
        if kind == "tasks_snapshot" and payload is not None:
            self.result.tasks = payload["state"]  # 已提交快照供实时视图与中断历史共同读取。
        if kind == "team_snapshot" and payload is not None:
            self.result.team = payload["team"]  # 已落盘的团队代次供实时显示与中断历史。
        phases = {
            "model_request_started": "model_request",
            "context_compaction_started": "context_compaction",
            "model_response": "model_response",
            "tool_started": "tool_execution",
            "tool_result": "tool_result",
            "execution_completed": "execution_completed",
            "verification_started": "verification",
            "verification_result": "verification_result",
            "repair_started": "repair",
            "artifact_collection_started": "artifact_collection",
            "budget_handoff_started": "budget_handoff",
        }
        self.phase = "context_compaction" if kind == "model_request_started" and (payload or {}).get("purpose") == "summary" else phases.get(kind, self.phase)
        if kind == "hook_record" and payload is not None:
            status = payload["hook"]["status"]
            if status in {"running", "waiting"}:
                self.phase = "hook_execution" if status == "running" else "action_approval"
        if kind == "subagent_snapshot" and (payload or {}).get("subagent", {}).get("status") == "running":
            self.phase = "subagent_execution"
        if self.on_event is not None:
            self.on_event(kind, payload or {})

    def model_delta(self, delta: ModelDelta) -> None:
        """记录 delta 的展示事件；用请求上下文隔离父子并发，内部摘要不公开，返回 None。"""
        owner = REQUEST_CONTEXT.get() or self
        if owner.request_purpose == "summary":
            return
        owner.emit("model_delta", {"model_turn": owner.request_turn, "channel": delta.channel,
                                   "delta": delta.text, "tool_index": delta.tool_index,
                                   "call_id": delta.tool_id, "name": delta.tool_name})

    def http_attempt(self, attempt: int) -> None:
        """登记单轮 HTTP 尝试；出现重试时，用量可能含未知部分，因此取消完整标记。"""
        owner = REQUEST_CONTEXT.get()
        if owner is not None and owner is not self:
            owner.http_attempt(attempt)
            return
        self.result.http_attempts += 1
        if self.budget_parent is not None:
            self.budget_parent.result.http_attempts += 1
            if attempt > 1:
                self.budget_parent.result.usage_complete = False
        if attempt > 1:
            self.result.usage_complete = False
        self.emit("model_http_attempt", {"model_turn": self.request_turn or self.result.model_turns, "attempt": attempt})


class AgentStopped(Exception):
    """携带额度耗尽等可预期停止状态，把 Agent 的停止原因交回 Runner。"""

    def __init__(self, status: str, reason: str) -> None:
        """保存待写入 RunResult 的执行状态与稳定原因码。"""
        self.status = status
        self.reason = reason
        super().__init__(reason)


def validate_execution(manifest: RunManifest, api_key: str) -> None:
    """检查 manifest 的执行所需凭据格式；返回 None，不启动模型或验收。

    api_key 必须是非空可打印 ASCII 且无空白，错误抛 ConfigurationError。
    验收入口由配置模块启动预检并固定，密钥不写入 manifest 或日志。
    """
    config = manifest.config
    if any(not config.custom_providers[provider].enabled for provider in config.selected_providers() & config.custom_providers.keys()):
        raise ConfigurationError("所选自定义服务商已停用，请重新选择。")
    if config.requires_model_key and not valid_model_key(api_key):
        hint = f"（Web/CLI 使用 {config.api_key_env}）" if config.api_key_env else ""
        raise ConfigurationError(f"请为当前供应商配置有效 API 密钥{hint}。")


def validate_auto_repair(manifest: RunManifest) -> None:
    """核对manifest的修复开关与公开测试来源；不执行操作，无公开项目测试时抛配置错误。"""
    if manifest.config.auto_repair and (manifest.task.verifier is None or manifest.task.verifier.kind != "project_tests"):
        raise ConfigurationError("自动修复需要已配置的公开项目测试；独立验收及隐藏检查不能反馈给模型。")


def _collect_tool_state(result: RunResult, executor: ToolExecutor) -> None:
    """汇总执行器已登记的产物和最坏清理状态，取消路径也保留真实信息。"""
    rank = {"not_needed": 0, "complete": 1, "unknown": 2, "incomplete": 3}
    status = getattr(executor, "cleanup_status", "not_needed")
    if status in rank and rank[status] > rank[result.cleanup_status]:
        result.cleanup_status = status
    for raw in getattr(executor, "artifact_refs", []):
        reference = ArtifactRef.model_validate(raw)
        if reference.location == "run" and reference not in result.artifacts:
            result.artifacts.append(reference)


async def _file_operation(function: Callable, *arguments, finalizing: bool = False):
    """在线程完成function(*arguments)并返回结果；捕获操作没有Runner事件写入权。

    finalizing=False时取消会等待线程结束后继续传播，避免与下一阶段并发写证据；
    True表示执行已结束，等待证据收尾后交回真实结果，不中断固定历史文件的提交。
    """
    task = asyncio.create_task(asyncio.to_thread(function, *arguments))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        result = await asyncio.shield(task)
        if not finalizing:
            raise
        return result


def _capture_artifacts(manifest: RunManifest, changes: LocalChangeStore, projects: ProjectManager | None) -> tuple[list[ArtifactRef], list[str]]:
    """固定manifest的本轮结束证据，返回新增/修改文件引用及采集警告；不写Runner事件。

    changes提供本轮开始基线；projects非空表示显式Git隔离，复用其结束文件快照，
    不以累计Git补丁冒充本轮修改。采集失败保留可核对的状态，不改执行/验收结论。
    """
    warnings = []  # 只保存受控说明，文件变化不等于内容验收通过。
    if projects is not None:
        try:
            projects.capture(manifest.run_id)
        except (ProjectError, OSError, ValueError):
            warnings.append("本轮Git证据未完整保存，请检查代码审阅状态。")
    try:
        view = changes.finish(manifest.run_id, manifest.task.workspace, projects.files if projects is not None else None)
        if view.status != "ready":
            return [], [*warnings, *view.warnings]
        references = [ArtifactRef(location="workspace", path=item.path)
                      for item in view.files if item.status != "deleted"]
        return references, warnings
    except Exception:
        return [], [*warnings, "本轮文件未完整收集，请检查采集范围、目录身份与磁盘权限；实际修改仍保留。"]


def _interrupted_verification(verifier: Verifier, reason: str, *, timed_out: bool = False, cancelled: bool = False) -> VerificationResult:
    """保留被中断验收的输出和清理事实，返回 error 详情而非改写任务执行状态。

    verifier.last_result=None 表示尚无子进程结果；reason 是稳定中断原因。
    timed_out 与 cancelled 分别标记预算耗尽和外部取消，二者由调用方明确区分。
    """
    previous = verifier.last_result or VerificationResult(status="error", reason=reason)
    return previous.model_copy(update={"status": "error", "reason": reason, "timed_out": timed_out, "cancelled": cancelled})


async def _run_task(
    manifest: RunManifest,
    runs_root: Path,
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    model_client: ModelClient | None = None,
    executor: ToolExecutor | None = None,
    cancellation_state: CancellationState | None = None,
    on_context: Callable[[RunContext], None] | None = None,
    file_changes: LocalChangeStore | None = None,
    project_manager: ProjectManager | None = None,
    mcp_credential_provider=None,
    model_credential_provider: Callable[[RunConfig], str] | None = None,
) -> RunOutcome:
    """执行模型任务及可选独立验收，返回已保存的结果、目录和事件数。

    manifest 提供任务、预算和宿主验收入口；runs_root 是记录根目录；api_key 只交给
    模型适配器。三个可选组件参数默认 None，表示使用真实实现，可注入离线测试组件。
    cancellation_state 默认 None；只按宿主明确标记区分SIGINT、网页取消与服务关闭。
    on_context 默认None；非空时在记录创建后、首次模型调用前同步通知一次，返回值忽略。
    接收方只能读取该可变上下文供进度查询，不能修改状态或另行写入运行事件。
    file_changes=None时使用默认有界快照；宿主可传入带保护目录的原目录证据存储。
    project_manager仅供Git隔离运行，非隔离时不使用；结束快照与项目审阅复用。
    mcp_credential_provider=None 时外部认证仅用环境引用；非空为异步桌面私有读取器，不进入快照。
    model_credential_provider 为独立角色的宿主密钥解析器；None 时读取供应商对应环境变量。
    总秒级预算覆盖模型、工具与验收；之后固定交付证据再保存唯一终态，采集异常独立提示。
    函数会调用外部服务、执行工具并写文件；StorageError 立即传播，不伪造落盘成功。
    """
    validate_execution(manifest, api_key)
    validate_auto_repair(manifest)
    try:
        execution = manifest.task.python_execution or prepare_python_execution(manifest.task.workspace)
        if execution.workspace != manifest.task.workspace:
            raise PythonExecutionError("Python 执行快照与任务实际工作区不一致。")
        validate_python_execution(execution)
    except PythonExecutionError as error:
        raise ConfigurationError(str(error)) from None
    # 程序入口也在落盘前固定快照；读取历史不执行此准备流程。
    task = manifest.task.model_copy(update={"python_execution": execution})
    manifest = manifest.model_copy(update={"task": task})
    if manifest.config.planning_policy == "required" and not (manifest.planning.plans and manifest.planning.plans[-1].status == "approved"):
        # CLI和Web统一执行宿主强制策略；自动入口也不能先执行命令再补规划。
        state = manifest.planning.model_copy(deep=True)
        state.phase, state.reason = "planning", "宿主要求先批准计划"
        manifest = manifest.model_copy(update={"planning": state})
    try:
        tool_executor = executor or ToolExecutor(manifest.task.workspace, runs_root, manifest.config, python_execution=execution)
    except (OSError, RuntimeError, ValueError):
        raise StorageError("运行记录目录无法解析，请检查目录和符号链接。") from None
    if hasattr(tool_executor, "bind_python_execution"):
        try:
            tool_executor.bind_python_execution(execution)
        except PythonExecutionError as error:
            raise ConfigurationError(str(error)) from None
    # 所有入口在模型动作前固定本轮基线，原目录的既有未提交改动不会变成新产物。
    changes = file_changes or LocalChangeStore(runs_root)
    isolated = manifest.origin is not None and manifest.origin.execution_mode == "isolated"
    if isolated and project_manager is None:
        raise ConfigurationError("Git隔离运行需要宿主提供项目证据管理器。")
    projects = project_manager if isolated else None
    root = manifest.task.workspace
    if manifest.origin is not None and not isolated:
        changes.verify(manifest.origin, root)
    identity = changes.identity(root)
    try:
        await _file_operation(changes.begin, manifest.run_id, root)
    except (SnapshotError, OSError):
        pass  # 开始快照缺失会在统一收尾中明确标为收集不完整，仍允许当前目录执行。
    if changes.identity(root) != identity:
        raise ConfigurationError("建立文件基线时任务目录发生变化，请重新打开项目。")
    store = RunStore.create(runs_root, manifest)
    if hasattr(tool_executor, "bind_run"):
        tool_executor.bind_run(store.run_dir)
    result = RunResult(run_id=manifest.run_id, execution_status="running", usage_complete=True, planning=manifest.planning.model_copy(deep=True))
    started_at = asyncio.get_running_loop().time()
    resume = manifest.permission_resume
    budget = manifest.execution_resume or (resume if resume and resume.decision == "approve" else None)
    deadline = started_at + max(0, manifest.config.limits.run_timeout_seconds - (budget.spent_seconds if budget else 0))
    context = RunContext(manifest, store, result, deadline, started_at=started_at)
    context.mcp_credential_provider = mcp_credential_provider  # 只驻留内存；子执行通过根预算上下文读取。
    from agent_harness.runtime.control import RuntimeControl
    context.runtime_control = RuntimeControl(context)
    planning = PlanningController(manifest.planning, root, context.emit)
    from agent_harness.permissions.controller import PermissionController
    permissions = PermissionController(context, planning)
    result.permissions = permissions.state.model_copy(deep=True)
    context.emit("permissions_prepared", {"state": result.permissions.model_dump(mode="json")})
    result.repair_attempts = budget.repair_attempts if budget is not None else 0
    context.delegation_used = bool(budget and budget.delegation_used)
    tool_executor.permissions = permissions
    tool_executor.project_manager = project_manager or ProjectManager(runs_root, runs_root / ".worktrees")
    tasks = TaskController(WorkStore(runs_root), manifest, planning)
    result.tasks = tasks.snapshot()
    context.emit("tasks_snapshot", {"state": result.tasks})
    if hasattr(tool_executor, "bind_planning"):
        tool_executor.bind_planning(planning)
        tool_executor.tasks = tasks
    verifier = Verifier(manifest.config.limits, store.run_dir) if manifest.task.verifier is not None else None
    from agent_harness.runtime.agent import run_agent
    from agent_harness.runtime.completion import apply_verification

    def role_client(config: RunConfig) -> ChatCompletionsClient:
        """返回 config 对应的独立角色客户端，缺少有效密钥时抛可记录的模型错误。"""
        key = model_credential_provider(config) if model_credential_provider is not None else os.environ.get(config.api_key_env, "") if config.api_key_env else ""
        if config.requires_model_key and not valid_model_key(key):
            raise ModelError("model_credentials_invalid", "请为委派角色配置有效 API 密钥。")
        return create_model_client(config, key, on_attempt=context.http_attempt, on_delta=context.model_delta, transport=transport)

    context.role_client = role_client
    try:
        if on_context is not None:
            on_context(context)
        async with asyncio.timeout_at(deadline) as timeout_scope:
            context.timeout_scope = timeout_scope
            if model_client is not None:
                await run_agent(context, model_client, tool_executor, verifier=verifier)
            else:
                async with create_model_client(manifest.config, api_key, on_attempt=context.http_attempt, on_delta=context.model_delta, transport=transport) as client:
                    await run_agent(context, client, tool_executor, verifier=verifier)
    except TimeoutError:
        if result.execution_status == "completed" and verifier is not None:
            apply_verification(context, _interrupted_verification(verifier, "run_timeout", timed_out=True))
        else:
            result.execution_status = "timed_out"
            result.stop_reason = "run_timeout"
            result.usage_complete = False
    except asyncio.CancelledError as error:
        cancelled_phase = context.phase  # 接收取消时固定真实阶段，不能被后续检查结果/清理事件覆盖。
        task = asyncio.current_task()
        cancelling = task.cancelling() if task is not None else 0
        if cancellation_state is not None and cancellation_state.user_requested:
            source, reason, status = "user_sigint", "user_cancelled", "cancelled"
        elif cancellation_state is not None and cancellation_state.api_requested:
            source, reason, status = "api_user", "user_cancelled", "cancelled"
        elif cancellation_state is not None and cancellation_state.shutdown_requested:
            source, reason, status = "service_shutdown", "service_shutdown", "cancelled"
        elif cancelling:
            source, reason, status = "task_cancel", "task_cancelled", "cancelled"
        else:
            source, reason, status = "unexpected_exception", "unexpected_cancellation", "failed"
        if result.execution_status == "completed" and verifier is not None:
            apply_verification(context, _interrupted_verification(verifier, reason, cancelled=status == "cancelled"))
        else:
            result.execution_status, result.stop_reason = status, reason
            result.usage_complete = False
        # 不保存异常正文、Task repr、代码行内容或局部变量，防止输入与凭据泄漏。
        frames = traceback.extract_tb(error.__traceback__)[-16:]
        context.emit("cancellation_received", {
            "source": source, "task_cancelling": cancelling, "phase": cancelled_phase,
            "trace": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno} for frame in frames],
        })
    except AgentStopped as error:
        result.execution_status, result.stop_reason = error.status, error.reason
    except HookPending:
        result.execution_status, result.stop_reason = "waiting_input", "action_approval"
        result.final_answer = "Hook 等待本次命令批准，已完成的阶段会在接续时保留。"
    except TaskError as error:
        result.execution_status, result.stop_reason = "failed", error.code
        result.final_answer = error.message
    except ModelError as error:
        result.execution_status = "timed_out" if error.code == "model_timeout" else "failed"
        result.stop_reason, result.usage_complete = error.code, False
        context.emit("model_request_failed", {"model_turn": result.model_turns, "code": error.code})
    except StorageError:
        # 事件存储失败时仍尽力固定现场，但不能补造可信终态或追加其他写入者。
        await _file_operation(_capture_artifacts, manifest, changes, projects, finalizing=True)
        raise
    except Exception as error:
        if result.execution_status == "completed" and verifier is not None:
            apply_verification(context, VerificationResult(status="error", reason="verifier_runtime_error", cleanup_status="unknown"))
        else:
            result.execution_status, result.stop_reason, result.usage_complete = "failed", "internal_error", False
        context.emit("runtime_error", {"code": result.stop_reason, "error_type": type(error).__name__})

    _collect_tool_state(result, tool_executor)
    if resume is not None and resume.decision == "approve":
        # 若必需上下文或预算校验在认领前停止，旧许可也不能继续显示为等待执行。
        permissions.store.invalidate(resume.action_id, result.stop_reason or "resume_stopped")
    tasks.service.release("前轮已结束，请核对现场后接续。" if result.execution_status != "completed" else "前轮尚未完成此项，请接续。")
    result.tasks = tasks.snapshot()
    if getattr(tool_executor, "team", None) is not None:
        result.team = tool_executor.team.snapshot()
    result.planning = planning.state.model_copy(deep=True)
    result.permissions = permissions.snapshot()
    context.emit("artifact_collection_started")
    references, warnings = await _file_operation(_capture_artifacts, manifest, changes, projects, finalizing=True)
    result.artifact_status = "incomplete" if warnings else "complete"
    result.artifact_warnings = warnings
    for reference in references:
        result.artifacts.append(reference)
        context.emit("artifact_registered", {"artifact": reference.model_dump(mode="json")})
    context.emit("artifacts_collected", {"status": result.artifact_status, "warnings": warnings, "file_count": len(references)})
    if result.verification is None:
        reason = "not_configured" if verifier is None else "execution_not_completed"
        context.emit("verification_skipped", {"reason": reason})
    result.active_duration_seconds = context.active_duration()
    context.runtime_control.store.finish(context)  # 成果证据固定后才发布安全暂停，累计秒数与本段结果一致。
    store.finalize(result)
    return RunOutcome(result, store.run_dir, context.event_count + 1)


async def run_task(manifest: RunManifest, runs_root: Path, api_key: str, *, execution_lease: ExecutionLease | None = None, **options) -> RunOutcome:
    """持有数据目录执行锁运行 manifest，返回固定结果；所有退出路径释放进程锁。

    runs_root 为同宿主记录目录，api_key 仅传模型；options 接收 _run_task 的可选依赖、
    取消来源与回调，None 行为沿用该函数。锁冲突在文件快照前抛 TaskError，不执行项目动作。
    """
    validate_execution(manifest, api_key)
    validate_auto_repair(manifest)  # 无效配置在创建执行锁文件前拒绝。
    coordinator = None  # CLI 自行协调资源；Web/桌面已在项目准备前认领，直接复用宿主锁。
    owner = str(manifest.run_id)
    if execution_lease is None:
        from agent_harness.execution.scheduling import Coordinator, resources
        coordinator = Coordinator(WorkStore(runs_root), 1, 1, 1)
        paths = [manifest.task.workspace]
        if manifest.origin is not None:
            paths.append(manifest.origin.registered_path)
        coordinator.acquire(owner, resources(paths, {}), str(manifest.session_id or manifest.run_id), owner)
    elif execution_lease.root != runs_root.resolve() or execution_lease.handle is None:
        raise StorageError("宿主传入的执行锁不属于当前记录目录。")
    outcome = None  # 未取得可信终态时保留资源为待核对，不把异常当作无副作用。
    try:
        outcome = await _run_task(manifest, runs_root, api_key, **options)
        return outcome
    finally:
        if coordinator is not None:
            coordinator.release(owner, outcome is None or outcome.result.cleanup_status not in {"complete", "not_needed"})



def exit_code(result: RunResult) -> int:
    """把 result 映射为整数退出码；取消/总超时即使发生于验收也优先返回 130/124。

    执行或验收失败返回 1，完成但未验收返回 3；只有完成且验收通过、清理可确认才为 0。
    本函数无副作用，允许 CLI 与后续前端服务共用同一判断。
    """
    if result.execution_status == "cancelled" or result.stop_reason in {"user_cancelled", "task_cancelled", "service_shutdown"}:
        return 130
    if result.execution_status == "timed_out" or result.stop_reason == "run_timeout":
        return 124
    if result.execution_status == "waiting_input":
        return 4
    if result.execution_status != "completed" or result.cleanup_status in {"incomplete", "unknown"}:
        return 1
    if result.validation_status == "unverified":
        return 3
    return 0 if result.validation_status == "passed" else 1
