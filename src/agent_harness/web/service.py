"""本地 Web 任务管理：按 Run 调度真实 Runner，读取事实记录，不另造执行状态。"""

import asyncio
from contextlib import asynccontextmanager
import json
from dataclasses import dataclass
import os
import re
from pathlib import Path
import subprocess
import shlex
from collections.abc import Callable
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from agent_harness.config import ConfigurationError, load_config, prepare_task
from agent_harness.providers.base import ModelClient, ModelError
from agent_harness.model_selection import valid_model_key
from agent_harness.runtime.runner import CancellationState, RunContext, run_task, validate_auto_repair, validate_execution
from agent_harness.execution.python import PythonExecutionError, prepare_python_execution, python_command
from agent_harness.schemas import ContextAction, ContextCheck, ContextView, PlanningState, RunConfig, RunEvent, RunManifest, RunOrigin, RunResult, VerificationAttempt, VerifierSpec
from agent_harness.projects import ProjectError, ProjectManager, ProjectPreset, ProjectReview, ProjectWorkspace
from agent_harness.storage.runs import RunHistory, RunStore, StorageError
from agent_harness.context.skill_library import SkillLibrary
from agent_harness.context.skill_runtime import recorded_skills
from agent_harness.execution.local import LocalChangeStore
from agent_harness.storage.snapshots import SnapshotError
from agent_harness.storage.sessions import SessionError
from agent_harness.work.planning import planning_from_history
from agent_harness.work.exports import materialize_plan_export
from agent_harness.subagents.service import read_child, report_page, SubagentError
from agent_harness.subagents.reports import partial_report
from agent_harness.schemas import SubagentRecord
from agent_harness.schemas import ProjectFileSnapshot
from agent_harness.web.file_references import check_reference_budget
from agent_harness.web.config import WebConfig
from agent_harness.web.conversations import ConversationService
from agent_harness.web.conversation_index import ConversationIndex
from agent_harness.web.project_catalog import ProjectCatalog
from agent_harness.web.folder_picker import FolderPicker
from agent_harness.execution.scheduling import Coordinator, resources, host_limits
from agent_harness.web.schemas import CreateRunRequest, EventPage, LifecycleStageView, RunView


class WebServiceError(Exception):
    """携带固定 HTTP 状态与安全提示；不保存输入值、底层异常或密钥。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        """保存 status HTTP 状态、code 稳定原因与 message 中文提示；返回 None。"""
        self.status = status  # 响应状态码。
        self.code = code  # 前端可识别的稳定错误码。
        self.message = message  # 固定或受控中文提示。
        super().__init__(message)


@dataclass
class ActiveRun:
    """按 Run 独立保存控制句柄；结果事实仍归 Runner 与 RunStore 所有。"""

    manifest: RunManifest  # 后台任务完整输入；不包含密钥值。
    cancellation: CancellationState  # 取消来源，在调用 task.cancel 前设置。
    started: asyncio.Future[None]  # on_context 到达后完成，防止任务启动前被取消。
    task: asyncio.Task[None] | None = None  # 后台协程；创建后立即绑定。
    context: RunContext | None = None  # Runner 回调交出的实时进度，启动前为空。
    owner: str = ""  # 持久资源租约所属作业编号。
    cancelling: bool = False  # 首次请求取消后保持 True，不重复打断进程清理。
    finalizing: bool = False  # Runner 已退出、正在固化 diff，此时不能再次取消收尾。
    startup_failed: bool = False  # 首个上下文尚未建立时运行异常。


def _phase(events: list[RunEvent]) -> str:
    """根据已落盘 events 推导展示阶段并返回字符串，不猜测未记录的动作。"""
    phases = {
        "run_created": "created",
        "run_started": "starting",
        "model_request_started": "model_request",
        "context_compaction_started": "context_compaction",
        "model_response": "model_response",
        "tool_started": "tool_execution",
        "tool_result": "tool_result",
        "execution_completed": "execution_completed",
        "verification_started": "verification",
        "repair_started": "repair",
        "artifact_collection_started": "artifact_collection",
        "verification_result": "verification_result",
        "run_finished": "finished",
        "budget_handoff_started": "budget_handoff",
    }
    current = "created"
    for event in events:
        current = "context_compaction" if event.type == "model_request_started" and event.payload.get("purpose") == "summary" else phases.get(event.type, current)
        if event.type == "hook_record" and event.payload["hook"]["status"] in {"running", "waiting"}:
            current = "hook_execution" if event.payload["hook"]["status"] == "running" else "action_approval"
        if event.type == "subagent_snapshot" and event.payload.get("subagent", {}).get("status") == "running":
            current = "subagent_execution"
        if event.type == "run_finished":
            reason = event.payload.get("result", {}).get("stop_reason")
            if reason in {"planning_complete", "plan_rejected", "user_question", "plan_approval", "interaction_cancelled", "action_approval", "permission_rejected", "permission_cancelled", "verification_permission_denied", "user_paused", "subagent_paused", "team_attention_required"}:
                current = reason
    return current


def _lifecycle_view(events: list[RunEvent]) -> list[LifecycleStageView]:
    """从有序 events 返回每个阶段最近的诊断；按事件时间排序，不受分页窗口影响。"""
    latest: dict[str, RunEvent] = {}
    for event in events:
        if event.type == "pipeline_stage":
            latest[str(event.payload.get("stage", ""))] = event
    return [LifecycleStageView.model_validate({**event.payload, "seq": event.seq})
            for event in sorted(latest.values(), key=lambda event: event.seq)]


def _context_view(events: list[RunEvent]) -> ContextView | None:
    """从有序 events 还原指令快照与最新检查；尚未准备时返回 None，不从当前文件补造数据。

    本函数只读取事件，不读取工作区；受损上下文载荷抛 ValidationError，由视图层
    转为警告。只采用 context_prepared 之后的检查，避免无来源的统计被当作有效记录。
    """
    view = None
    for event in events:
        if event.type == "context_prepared":
            view = ContextView.model_validate(event.payload)
        elif event.type == "context_checked" and view is not None:
            view.last_check = ContextCheck.model_validate(event.payload)
        elif event.type in {"memory_prepared", "memory_selected", "memory_read", "memory_proposed"} and view is not None:
            view.memory = event.payload["memory"]
        elif event.type == "context_action" and view is not None:
            view.actions.append(ContextAction.model_validate(event.payload))
            view.actions = view.actions[-50:]
            view.action_count += 1
        elif event.type == "model_request_started" and view is not None and event.payload.get("purpose") == "summary":
            view.summary_calls += 1
    if view is not None:
        view.skills, view.skill_resources = recorded_skills(events)
    return view


def _unfinished_result(history: RunHistory) -> RunResult:
    """按history有效事件还原本次已开始计数和已知token；缺终态一律标用量不完整。

    本函数不补造终态、回答或测试结论；主动耗时仅采用最后落盘elapsed的下界。
    """
    result = RunResult(
        run_id=history.manifest.run_id, execution_status="running" if history.interrupted else "created",
        model_turns=sum(event.type == "model_request_started" for event in history.events),
        http_attempts=sum(event.type == "model_http_attempt" for event in history.events),
        tool_calls=sum(event.type == "tool_requested" for event in history.events),
        usage_complete=False, active_duration_seconds=history.events[-1].elapsed_ms / 1000 if history.events else 0,
        planning=planning_from_history(history),
    )
    # 未封存终态不等于每个旧进程都失联；采用已配对结果的清理事实，只有在途动作未知。
    pending_commands: set[str] = set()  # 已开始但尚无结果的命令编号。
    background_commands: dict[str, str] = {}  # 独立命令快照，接纳回执不能代替进程结束事实。
    verifier_pending = False  # 宿主验收已启动而未得到清理结论。
    cleanup_rank = {"not_needed": 0, "complete": 1, "unknown": 2, "incomplete": 3}
    for event in history.events:
        if event.type in {"hooks_prepared", "hooks_restored"}:
            from copy import deepcopy
            result.hooks = deepcopy(event.payload["hooks"])
        elif event.type == "hook_queued":
            result.hooks.setdefault("queue", []).append(event.payload)
        elif event.type == "hook_dequeued":
            result.hooks["queue"] = [item for item in result.hooks.get("queue", []) if (item["event"], item["key"]) != (event.payload["event"], event.payload["key"])]
        elif event.type == "hook_record":
            record = event.payload["hook"]
            records = result.hooks.setdefault("records", [])
            result.hooks["records"] = [item for item in records if item["id"] != record["id"]] + [record]
            if record["status"] == "running":
                result.hooks["calls"] = result.hooks.get("calls", 0) + 1
            elif record.get("seconds") is not None:
                result.hooks["seconds"] = result.hooks.get("seconds", 0) + record["seconds"]
        if event.type == "command_snapshot":
            command = event.payload["command"]
            background_commands[command["id"]] = command["status"]
            command_cleanup = command.get("cleanup_status", "unknown")
            if command_cleanup in cleanup_rank and cleanup_rank[command_cleanup] > cleanup_rank[result.cleanup_status]:
                result.cleanup_status = command_cleanup
        if event.type == "permissions_prepared":
            from agent_harness.permissions.models import PermissionState
            result.permissions = PermissionState.model_validate(event.payload["state"])
        elif event.type == "permission_requested":
            result.permissions.pending = event.payload["action"]
        if event.type == "subagent_snapshot":
            record = SubagentRecord.model_validate_json(json.dumps(event.payload["subagent"]))
            result.subagents = [item for item in result.subagents if item.run_id != record.run_id] + [record]
        if event.type == "team_snapshot":
            result.team = event.payload["team"]
        if event.type == "tasks_snapshot":
            result.tasks = event.payload["state"]
        if event.type == "tool_started" and event.payload.get("name") == "run_command":
            pending_commands.add(str(event.payload.get("call_id")))
        elif event.type == "tool_result" and event.payload.get("name") == "run_command":
            pending_commands.discard(str(event.payload.get("call_id")))
            observation = event.payload.get("result")
            data = observation.get("data") if isinstance(observation, dict) else None
            status = data.get("cleanup_status", "not_needed" if data.get("accepted") else "unknown") if isinstance(data, dict) else "unknown"
            if status not in cleanup_rank:
                status = "unknown"
            if cleanup_rank[status] > cleanup_rank[result.cleanup_status]:
                result.cleanup_status = status
        elif event.type == "verification_started":
            verifier_pending = True
        elif event.type == "verification_result":
            verifier_pending = False
            verification = event.payload.get("verification")
            try:
                attempt = VerificationAttempt.model_validate({"repair_attempt": event.payload["repair_attempt"], "result": verification})
                result.verification_attempts.append(attempt)
                result.artifacts.extend(reference for reference in attempt.result.artifacts if reference not in result.artifacts)
            except (KeyError, ValidationError):
                pass  # 损坏事件不补造检查结论，历史读取的警告仍由RunStore保留。
            status = verification.get("cleanup_status", "unknown") if isinstance(verification, dict) else "unknown"
            if status not in cleanup_rank:
                status = "unknown"
            if cleanup_rank[status] > cleanup_rank[result.cleanup_status]:
                result.cleanup_status = status
        elif event.type == "repair_started":
            attempt = event.payload.get("repair_attempt")
            if type(attempt) is int and 1 <= attempt <= 2:
                result.repair_attempts = attempt
        if event.type == "model_response":
            usage = event.payload.get("usage")
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(field) if isinstance(usage, dict) else None
                if type(value) is int and value >= 0:
                    setattr(result.usage, field, getattr(result.usage, field) + value)
    for record in result.subagents:
        if record.log_parent_id is not None and record.log_parent_id != history.manifest.run_id:
            continue  # 接续保留的旧兄弟快照已计入此前预算，不能再加到当前段用量。
        # 父事件仅保存子快照；从各自最新快照累计一次，避免把每次进度重复相加。
        result.model_turns += record.model_turns
        result.tool_calls += record.tool_calls
        result.http_attempts += record.http_attempts
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            setattr(result.usage, key, getattr(result.usage, key) + record.usage.get(key, 0))
    hook_running = any(item["status"] == "running" for item in result.hooks.get("records", []))
    if (pending_commands or verifier_pending or hook_running or any(value in {"starting", "running", "stopping"} for value in background_commands.values())) and cleanup_rank["unknown"] > cleanup_rank[result.cleanup_status]:
        result.cleanup_status = "unknown"
    return result


class WebRunManager:
    """管理本地多个独立运行、受限提交、取消和只读历史，不因页面断开而取消。"""

    def __init__(
        self,
        web_config: WebConfig,
        run_config: RunConfig,
        runs_root: Path,
        *,
        model_client: ModelClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        key_provider: Callable[[RunConfig], str] | None = None,
    ) -> None:
        """固定宿主配置和 runs_root；可选 client/transport 仅供测试注入，None 用真实实现。

        key_provider 为宿主凭据读取器，None 使用 CLI 环境变量；返回值只传 Runner。
        构造不创建运行或调用模型，返回 None；路径解析失败由服务启动层处理。
        """
        self.web_config = web_config  # 宿主工作区和验收器白名单。
        self.run_config = run_config.model_copy(deep=True)  # 页面只能读取，不原地修改默认配置。
        self.runs_root = runs_root.resolve()  # 独立运行记录根目录。
        from agent_harness.mcp.store import McpStore
        self.mcp = McpStore(self.runs_root)  # 连接目录读取不启动任何服务。
        self.skills = SkillLibrary(self.runs_root, self.run_config.skill_roots)  # 管理页和任务共用的本机目录。
        self.mcp_credential_provider = None  # 桌面安装异步原生读取器；CLI/Web 使用环境引用。
        self.catalog = ProjectCatalog(self.runs_root, web_config)  # 明确打开目录的持久登记，首轮与续轮共用。
        self.folder_picker = FolderPicker()  # 独立原生选择器，读取页面不创建窗口。
        self._model_client = model_client  # 离线注入；默认无客户端，共用 Runner 创建逻辑。
        self._transport = transport  # 可选 HTTP 测试传输，不在配置或响应序列化。
        self._key_provider = key_provider  # 桌面凭据来自 Windows 凭据库，不进入运行配置。
        self._lock = asyncio.Lock()  # 串行化短暂的准备与管理操作，不覆盖运行期。
        self._submissions: dict[tuple[UUID, UUID], asyncio.Task] = {}  # 会话/请求对应的准备，断线后继续并幂等等待。
        self._active: dict[UUID, ActiveRun] = {}  # 每个主 Run 独立的生命周期与取消句柄。
        self._workspace_actions: set[asyncio.Task] = set()  # 人工文件操作请求，关停时等待其线程及资源释放。
        self._warnings: dict[UUID, list[str]] = {}  # 服务异常安全提示，不写伪造 run_finished。
        self._history_cache: dict[UUID, tuple[tuple[object, ...], RunHistory, int]] = {}  # 最近8条/32MiB记录；活动任务可单独保留。
        self.projects = ProjectManager(self.runs_root, web_config.worktrees_root)  # 独立 Git 归属与审阅事实。
        self.local_changes = LocalChangeStore(self.runs_root, (self.projects.worktrees_root,))  # 原目录仅保存差异，不派生执行目录。
        self._recovery_tasks: dict[UUID, asyncio.Task[ProjectReview]] = {}  # 并发页面共享一次恢复，全部等待同一完成结果。
        self._recovered: set[UUID] = set()  # 每个中断任务最多恢复捕获一次，避免 GET 重跑 Git。
        self._closing = False  # 关停后永久拒绝新提交。
        self.conversation_index = ConversationIndex(self)  # 项目下按会话聚合的只读导航。
        self.conversations = ConversationService(self)  # 会话读取、幂等追加和文件快照复用本调度器。
        from agent_harness.web.conversation_management import ConversationManager
        self.management = ConversationManager(self)  # 可变标题、归属及生命周期，与不可变运行事实分开。
        self.coordinator = Coordinator(self.management.store, web_config.max_concurrent_runs, web_config.max_model_requests, web_config.max_processes)  # 全入口共用资源与宿主锁。
        from agent_harness.web.integrations import IntegrationService
        self.integrations = IntegrationService(self)  # 人工成果集成复用队列，不创建模型运行或执行后端。
        from agent_harness.web.jobs import JobQueue
        self.jobs = JobQueue(self)  # 同一宿主的持久资源队列，启动恢复由明确偏好控制。
        from agent_harness.automations.service import AutomationService
        self.automations = AutomationService(self)  # 只增加规则触发入口，实际执行继续复用jobs。

    @property
    def active_run_ids(self) -> list[UUID]:
        """返回全部尚未收尾的主运行编号列表；不包含只有作业而尚无 Run 的准备阶段。"""
        return [identifier for identifier, item in self._active.items() if item.task is not None and not item.task.done()]

    def active_for_session(self, session: str) -> ActiveRun | None:
        """按 session 会话身份定位活动句柄；不存在返回 None，不借用其他项目的执行者。"""
        return next((item for item in self._active.values() if str(item.manifest.session_id or item.manifest.run_id) == session), None)

    @asynccontextmanager
    async def workspace_action(self, run_id: UUID):
        """为 run_id 的人工工作树操作占用来源资源；退出释放，进程外的文件动作必须自行等待线程收尾。"""
        if self._closing:
            raise WebServiceError(503, "service_closing", "本地服务正在关闭。")
        history = self._read_history(run_id)
        source = history.manifest.origin.registered_path if history.manifest.origin else history.manifest.task.workspace
        owner = str(uuid4())
        scope = resources([source], self.web_config.serial_resource_groups)
        self.coordinator.acquire(owner, scope, str(history.manifest.session_id or run_id), str(run_id), recover_run=False)
        task = asyncio.current_task()  # 将持有该租约的请求纳入宿主关停，避免线程仍在修改文件时退出。
        self._workspace_actions.add(task)
        try:
            yield
        finally:
            try:
                self.coordinator.release(owner)
                self.jobs.kick()
            finally:
                self._workspace_actions.discard(task)

    def options(self) -> dict:
        """返回可公开的配置、预设和槽位；密钥只报告是否存在，不返回环境值。"""
        project_presets, _, recent = self.catalog.snapshot()
        # Git探测可能耗时；之后不再重读目录登记，避免并发打开项目造成列表身份错配。
        project_options = [self._project_option(item) for item in project_presets]
        for item in recent:
            if item["kind"] == "project":
                item.update(next(option for option in project_options if option["id"] == item["id"]))
            else:
                item.update(available=Path(item["path"]).is_dir(), error=None if Path(item["path"]).is_dir() else "目录已移动或无法访问。")
        return {
            "recent_projects": recent,
            "folder_picker_available": self.folder_picker.available,
            "config": self.run_config,
            "skills": self.skills.discover(),
            "mcp_tools": self.mcp.options(),
            "model_providers": self.model_options(),
            "verifiers": [
                {"id": item.id, "label": item.label, "description": item.description}
                for item in self.web_config.verifiers
            ],
            "active_run_ids": self.active_run_ids,
            "scheduler": self.jobs.snapshot(),
            "queue_pending": sum(job["status"] == "queued" for job in self.jobs.list()),
            "queue_paused": self.jobs.paused,
        }

    def api_key(self, config: RunConfig) -> str:
        """按 config 取得宿主凭据并返回；默认从环境读取，桌面注入凭据库，不记录值。"""
        if not config.requires_model_key:
            return ""
        return self._key_provider(config) if self._key_provider is not None else os.environ.get(config.api_key_env, "") if config.api_key_env else ""

    def model_options(self) -> dict:
        """返回用户登记的服务商和公开凭据状态，空目录返回空映射。"""
        options = {}
        for provider, definition in self.run_config.custom_providers.items():
            config = RunConfig(provider=provider, custom_providers={provider: definition})
            options[provider] = {
                **definition.model_dump(exclude={"generation"}),
                "version": definition.version,
                "key_configured": valid_model_key(self.api_key(config)),
            }
        return options

    def check_model_versions(self, versions: dict[str, str]) -> None:
        """核对任务登记的 versions。已停用、移除或端点已变化时抛受控冲突。"""
        current = self.run_config.custom_providers
        for provider, version in versions.items():
            definition = current.get(provider)
            if definition is None or not definition.enabled:
                raise WebServiceError(409, "model_provider_disabled", "所选自定义服务商已停用或移除。")
            if definition.version != version:
                raise WebServiceError(409, "model_provider_changed", "自定义服务商的连接已变更，请用当前配置新建会话。")

    def role_model_key(self, config: RunConfig) -> str:
        """核对独立角色 config 的当前端点并读取凭据，过期时返回可记录的模型错误。"""
        try:
            self.check_model_versions(config.model_versions())
        except WebServiceError as error:
            raise ModelError(error.code, error.message) from None
        return self.api_key(config)

    def _project_option(self, preset: ProjectPreset) -> dict:
        """只读探测 preset 的固定 HEAD 与 dirty 标记；复用有界 Git，异常仅使该项目不可用。"""
        option = {
            "id": preset.id, "label": preset.label, "path": str(preset.path),
            "base_ref": "HEAD", "head_commit": None, "source_dirty": False,
            "test_argv": preset.test_argv, "test_timeout_seconds": preset.test_timeout_seconds,
            "python_executable": preset.python_executable,
            "test_command": subprocess.list2cmdline(preset.test_argv) if os.name == "nt" else shlex.join(preset.test_argv),
            "available": False, "error": None,
        }
        try:
            option.update(self.projects.inspect(preset))
            option["available"] = True
        except ProjectError as error:
            option["error"] = error.message
        return option

    async def submit(self, request: CreateRunRequest, *, run_id: UUID, job_id: str, memory_policy: dict, file_references: list[ProjectFileSnapshot]) -> RunView:
        """准备已认领资源的 request 并返回 RunView；run_id/job_id 是队列固定归属，memory_policy 是接纳时策略。

        file_references 为接纳时固定的资料。页面断开不取消准备；失败由原作业保存错误和释放资源。
        """
        if self._closing:
            raise WebServiceError(503, "service_closing", "本地服务正在关闭。")
        preparation = asyncio.create_task(self._start_request(request, run_id, job_id, memory_policy, file_references), name=f"harness-prepare-{run_id}")
        key = (run_id, run_id)
        self._submissions[key] = preparation

        def observe_completion(task: asyncio.Task[RunView]) -> None:
            """回收 task 异常及准备索引；连接断开不取消归属明确的后台准备。"""
            self._submissions.pop(key, None)
            if not task.cancelled():
                task.exception()
        preparation.add_done_callback(observe_completion)
        return await asyncio.shield(preparation)

    async def _start_request(self, request: CreateRunRequest, identifier: UUID, owner: str, memory_policy: dict, file_references: list[ProjectFileSnapshot]) -> RunView:
        """预检 request 后在已认领资源内准备 identifier；owner 绑定作业，memory_policy 固定本轮记忆。

        file_references 是排队时固定的正文与范围。密钥、预算与产物路径在 Git 创建前检查。
        Git 同步操作在线程执行，调用方取消也等待准备结束，防止工作树无人登记。
        """
        async with self._lock:
            if self._closing:
                raise WebServiceError(503, "service_closing", "本地服务正在关闭。")
            project_presets, workspace_presets, _ = self.catalog.snapshot()
            workspace = next((item for item in workspace_presets if item.id == request.workspace_id), None)
            preset = next((item for item in project_presets if item.id == request.project_id), None)
            if request.project_id is not None and preset is None:
                raise WebServiceError(400, "unknown_project", "请选择已登记的 Git 项目。")
            if preset is None and workspace is None:
                raise WebServiceError(400, "unknown_workspace", "请选择已登记的工作区。")
            verifier_path = None
            if request.verifier_id is not None:
                verifier = next((item for item in self.web_config.verifiers if item.id == request.verifier_id), None)
                if verifier is None:
                    raise WebServiceError(400, "unknown_verifier", "请选择已登记的验收器。")
                verifier_path = verifier.path
            source = preset.path if preset is not None else workspace.path
            prepared: ProjectReview | None = None  # 已创建但尚未交给 Runner，预检失败时可安全清理。
            try:
                merged = self.run_config.model_dump(mode="python")
                patch = request.config.model_dump(exclude_none=True, exclude_unset=True)
                for section in ("limits", "context", "memory"):
                    if section in patch:
                        merged[section] = {**merged[section], **patch.pop(section)}
                merged.update(patch)
                if memory_policy is not None:
                    merged["memory"] = memory_policy
                config = load_config(overrides=merged).freeze_models()
                self.check_model_versions(config.model_versions())
                if config.model_versions() != self.jobs.get(owner).get("model_versions", {}):
                    raise WebServiceError(409, "model_provider_changed", "排队期间服务商连接已变更，请重新提交。")
                source = preset.path if preset is not None else workspace.path
                task = prepare_task(
                    request.prompt, source, verifier_path, config.limits.verifier_timeout_seconds,
                    python_paths=(preset or workspace).python_paths,
                    python_executable=(preset or workspace).python_executable,
                )
                task = task.model_copy(update={"file_references": file_references})
                check_reference_budget(task, config.context.max_input_bytes)
                selected_id = preset.id if preset is not None else workspace.id
                mode = request.execution_mode
                if mode == 'isolated' and workspace is not None:
                    raise WebServiceError(400, 'workspace_copy_not_supported', '所选文件夹直接在原目录执行，不创建副本。')
                if mode == 'local' and request.base_ref != 'HEAD':
                    raise WebServiceError(400, 'local_base_ref', '原目录执行使用当前文件，不能切换Git基线；请选择隔离执行。')
                origin = RunOrigin(kind="project" if preset is not None else "workspace", preset_id=selected_id, registered_path=source.resolve(), verifier_id=request.verifier_id, execution_mode=mode, workspace_identity=self.local_changes.identity(source.resolve()) if mode == 'local' else None)
                required = request.mode == "plan" or config.planning_policy == "required"
                planning = PlanningState(phase="planning", reason="宿主要求先批准计划" if config.planning_policy == "required" else "用户选择先规划") if required else PlanningState()
                manifest = RunManifest(run_id=identifier, task=task, config=config, session_id=identifier, origin=origin, planning=planning)
                api_key = self.api_key(config)
                validate_execution(manifest, api_key)
                if config.auto_repair and (preset is None or not preset.test_argv):
                    validate_auto_repair(manifest)  # 缺少公开测试先拒绝，避免为无效修复请求创建工作树。
                if mode == 'local':
                    root = self.local_changes.verify(origin, task.workspace)
                    if preset is not None:
                        await asyncio.to_thread(self.projects.inspect, preset)  # 只读核对Git来源，不检出、不写索引。
                        task = task.model_copy(update={"verifier": self.project_verifier(preset, root)})
                        manifest = manifest.model_copy(update={"task": task})
                else:  # 隔离模式已经限定为 Git 项目。
                    preparation = asyncio.create_task(asyncio.to_thread(
                        self.projects.prepare, preset, identifier, request.base_ref
                    ))
                    try:
                        review = await asyncio.shield(preparation)
                    except asyncio.CancelledError:
                        # 线程中的 Git 不可安全强停；等待其保存归属后才退出提交请求。
                        await asyncio.shield(preparation)
                        raise
                    prepared = review
                    root = Path(review.worktree)
                    verifier = self.project_verifier(preset, root)
                    task = task.model_copy(update={"workspace": root, "verifier": verifier})
                    manifest = manifest.model_copy(update={"task": task})
                # 必须等实际目录确定后冻结，避免把原仓库源码路径带到新工作树。
                execution = prepare_python_execution(task.workspace, (preset or workspace).python_paths, python_executable=(preset or workspace).python_executable)
                task = task.model_copy(update={"python_execution": execution})
                manifest = manifest.model_copy(update={"task": task})
                validate_auto_repair(manifest)
                self.check_model_versions(config.model_versions())
            except (ConfigurationError, ProjectError, SnapshotError, ValidationError, PythonExecutionError, WebServiceError) as error:
                if prepared is not None:
                    try:
                        await asyncio.to_thread(self.projects.capture, identifier)
                        await asyncio.to_thread(self.projects.decide, identifier, "discard")
                    except ProjectError:
                        raise WebServiceError(503, "project_prepare_cleanup_failed", "任务尚未启动，项目工作树未能清理；请检查本地项目审阅记录。") from None
                if isinstance(error, WebServiceError):
                    raise
                if isinstance(error, (ProjectError, SnapshotError)):
                    raise WebServiceError(400, error.code, error.message) from None
                message = str(error) if isinstance(error, (ConfigurationError, PythonExecutionError)) else "项目测试工作目录或导入路径无效。"
                raise WebServiceError(400, "invalid_run", message) from None
            job = self.jobs.get(owner)
            if job.get("automation"):
                from agent_harness.schemas import AutomationOrigin
                manifest = manifest.model_copy(update={"automation": AutomationOrigin.model_validate(job["automation"])})
            return await self._launch(manifest, api_key, owner)

    @staticmethod
    def project_verifier(preset: ProjectPreset, root: Path) -> VerifierSpec | None:
        """按preset当前测试设置为实际root构建验收；空命令返回None，不启动进程。"""
        return VerifierSpec(
            kind="project_tests",
            argv=python_command([part.replace("{workspace}", str(root)) for part in preset.test_argv], prepare_python_execution(root, preset.python_paths, python_executable=preset.python_executable)),
            cwd=root, timeout_seconds=preset.test_timeout_seconds,
        ) if preset.test_argv else None

    async def _launch(self, manifest: RunManifest, api_key: str, owner: str) -> RunView:
        """为已认领的 owner 启动 manifest 并等待记录建立，返回初始视图；不另建执行循环。

        api_key 只传 Runner；本方法由首次提交和追加共同使用。启动记录失败时返回受控错误，
        会话中已提交的请求不会因重试再次执行。
        """
        item = ActiveRun(manifest, CancellationState(), asyncio.get_running_loop().create_future())
        item.owner = owner
        self._active[manifest.run_id] = item
        job = self.jobs.get(owner)
        job.update(status="running", session_id=str(manifest.session_id or manifest.run_id), execution_status="running")
        self.jobs.save(job)
        item.task = asyncio.create_task(self._execute(item, api_key), name=f"harness-web-{manifest.run_id}")
        await asyncio.wait({item.started, item.task}, return_when=asyncio.FIRST_COMPLETED)
        if item.context is None:
            raise WebServiceError(503, "run_start_failed", "任务未能建立运行记录，请检查本地配置与目录权限。")
        return self.get_run(manifest.run_id)

    async def _execute(self, item: ActiveRun, api_key: str) -> None:
        """让真实 Runner 执行 item；api_key 仅传模型，不进入结果、异常提示或日志。

        内部异常记录固定服务提示并释放槽，不伪造终态；返回 None。取消通过 Runner 收尾。
        """
        def on_context(context: RunContext) -> None:
            """保存 Runner 已建立的 context 并解锁提交响应；返回 None，不写额外事件。"""
            item.context = context
            if not item.started.done():
                item.started.set_result(None)
            if self._closing:
                # 服务关闭可能发生于 Git 线程准备期间；先建立可信取消记录，模型一次也不调用。
                item.cancellation.shutdown_requested = True
                item.cancelling = True
                raise asyncio.CancelledError
            if item.manifest.automation is not None:
                job = self.jobs.get(item.owner)
                if job.get("automation_stop_requested"):
                    item.cancelling = True
                    raise asyncio.CancelledError
        limit_token = host_limits.set(self.coordinator.limits)
        try:
            await run_task(
                item.manifest,
                self.runs_root,
                api_key,
                model_client=self._model_client,
                transport=self._transport,
                cancellation_state=item.cancellation,
                on_context=on_context,
                file_changes=self.local_changes if item.manifest.origin is None or item.manifest.origin.execution_mode == 'local' else None,
                project_manager=self.projects,
                mcp_credential_provider=self.mcp_credential_provider,
                model_credential_provider=self.role_model_key,
                execution_lease=self.coordinator.lease,
            )
        except asyncio.CancelledError:
            item.startup_failed = item.context is None
            self._warnings.setdefault(item.manifest.run_id, []).append("后台任务在运行器收尾之外被中断，请以已保存事件为准。")
        except Exception:
            item.startup_failed = item.context is None
            self._warnings.setdefault(item.manifest.run_id, []).append("后台执行或记录保存发生异常；未生成可信终态时，请检查本地运行记录。")
        finally:
            # 正常文件证据已在Runner终态前固定；启动失败的资源清理完成后才释放执行槽。
            item.finalizing = True
            if item.startup_failed and item.context is None and self._project_review(item.manifest.run_id) is not None:
                # 模型尚未启动的准备失败仍回收专属资源；正常证据已由Runner在终态前固化。
                try:
                    await asyncio.to_thread(self.projects.capture, item.manifest.run_id)
                    if item.manifest.parent_run_id is None:
                        await asyncio.to_thread(self.projects.decide, item.manifest.run_id, "discard")
                    else:
                        await asyncio.to_thread(self.projects.abandon_turn, item.manifest.run_id)
                except (ProjectError, OSError):
                    self._warnings.setdefault(item.manifest.run_id, []).append("未启动任务的工作树清理未完成，请检查项目审阅记录。")
            self._active.pop(item.manifest.run_id, None)
            host_limits.reset(limit_token)
            # 超预算快照只为活动订阅保留；任务结束后立即释放这个特例。
            cached = self._history_cache.get(item.manifest.run_id)
            if cached is not None and cached[2] > 32 * 1024 * 1024:
                self._history_cache.pop(item.manifest.run_id)

    def _read_history(self, identifier: UUID) -> RunHistory:
        """按三份记录文件的签名缓存 identifier 历史；返回可信快照，读取失败仍抛 StorageError。

        每次检查文件设备、标识、纳秒修改时间和字节数，追加事件/终态/摘要替换会使缓存失效。
        缓存最近8个运行且历史记录合计不超过32MiB；活动运行保留各自的当前快照。
        多个 SSE 订阅共享未变化的快照，不缓存读取错误。预算按记录原始字节估算。
        """
        directory = self.runs_root / str(identifier)
        signature: list[object] = []
        try:
            for name in ("run.json", "events.jsonl", "summary.json"):
                try:
                    metadata = (directory / name).stat()
                    signature.append((metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size))
                except FileNotFoundError:
                    signature.append(None)
        except OSError:
            raise StorageError("运行记录元数据无法读取") from None
        key = tuple(signature)
        cached = self._history_cache.get(identifier)
        if cached is not None and cached[0] == key:
            self._history_cache.pop(identifier)
            self._history_cache[identifier] = cached
            return cached[1]
        history = RunStore.read(self.runs_root, str(identifier))
        self._history_cache.pop(identifier, None)
        record_bytes = sum(part[3] for part in signature if isinstance(part, tuple))
        byte_budget = 32 * 1024 * 1024
        active_ids = set(self.active_run_ids)
        if record_bytes > byte_budget and identifier not in active_ids:
            return history
        self._history_cache[identifier] = (key, history, record_bytes)
        while (
            len(self._history_cache) > 8
            or sum(entry[2] for entry in self._history_cache.values()) > byte_budget
        ):
            removable = next((key for key in self._history_cache if key not in active_ids), None)
            if removable is None:
                break
            self._history_cache.pop(removable)
        return history

    def get_run(self, run_id: UUID | str) -> RunView:
        """读取 run_id 的可信记录并融合当前进度；无记录抛 404，坏记录抛 503。

        历史缺少终态时保留 created/running 并标记 interrupted，不猜测失败或取消。
        """
        try:
            identifier = UUID(str(run_id))
        except (ValueError, TypeError):
            raise WebServiceError(404, "run_not_found", "运行记录不存在。") from None
        try:
            history = self._read_history(identifier)
        except StorageError:
            if not (self.runs_root / str(identifier)).exists():
                raise WebServiceError(404, "run_not_found", "运行记录不存在。") from None
            raise WebServiceError(503, "run_unreadable", "运行记录无法读取，请检查本地文件。") from None
        item = self._active.get(identifier)
        active = identifier in self.active_run_ids
        if history.result is not None:
            result = history.result.model_copy(deep=True)
        elif active and item.context is not None:
            result = item.context.result.model_copy(deep=True)
        else:
            result = _unfinished_result(history)
        warnings = [*history.warnings, *self._warnings.get(identifier, [])]
        warnings.extend(event.payload["message"] for event in history.events if event.type == "memory_failed" and isinstance(event.payload.get("message"), str))
        try:
            context_view = _context_view(history.events)
        except ValidationError:
            context_view = None
            warnings.append("上下文记录格式无效，请检查原始事件；此处显示为未记录。")
        try:
            lifecycle = _lifecycle_view(history.events)
        except ValidationError:
            lifecycle = []
            warnings.append("生命周期诊断格式无效，请检查原始事件。")
        return RunView(
            automation=history.manifest.automation,
            run_id=identifier,
            prompt=history.manifest.task.prompt,
            workspace=str(history.manifest.task.workspace),
            python_execution=history.manifest.task.python_execution,
            verification_kind=history.manifest.task.verifier.kind if history.manifest.task.verifier else None,
            execution_mode=history.manifest.origin.execution_mode if history.manifest.origin else None,
            source_path=str(history.manifest.origin.registered_path) if history.manifest.origin else None,
            source_kind=history.manifest.origin.kind if history.manifest.origin else None,
            local_changes=self.local_changes.summary(identifier) if history.manifest.origin is None or history.manifest.origin.execution_mode == 'local' else None,
            created_at=history.manifest.created_at,
            result=result,
            lifecycle=lifecycle,
            phase=(("workspace_snapshot" if item.manifest.origin is not None and item.manifest.origin.kind == "workspace" else "project_snapshot") if item.finalizing else item.context.phase) if active and item.context is not None else _phase(history.events),
            event_count=len(history.events),
            last_seq=history.events[-1].seq if history.events else 0,
            active=active,
            cancelling=bool(active and item.cancelling),
            interrupted=(
                not active
                and history.result is None
                and (history.interrupted or bool(self._warnings.get(identifier)))
            ),
            warnings=warnings,
            config=history.manifest.config,
            project=self._project_review(identifier),
            project_workspace=self._project_workspace(identifier),
            context=context_view,
            session_id=history.manifest.session_id or identifier,
            parent_run_id=history.manifest.parent_run_id,
            turn_index=history.manifest.turn_index,
        )

    def list_subagents(self, run_id: UUID) -> dict:
        """只读run_id的委派快照并返回items；父运行已停止时把无终态子项明确标为中断。

        不扫描其他运行或自动续跑；运行不存在沿用get_run的404/503错误语义。
        """
        view = self.get_run(run_id)
        items = [item.model_copy(deep=True) for item in view.result.subagents]
        for item in items:
            if not view.active and item.status == "running":
                item.status, item.stop_reason = "interrupted", "missing_terminal"
        return {"items": [item.model_dump(mode="json") for item in items], "active": view.active}

    def get_subagent(self, run_id: UUID, child_id: UUID, after: int, limit: int, offset: int) -> dict:
        """分页读取run_id直属child_id轨迹及报告；返回快照、事件页、上下文、固定产物及工作树审阅状态，不启动执行。

        after是已读子事件序号，limit为1—100，offset为报告UTF-8字节偏移。
        未登记子项返回404，日志损坏/归属变化返回503，非法报告偏移返回422。
        """
        view = self.list_subagents(run_id)
        record = next((item for item in view["items"] if item["run_id"] == str(child_id)), None)
        if record is None:
            raise WebServiceError(404, "subagent_not_found", "该子执行不属于当前运行。")
        try:
            history = read_child(self.runs_root, UUID(record["log_parent_id"]) if record.get("log_parent_id") else run_id, child_id)
            if record["report"] is None and not view["active"]:
                recovered = history.result.subagent_report if history.result else None
                record["report"] = (recovered or partial_report(history.events, "missing_terminal", role=record["role"])).model_dump(mode="json")
            report = report_page(history, offset, 8192)
            report["status"] = record["status"]  # 当前活动状态取父宿主事实，缺终态的报告不擅自标中断。
            selected = [event for event in history.events if event.seq > after][:limit]
            last = selected[-1].seq if selected else after
            context = _context_view(history.events)
            # 所有附件均来自已固定子结果，绝不回退到当前工作树文件。
            result = history.result
            project = self.projects.get(child_id) if record["role"] == "develop" else None
            return {"subagent": record, "report_page": report,
                    "artifacts": [item.model_dump(mode="json") for item in result.artifacts] if result else [],
                    "artifact_status": result.artifact_status if result else "pending",
                    "artifact_warnings": result.artifact_warnings if result else [],
                    "project": project.model_dump(mode="json") if project else None,
                    "events": {"items": [event.model_dump(mode="json") for event in selected],
                               "has_more": last < len(history.events), "last_seq": len(history.events)},
                    "context": context.model_dump(mode="json") if context else None, "warnings": history.warnings}
        except SubagentError as error:
            raise WebServiceError(422, error.code, error.message) from None
        except (StorageError, ValidationError):
            raise WebServiceError(503, "subagent_unreadable", "子执行记录无法核对，请检查本地日志。") from None

    async def cancel_subagent(self, run_id: UUID, child_id: UUID) -> dict:
        """停止run_id直属child_id并返回真实子快照；历史仅读取，其他父归属返回404。

        仅当前活动父控制器可停止子项；未绑定时返回409，取消不停止兄弟研究或父模型。
        """
        detail = self.get_subagent(run_id, child_id, 0, 1, 0)
        if detail["subagent"]["status"] != "running":
            return {"subagent": detail["subagent"]}
        item = self._active.get(run_id)
        controller = item.context.subagent_controller if item is not None and item.context is not None else None
        if controller is None or item.context.manifest.run_id != run_id:
            raise WebServiceError(409, "subagent_not_active", "子研究已停止或正在收尾，请刷新记录。")
        return await controller.cancel(child_id)

    def list_events(self, run_id: UUID | str, after: int, limit: int) -> EventPage:
        """返回 run_id 中 seq>after 的最多 limit 条有效事件；after 从零开始。

        无记录/损坏清单沿用 get_run 的受控异常；has_more 只表示当前已落盘的后续事件。
        """
        view = self.get_run(run_id)
        try:
            history = self._read_history(view.run_id)
        except StorageError:
            raise WebServiceError(503, "run_unreadable", "运行事件暂时无法读取。") from None
        selected = [event for event in history.events if event.seq > after]
        return EventPage(items=selected[:limit], has_more=len(selected) > limit, last_seq=view.last_seq)

    async def cancel(self, run_id: UUID | str) -> RunView:
        """幂等请求取消指定活动任务；终态/历史任务仅返回视图，不重新执行或追加事件。"""
        view = self.get_run(run_id)
        item = self._active.get(view.run_id)
        if view.active and item is not None and item.task is not None and not item.cancelling and not item.finalizing and (item.context is None or item.context.phase != 'artifact_collection'):
            item.cancellation.api_requested = True
            item.cancelling = True
            item.task.cancel()
        return self.get_run(view.run_id)

    async def wait_idle(self, run_id: UUID | str | None = None) -> None:
        """等待 run_id 指定运行；None 等待当前全部运行快照，shield 防止调用方取消传播。"""
        items = [self._active.get(UUID(str(run_id)))] if run_id is not None else list(self._active.values())
        await asyncio.gather(*(asyncio.shield(item.task) for item in items if item is not None and item.task is not None))

    async def shutdown(self) -> None:
        """停止接纳和出队，取消所有活动 Run 与集成作业，等待准备、子进程及证据收尾。"""
        self._closing = True
        await self.automations.close()
        self.jobs.closed = True
        if self.integrations.active_job:
            self.integrations.cancel(self.integrations.active_job)
        await asyncio.gather(*(asyncio.shield(task) for task in list(self._submissions.values())), return_exceptions=True)
        async with self._lock:
            for item in list(self._active.values()):
                if item.task is not None and not item.task.done() and not item.cancelling and not item.finalizing and (item.context is None or item.context.phase != 'artifact_collection'):
                    item.cancellation.shutdown_requested = True
                    item.cancelling = True
                    item.task.cancel()
        await self.wait_idle()
        await asyncio.gather(*(asyncio.shield(task) for task in list(self._workspace_actions)), return_exceptions=True)
        await asyncio.gather(*(asyncio.shield(task) for task in self._recovery_tasks.values() if not task.done()), return_exceptions=True)
        if self.jobs.worker is not None:
            await asyncio.shield(self.jobs.worker)
        await asyncio.gather(*(asyncio.shield(task) for task in list(self.jobs.executing.values())), return_exceptions=True)

    def artifact_path(self, run_id: UUID | str, location: str, path: str, parent_run_id: UUID | None = None) -> Path:
        """仅解析已登记产物 path；location 为 run/workspace，越界、链接或未登记抛 404。

        每次访问重新检查真实路径，拒绝文件登记后被链接替换；不返回任意工作区文件。
        run/plans/vN.md 是由本轮已保存版本生成的 Markdown 导出，复用网页和原生下载链路。
        """
        child_base = None  # 子日志目录只能从父快照归属解析，客户端不能指定磁盘位置。
        if parent_run_id is not None:
            from types import SimpleNamespace
            detail = self.get_subagent(parent_run_id, UUID(str(run_id)), 0, 1, 0)
            log_parent = UUID(detail["subagent"]["log_parent_id"]) if detail["subagent"].get("log_parent_id") else parent_run_id
            history = read_child(self.runs_root, log_parent, UUID(str(run_id)))
            if history.result is None:
                raise WebServiceError(409, "artifact_pending", "子执行尚未固定产物。")
            view = SimpleNamespace(run_id=UUID(str(run_id)), result=history.result, execution_mode="isolated")
            child_base = self.runs_root / str(log_parent) / "subagents" / str(run_id)
        else:
            view = self.get_run(run_id)
        plan_export = re.fullmatch(r"plans/v([1-9][0-9]{0,2})\.md", path) if location == "run" and child_base is None else None
        if plan_export is not None:
            version = int(plan_export[1])
            plan = next((item for item in view.result.planning.plans if item.version == version), None)
            if plan is None:
                raise WebServiceError(404, "plan_not_found", "所选计划版本不存在。")
            try:
                return materialize_plan_export(self.runs_root / str(view.run_id), plan)
            except StorageError:
                raise WebServiceError(503, "plan_export_unavailable", "计划导出暂不可用，请检查运行记录后重试。") from None
        references = view.result.artifacts
        if not any(item.location == location and item.path == path for item in references):
            raise WebServiceError(404, "artifact_not_found", "产物未登记或不可访问。")
        if location == 'workspace' and view.execution_mode != 'isolated':
            try:
                return self.local_changes.after.saved_file(view.run_id, path)
            except SnapshotError:
                raise WebServiceError(409, 'local_artifact_unavailable', '本轮固定产物尚未保存或已不可核对；请查看原目录，旧轮不会返回当前同名文件。') from None
        if location == 'workspace':
            try:
                return self.projects.files.saved_file(view.run_id, path)
            except SnapshotError:
                raise WebServiceError(409, 'project_artifact_unavailable', '本轮固定产物不可用，不会用共享工作树当前文件冒充历史内容。') from None
        base = child_base or self.runs_root / str(view.run_id)
        try:
            candidate = base / path
            # ArtifactRef已校验词法边界，这里检查登记后新增的每一级链接。
            prefix = base
            if base.is_symlink() or base.is_junction():
                raise ValueError
            for part in Path(path).parts:
                prefix = prefix / part
                if prefix.is_symlink() or prefix.is_junction():
                    raise ValueError
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(base.resolve(strict=True)) or not resolved.is_file():
                raise ValueError
            return resolved
        except (OSError, RuntimeError, ValueError):
            raise WebServiceError(404, "artifact_not_found", "产物未登记或不可访问。") from None


    def _project_review(self, run_id: UUID) -> ProjectReview | None:
        """读取 run_id 的独立项目元数据；坏元数据返回空并附警告，不伪造审阅结论。"""
        try:
            return self.projects.get(run_id)
        except ProjectError:
            self._warnings.setdefault(run_id, ["项目审阅元数据暂时无法读取。"])
            return None

    def _project_workspace(self, run_id: UUID) -> ProjectWorkspace | None:
        """读取 run_id 会话共享工作树当前状态；不重建目录，损坏或非项目轮次返回 None。"""
        if self._project_review(run_id) is None:
            return None
        try:
            return self.projects.workspace(run_id)
        except ProjectError:
            self._warnings.setdefault(run_id, []).append("会话工作树管理状态不可用，历史证据仍按原记录读取。")
            return None

    async def get_review(self, run_id: UUID | str) -> ProjectReview:
        """返回已停止 run_id 的审阅；重启遗留 pending 快照最多补捕获一次，不恢复执行。"""
        view = self.get_run(run_id)
        if view.active:
            raise WebServiceError(409, "run_active", "任务仍在执行或保存代码快照，请等待收尾。")
        review = view.project
        if review is None:
            raise WebServiceError(404, "project_review_not_found", "此运行没有可用的项目审阅记录。")
        if review.snapshot_status == "pending":
            recovery = self._recovery_tasks.get(view.run_id)
            if recovery is None and view.run_id not in self._recovered:
                if self._closing:
                    raise WebServiceError(503, "service_closing", "本地服务正在关闭。")
                self._recovered.add(view.run_id)
                recovery = asyncio.create_task(self._recover_review(view.run_id), name=f"harness-review-{view.run_id}")
                self._recovery_tasks[view.run_id] = recovery

                def observe_recovery(task: asyncio.Task[ProjectReview]) -> None:
                    """请求断开后仍读取共享恢复异常，避免无人取回告警；返回值仍交每个等待请求。"""
                    if not task.cancelled():
                        task.exception()
                recovery.add_done_callback(observe_recovery)
            if recovery is not None:
                # 取消一个页面请求不影响另一个页面等待的同一份恢复结果。
                review = await asyncio.shield(recovery)
        return review

    async def _recover_review(self, run_id: UUID) -> ProjectReview:
        """为停止的 run_id 补捕获一次快照，返回 ready/error 事实；缺元数据时抛503，不恢复模型。"""
        try:
            async with self.workspace_action(run_id):
                return await asyncio.to_thread(self.projects.capture, run_id)
        except ProjectError:
            review = self._project_review(run_id)
            if review is None:
                raise WebServiceError(503, "project_review_unreadable", "项目审阅记录无法读取。") from None
            return review

    async def decide_review(self, run_id: UUID | str, decision: str) -> ProjectReview:
        """将终态 run_id 标记 keep/discard；缺快照、未知进程清理或仍执行时拒绝危险删除。"""
        async with self._lock:
            review = await self.get_review(run_id)
            view = self.get_run(run_id)
            if self.active_for_session(str(view.session_id)) is not None:
                raise WebServiceError(409, "run_active", "会话正在执行或保存证据，不能处理共享工作树。")
            if any(job.get("kind") == "integration" and job.get("session_id") == str(view.session_id) and job["status"] in {"queued", "preparing", "running", "needs_review"} for job in self.jobs.list()):
                raise WebServiceError(409, "integration_busy", "成果集成仍在执行或待核对，请先处理后台作业。")
            if review.snapshot_status != "ready":
                raise WebServiceError(409, "project_snapshot_unavailable", "代码快照尚未准备完成，不能处理工作树。")
            if decision == "discard" and (view.interrupted or view.result.cleanup_status not in {"not_needed", "complete"}):
                raise WebServiceError(409, "project_cleanup_unconfirmed", "无法确认任务进程已清理，请保留工作树并先处理本地进程。")
            try:
                async with self.workspace_action(view.run_id):
                    operation = asyncio.create_task(asyncio.to_thread(self.projects.decide, view.run_id, decision))
                    try:
                        return await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        await asyncio.shield(operation)
                        raise
            except ProjectError as error:
                raise WebServiceError(409, error.code, error.message) from None

    async def project_patch(self, run_id: UUID | str, parent_run_id: UUID | None = None) -> Path:
        """返回终态 run_id 已固化的补丁文件；活动或快照失败时抛受控错误，不重新生成 diff。"""
        if parent_run_id is not None:
            parent = self.get_run(parent_run_id)
            if not any(item.run_id == UUID(str(run_id)) for item in parent.result.subagents):
                self.integrations.team_review(str(run_id), parent)
                return await asyncio.to_thread(self.projects.patch_path, UUID(str(run_id)))
            detail = self.get_subagent(parent_run_id, UUID(str(run_id)), 0, 1, 0)
            if detail["subagent"]["role"] != "develop" or detail["subagent"]["snapshot_status"] != "ready":
                raise WebServiceError(409, "project_snapshot_required", "开发子成果尚无完整补丁。")
            return await asyncio.to_thread(self.projects.patch_path, UUID(str(run_id)))
        view = self.get_run(run_id)
        if view.execution_mode == 'local':
            if view.active:
                raise WebServiceError(409, 'run_busy', '任务仍在执行或保存差异，请等待完成。')
            try:
                return await asyncio.to_thread(self.local_changes.patch_path, view.run_id)
            except SnapshotError as error:
                raise WebServiceError(409, error.code, error.message) from None
        review = await self.get_review(run_id)
        if review.snapshot_status != "ready" or not review.patch_available:
            raise WebServiceError(409, "project_snapshot_unavailable", "代码快照尚不可下载，请检查审阅状态。")
        try:
            return await asyncio.to_thread(self.projects.patch_path, UUID(str(run_id)))
        except ProjectError as error:
            raise WebServiceError(409, error.code, error.message) from None
