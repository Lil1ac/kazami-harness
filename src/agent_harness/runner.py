"""单次运行的生命周期与计数；超时和取消统一在这里保存终态。"""

import asyncio
from collections.abc import Callable
import traceback
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import ValidationError

from .config import ConfigurationError
from .schemas import ArtifactRef, RunManifest, RunResult, TaskSpec, VerificationResult
from .providers.base import ModelClient, ModelError
from .providers.deepseek import DeepSeekClient
from .store import RunStore, StorageError
from .tools.registry import ToolExecutor
from .workspace import Workspace, WorkspaceError
from .verifier import Verifier


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
    started_at: float = 0.0  # 本次运行的单调起点，用于计量实际执行时长。

    def active_duration(self) -> float:
        """返回本次运行耗时秒数；使用单调时间，不受系统时间校准影响。"""
        return max(0.0, time.monotonic() - self.started_at)


    def emit(self, kind: str, payload: dict | None = None) -> None:
        """提交 kind 事件，成功后更新计数与阶段，返回 None。

        payload 默认为 None，表示无额外载荷；事件写入失败抛 StorageError，
        保留原阶段与计数，禁止后续按“已开始”调度副作用。
        """
        self.event_count = self.store.append(kind, payload).seq
        phases = {
            "model_request_started": "model_request",
            "context_compaction_started": "context_compaction",
            "model_response": "model_response",
            "tool_started": "tool_execution",
            "tool_result": "tool_result",
            "execution_completed": "execution_completed",
            "verification_started": "verification",
        }
        self.phase = "context_compaction" if kind == "model_request_started" and (payload or {}).get("purpose") == "summary" else phases.get(kind, self.phase)

    def http_attempt(self, attempt: int) -> None:
        """登记单轮 HTTP 尝试；出现重试时，用量可能含未知部分，因此取消完整标记。"""
        self.result.http_attempts += 1
        if attempt > 1:
            self.result.usage_complete = False
        self.emit("model_http_attempt", {"model_turn": self.result.model_turns, "attempt": attempt})


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
    if not api_key or any(not 33 <= ord(char) <= 126 for char in api_key):
        raise ConfigurationError("请在环境变量 DEEPSEEK_API_KEY 中设置有效密钥（不能包含空白或控制字符）。")


def validate_output_paths(task: TaskSpec, runs_root: Path) -> None:
    """在启动前检查显式预期输出的相对路径与工作区边界，不创建输出文件。"""
    try:
        workspace = Workspace(task.workspace, runs_root)
        for value in task.expected_outputs:
            ArtifactRef(location="workspace", path=value)
            path = Path(value)
            if not value.strip() or path.anchor or path.drive or ".." in path.parts:
                raise ConfigurationError("预期输出必须是工作区内的相对路径。")
            workspace.resolve(value)
    except (WorkspaceError, ValidationError):
        raise ConfigurationError("预期输出路径无效、越出工作区或指向运行记录。") from None


def _collect_tool_state(result: RunResult, executor: ToolExecutor) -> None:
    """汇总执行器已登记的产物和最坏清理状态，取消路径也保留真实信息。"""
    rank = {"not_needed": 0, "complete": 1, "unknown": 2, "incomplete": 3}
    status = getattr(executor, "cleanup_status", "not_needed")
    if status in rank and rank[status] > rank[result.cleanup_status]:
        result.cleanup_status = status
    for raw in getattr(executor, "artifact_refs", []):
        reference = ArtifactRef.model_validate(raw)
        if reference not in result.artifacts:
            result.artifacts.append(reference)


def _collect_expected_outputs(context: RunContext, runs_root: Path) -> None:
    """登记显式声明且存在的命令产物；缺失只作交付提示，不冒充内容验收。"""
    workspace = Workspace(context.manifest.task.workspace, runs_root)
    for value in context.manifest.task.expected_outputs:
        try:
            path = workspace.resolve(value, must_exist=True)
            exists = path.is_file()
        except (WorkspaceError, OSError):
            exists = False
        if exists:
            reference = ArtifactRef(location="workspace", path=path.relative_to(workspace.workspace).as_posix())
            if reference not in context.result.artifacts:
                context.result.artifacts.append(reference)
                context.emit("artifact_registered", {"artifact": reference.model_dump(mode="json")})
        elif context.result.execution_status == "completed":
            context.result.missing_artifacts.append(value)
            context.emit("artifact_missing", {"path": value})


def _apply_verification(context: RunContext, detail: VerificationResult) -> None:
    """把独立验收详情合入执行已完成的任务，返回 None 并提交验收结果事件。

    context 持有唯一运行结果；detail 是宿主验收事实。只提升最坏清理状态，
    将验收输出索引合入产物，不覆盖执行状态，也不会重新请求模型修改产物。
    """
    result = context.result
    result.verification = detail
    result.validation_status = detail.status
    result.stop_reason = {"passed": "verification_passed", "failed": "verification_failed"}.get(detail.status, detail.reason)
    rank = {"not_needed": 0, "complete": 1, "unknown": 2, "incomplete": 3}
    if rank[detail.cleanup_status] > rank[result.cleanup_status]:
        result.cleanup_status = detail.cleanup_status
    for reference in detail.artifacts:
        if reference not in result.artifacts:
            result.artifacts.append(reference)
    context.emit("verification_result", {"verification": detail.model_dump(mode="json")})


def _interrupted_verification(verifier: Verifier, reason: str, *, timed_out: bool = False, cancelled: bool = False) -> VerificationResult:
    """保留被中断验收的输出和清理事实，返回 error 详情而非改写任务执行状态。

    verifier.last_result=None 表示尚无子进程结果；reason 是稳定中断原因。
    timed_out 与 cancelled 分别标记预算耗尽和外部取消，二者由调用方明确区分。
    """
    previous = verifier.last_result or VerificationResult(status="error", reason=reason)
    return previous.model_copy(update={"status": "error", "reason": reason, "timed_out": timed_out, "cancelled": cancelled})


async def run_task(
    manifest: RunManifest,
    runs_root: Path,
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    model_client: ModelClient | None = None,
    executor: ToolExecutor | None = None,
    cancellation_state: CancellationState | None = None,
    on_context: Callable[[RunContext], None] | None = None,
) -> RunOutcome:
    """执行模型任务及可选独立验收，返回已保存的结果、目录和事件数。

    manifest 提供任务、预算和宿主验收入口；runs_root 是记录根目录；api_key 只交给
    模型适配器。三个可选组件参数默认 None，表示使用真实实现，可注入离线测试组件。
    cancellation_state 默认 None；只按宿主明确标记区分SIGINT、网页取消与服务关闭。
    on_context 默认None；非空时在记录创建后、首次模型调用前同步通知一次，返回值忽略。
    接收方只能读取该可变上下文供进度查询，不能修改状态或另行写入运行事件。
    总秒级预算同时覆盖模型、工具与验收；验收异常保留 completed 执行状态。
    函数会调用外部服务、执行工具并写文件；StorageError 立即传播，不伪造落盘成功。
    """
    validate_execution(manifest, api_key)
    validate_output_paths(manifest.task, runs_root)
    try:
        tool_executor = executor or ToolExecutor(manifest.task.workspace, runs_root, manifest.config)
    except (OSError, RuntimeError, ValueError):
        raise StorageError("运行记录目录无法解析，请检查目录和符号链接。") from None
    store = RunStore.create(runs_root, manifest)
    if hasattr(tool_executor, "bind_run"):
        tool_executor.bind_run(store.run_dir)
    result = RunResult(run_id=manifest.run_id, execution_status="running", usage_complete=True)
    started_at = asyncio.get_running_loop().time()
    deadline = started_at + manifest.config.limits.run_timeout_seconds
    context = RunContext(manifest, store, result, deadline, started_at=started_at)
    verifier = Verifier(manifest.config.limits, store.run_dir) if manifest.task.verifier is not None else None
    from .agent import run_agent

    try:
        if on_context is not None:
            on_context(context)
        async with asyncio.timeout_at(deadline):
            if model_client is not None:
                await run_agent(context, model_client, tool_executor)
            else:
                async with DeepSeekClient(api_key, manifest.config, on_attempt=context.http_attempt, transport=transport) as client:
                    await run_agent(context, client, tool_executor)
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            # 完成模型循环后才允许验收；后面不会再调度模型或工具修改工作区。
            result.execution_status = "completed"
            result.stop_reason = "final_answer"
            context.emit("execution_completed", {"model_turns": result.model_turns, "tool_calls": result.tool_calls})
            if verifier is not None:
                context.emit("verification_started", {"timeout_seconds": manifest.task.verifier.timeout_seconds})
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
                detail = await verifier.run(manifest.task.verifier, manifest.task.workspace, manifest.run_id)
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
                _apply_verification(context, detail)
    except TimeoutError:
        if result.execution_status == "completed" and verifier is not None:
            _apply_verification(context, _interrupted_verification(verifier, "run_timeout", timed_out=True))
        else:
            result.execution_status = "timed_out"
            result.stop_reason = "run_timeout"
            result.usage_complete = False
    except asyncio.CancelledError as error:
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
            _apply_verification(context, _interrupted_verification(verifier, reason, cancelled=status == "cancelled"))
        else:
            result.execution_status, result.stop_reason = status, reason
            result.usage_complete = False
        # 不保存异常正文、Task repr、代码行内容或局部变量，防止输入与凭据泄漏。
        frames = traceback.extract_tb(error.__traceback__)[-16:]
        context.emit("cancellation_received", {
            "source": source, "task_cancelling": cancelling, "phase": context.phase,
            "trace": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno} for frame in frames],
        })
    except AgentStopped as error:
        result.execution_status, result.stop_reason = error.status, error.reason
    except ModelError as error:
        result.execution_status = "timed_out" if error.code == "model_timeout" else "failed"
        result.stop_reason, result.usage_complete = error.code, False
        context.emit("model_request_failed", {"model_turn": result.model_turns, "code": error.code})
    except StorageError:
        raise
    except Exception as error:
        if result.execution_status == "completed" and verifier is not None:
            _apply_verification(context, VerificationResult(status="error", reason="verifier_runtime_error", cleanup_status="unknown"))
        else:
            result.execution_status, result.stop_reason, result.usage_complete = "failed", "internal_error", False
        context.emit("runtime_error", {"code": result.stop_reason, "error_type": type(error).__name__})

    _collect_tool_state(result, tool_executor)
    _collect_expected_outputs(context, runs_root)
    if result.verification is None:
        reason = "not_configured" if verifier is None else "execution_not_completed"
        context.emit("verification_skipped", {"reason": reason})
    result.active_duration_seconds = context.active_duration()
    store.finalize(result)
    return RunOutcome(result, store.run_dir, context.event_count + 1)


def exit_code(result: RunResult) -> int:
    """把 result 映射为整数退出码；取消/总超时即使发生于验收也优先返回 130/124。

    执行或验收失败返回 1，完成但未验收返回 3；只有完成且验收通过、清理可确认才为 0。
    本函数无副作用，允许 CLI 与后续前端服务共用同一判断。
    """
    if result.execution_status == "cancelled" or result.stop_reason in {"user_cancelled", "task_cancelled", "service_shutdown"}:
        return 130
    if result.execution_status == "timed_out" or result.stop_reason == "run_timeout":
        return 124
    if result.execution_status != "completed" or result.cleanup_status in {"incomplete", "unknown"}:
        return 1
    if result.validation_status == "unverified":
        return 3
    return 0 if result.validation_status == "passed" else 1
