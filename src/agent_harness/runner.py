"""单次运行的生命周期与计数；超时和取消统一在这里保存终态。"""

import asyncio
import traceback
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import ConfigurationError
from .schemas import ArtifactRef, RunManifest, RunResult, TaskSpec
from .providers.base import ModelClient, ModelError
from .providers.deepseek import DeepSeekClient
from .store import RunStore, StorageError
from .tools.registry import ToolExecutor
from .workspace import Workspace, WorkspaceError


@dataclass
class CancellationState:
    """记录 CLI 实际收到的 SIGINT，避免将其他取消异常猜测为用户操作。"""

    user_requested: bool = False


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

    def emit(self, kind: str, payload: dict | None = None) -> None:
        """提交一条事件，并在写入成功后更新上下文中的事件数量。"""
        self.event_count = self.store.append(kind, payload).seq
        phases = {
            "model_request_started": "model_request",
            "model_response": "model_response",
            "tool_started": "tool_execution",
            "tool_result": "tool_result",
        }
        self.phase = phases.get(kind, self.phase)

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
    """在创建记录和发请求前检查密钥，并拒绝尚未接入的工具及独立验收。"""
    if manifest.task.verifier is not None:
        raise ConfigurationError("本切片尚未实现独立验收；请移除 --verifier，或使用 --prepare-only 预检。")
    if not api_key or any(not 33 <= ord(char) <= 126 for char in api_key):
        raise ConfigurationError("请在环境变量 DEEPSEEK_API_KEY 中设置有效密钥（不能包含空白或控制字符）。")


def validate_output_paths(task: TaskSpec, runs_root: Path) -> None:
    """在启动前检查显式预期输出的相对路径与工作区边界，不创建输出文件。"""
    try:
        workspace = Workspace(task.workspace, runs_root)
        for value in task.expected_outputs:
            path = Path(value)
            if not value.strip() or path.anchor or path.drive or ".." in path.parts:
                raise ConfigurationError("预期输出必须是工作区内的相对路径。")
            workspace.resolve(value)
    except WorkspaceError:
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


async def run_task(
    manifest: RunManifest,
    runs_root: Path,
    api_key: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    model_client: ModelClient | None = None,
    executor: ToolExecutor | None = None,
    cancellation_state: CancellationState | None = None,
) -> RunOutcome:
    """管理单次任务从启动校验到终态提交的完整生命周期。

    总截止时间覆盖模型请求、重试和工具循环；超时、取消及模型错误统一转为结果。
    transport、model_client 和 executor 可注入离线实现，正常 CLI 使用真实组件。
    正常收尾返回结果与记录位置；存储故障直接上抛，避免继续调度却丢失执行记录。
    """
    # 依赖注入只用于离线测试；CLI 不提供任意 URL 或绕过凭据检查的选项。
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
    deadline = asyncio.get_running_loop().time() + manifest.config.limits.run_timeout_seconds
    context = RunContext(manifest, store, result, deadline)
    # 延迟导入避免 Agent Loop 和生命周期上下文相互导入。
    from .agent import run_agent

    try:
        async with asyncio.timeout_at(deadline):
            if model_client is not None:
                await run_agent(context, model_client, tool_executor)
            else:
                async with DeepSeekClient(
                    api_key, manifest.config, on_attempt=context.http_attempt, transport=transport
                ) as client:
                    await run_agent(context, client, tool_executor)
        # 同步短操作不能被异步定时器抢占；返回后补查截止时间。
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
        result.execution_status = "completed"
        result.stop_reason = "final_answer"
    except TimeoutError:
        result.execution_status = "timed_out"
        result.stop_reason = "run_timeout"
        result.usage_complete = False
    except asyncio.CancelledError as error:
        task = asyncio.current_task()
        cancelling = task.cancelling() if task is not None else 0
        if cancellation_state is not None and cancellation_state.user_requested:
            source = "user_sigint"
            result.execution_status = "cancelled"
            result.stop_reason = "user_cancelled"
        elif cancelling:
            source = "task_cancel"
            result.execution_status = "cancelled"
            result.stop_reason = "task_cancelled"
        else:
            # 网络栈直接抛出取消异常时没有 task.cancel；这属于执行故障。
            source = "unexpected_exception"
            result.execution_status = "failed"
            result.stop_reason = "unexpected_cancellation"
        result.usage_complete = False
        # 只保存源文件名、函数和行号；异常正文、任务 repr、局部变量可能含凭据。
        frames = traceback.extract_tb(error.__traceback__)[-16:]
        context.emit("cancellation_received", {
            "source": source,
            "task_cancelling": cancelling,
            "phase": context.phase,
            "trace": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno} for frame in frames],
        })
    except AgentStopped as error:
        result.execution_status = error.status
        result.stop_reason = error.reason
    except ModelError as error:
        result.execution_status = "timed_out" if error.code == "model_timeout" else "failed"
        result.stop_reason = error.code
        result.usage_complete = False
        context.emit("model_request_failed", {"model_turn": result.model_turns, "code": error.code})
    except StorageError:
        # 磁盘故障后立即停止，不尝试写一个可能再次失败的伪终态。
        raise
    except Exception as error:
        result.execution_status = "failed"
        result.stop_reason = "internal_error"
        result.usage_complete = False
        # 不保存异常正文：网络库异常和验证异常可能携带输入或认证信息。
        context.emit("runtime_error", {"code": "internal_error", "error_type": type(error).__name__})

    _collect_tool_state(result, tool_executor)
    _collect_expected_outputs(context, runs_root)
    context.emit("verification_skipped", {"reason": "not_configured"})
    store.finalize(result)
    return RunOutcome(result, store.run_dir, context.event_count + 1)


def exit_code(result: RunResult) -> int:
    """把运行状态映射为退出码：取消 130、超时 124、失败 1、未验收 3、通过 0。"""
    if result.execution_status == "cancelled":
        return 130
    if result.execution_status == "timed_out":
        return 124
    if result.execution_status != "completed" or result.cleanup_status in {"incomplete", "unknown"}:
        return 1
    if result.validation_status == "unverified":
        return 3
    return 0 if result.validation_status == "passed" else 1
