"""本地 Web 任务管理：单槽调度真实 Runner，读取事实记录，不另造执行状态。"""

import asyncio
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

from ..config import ConfigurationError, load_config, prepare_task
from ..providers.base import ModelClient
from ..runner import CancellationState, RunContext, run_task, validate_auto_repair, validate_execution
from ..execution import PythonExecutionError, prepare_python_execution, python_command
from ..schemas import ContextAction, ContextCheck, ContextView, PlanningState, RunConfig, RunEvent, RunManifest, RunOrigin, RunResult, VerificationAttempt, VerifierSpec
from ..projects import ProjectError, ProjectManager, ProjectPreset, ProjectReview, ProjectWorkspace
from ..store import RunHistory, RunStore, StorageError
from ..skills import SkillCatalog
from ..local_execution import LocalChangeStore
from ..snapshots import SnapshotError
from ..sessions import SessionError
from ..planning import planning_from_history
from ..plan_exports import materialize_plan_export
from .config import WebConfig
from .conversations import ConversationService
from .conversation_index import ConversationIndex
from .project_catalog import ProjectCatalog
from .folder_picker import FolderPicker
from .schemas import CreateRunRequest, EventPage, RunView


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
    """唯一活动任务的服务侧控制句柄；结果事实仍归 Runner 与 RunStore 所有。"""

    manifest: RunManifest  # 后台任务完整输入；不包含密钥值。
    cancellation: CancellationState  # 取消来源，在调用 task.cancel 前设置。
    started: asyncio.Future[None]  # on_context 到达后完成，防止任务启动前被取消。
    task: asyncio.Task[None] | None = None  # 后台协程；创建后立即绑定。
    context: RunContext | None = None  # Runner 回调交出的实时进度，启动前为空。
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
    }
    current = "created"
    for event in events:
        current = "context_compaction" if event.type == "model_request_started" and event.payload.get("purpose") == "summary" else phases.get(event.type, current)
        if event.type == "run_finished":
            reason = event.payload.get("result", {}).get("stop_reason")
            if reason in {"planning_complete", "plan_rejected", "user_question", "plan_approval", "interaction_cancelled"}:
                current = reason
    return current


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
        elif event.type == "context_action" and view is not None:
            view.actions.append(ContextAction.model_validate(event.payload))
            view.actions = view.actions[-50:]
            view.action_count += 1
        elif event.type == "model_request_started" and view is not None and event.payload.get("purpose") == "summary":
            view.summary_calls += 1
    return view


def _unfinished_result(history: RunHistory) -> RunResult:
    """按history有效事件还原本次已开始计数和已知token；缺终态一律标用量不完整。

    本函数不补造终态、回答或测试结论；主动耗时仅采用最后落盘elapsed的下界。
    """
    result = RunResult(
        run_id=history.manifest.run_id, execution_status="running" if history.interrupted else "created",
        model_turns=sum(event.type == "model_request_started" for event in history.events),
        http_attempts=sum(event.type == "model_http_attempt" for event in history.events),
        tool_calls=sum(event.type == "tool_started" for event in history.events),
        usage_complete=False, active_duration_seconds=history.events[-1].elapsed_ms / 1000 if history.events else 0,
        planning=planning_from_history(history),
    )
    # 未封存终态不等于每个旧进程都失联；采用已配对结果的清理事实，只有在途动作未知。
    pending_commands: set[str] = set()  # 已开始但尚无结果的命令编号。
    verifier_pending = False  # 宿主验收已启动而未得到清理结论。
    cleanup_rank = {"not_needed": 0, "complete": 1, "unknown": 2, "incomplete": 3}
    for event in history.events:
        if event.type == "tool_started" and event.payload.get("name") == "run_command":
            pending_commands.add(str(event.payload.get("call_id")))
        elif event.type == "tool_result" and event.payload.get("name") == "run_command":
            pending_commands.discard(str(event.payload.get("call_id")))
            observation = event.payload.get("result")
            data = observation.get("data") if isinstance(observation, dict) else None
            status = data.get("cleanup_status", "unknown") if isinstance(data, dict) else "unknown"
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
    if (pending_commands or verifier_pending) and cleanup_rank["unknown"] > cleanup_rank[result.cleanup_status]:
        result.cleanup_status = "unknown"
    return result


class WebRunManager:
    """管理本地单个执行槽、受限提交、取消和只读历史，不因页面断开而取消。"""

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
        self.catalog = ProjectCatalog(self.runs_root, web_config)  # 明确打开目录的持久登记，首轮与续轮共用。
        self.folder_picker = FolderPicker()  # 独立原生选择器，读取页面不创建窗口。
        self._model_client = model_client  # 离线注入；默认无客户端，共用 Runner 创建逻辑。
        self._transport = transport  # 可选 HTTP 测试传输，不在配置或响应序列化。
        self._key_provider = key_provider  # 桌面凭据来自 Windows 凭据库，不进入运行配置。
        self._lock = asyncio.Lock()  # 提交和关停争用同一槽，避免同时创建两个任务。
        self._submission: asyncio.Task[RunView] | None = None  # 服务持有准备任务；页面断开不遗失 Git 归属。
        self._active: ActiveRun | None = None  # 只有仍执行的任务持有槽位。
        self._warnings: dict[UUID, list[str]] = {}  # 服务异常安全提示，不写伪造 run_finished。
        self._history_cache: dict[UUID, tuple[tuple[object, ...], RunHistory, int]] = {}  # 最近8条/32MiB记录；活动任务可单独保留。
        self.projects = ProjectManager(self.runs_root, web_config.worktrees_root)  # 独立 Git 归属与审阅事实。
        self.local_changes = LocalChangeStore(self.runs_root, (self.projects.worktrees_root,))  # 原目录仅保存差异，不派生执行目录。
        self._recovery_tasks: dict[UUID, asyncio.Task[ProjectReview]] = {}  # 并发页面共享一次恢复，全部等待同一完成结果。
        self._recovered: set[UUID] = set()  # 每个中断任务最多恢复捕获一次，避免 GET 重跑 Git。
        self._closing = False  # 关停后永久拒绝新提交。
        self.conversation_index = ConversationIndex(self)  # 项目下按会话聚合的只读导航。
        self.conversations = ConversationService(self)  # 会话读取、幂等追加和文件快照复用本调度器。

    @property
    def busy_run_id(self) -> UUID | None:
        """返回仍执行的运行编号；没有任务或任务已收尾时返回 None，无副作用。"""
        item = self._active
        return item.manifest.run_id if item is not None and item.task is not None and not item.task.done() else None

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
            "skills": SkillCatalog(self.run_config.skill_roots).discover(),
            "key_configured": bool(self.api_key(self.run_config)),
            "verifiers": [
                {"id": item.id, "label": item.label, "description": item.description}
                for item in self.web_config.verifiers
            ],
            "busy_run_id": self.busy_run_id,
        }

    def api_key(self, config: RunConfig) -> str:
        """按 config 取得宿主凭据并返回；默认从环境读取，桌面注入凭据库，不记录值。"""
        return self._key_provider(config) if self._key_provider is not None else os.environ.get(config.api_key_env, "")

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

    async def submit(self, request: CreateRunRequest) -> RunView:
        """服务持有 request 的准备协程并 shield 等待；请求断开不取消创建，忙时立即返回409。"""
        if self._closing:
            raise WebServiceError(503, "service_closing", "本地服务正在关闭。")
        if self.busy_run_id is not None or (self._submission is not None and not self._submission.done()):
            raise WebServiceError(409, "run_busy", "已有任务正在准备或运行，请等待完成或取消。")
        self.conversations.submission_key = None
        preparation = asyncio.create_task(self._start_request(request), name="harness-project-prepare")
        self._submission = preparation

        def observe_completion(task: asyncio.Task[RunView]) -> None:
            """读取后台准备异常以避免请求断开后的未取回告警；正常响应仍由 await 原样返回。"""
            if not task.cancelled():
                task.exception()
        preparation.add_done_callback(observe_completion)
        return await asyncio.shield(preparation)

    async def _start_request(self, request: CreateRunRequest) -> RunView:
        """预检 request 后占单槽启动 Runner；项目工作树和快照收尾均属于同一槽。

        密钥、任务与产物路径在 Git 创建前检查；202 仅在 Runner 落盘建立上下文后返回。
        Git 同步操作在线程执行，调用方取消也等待准备结束，防止工作树无人登记。
        """
        async with self._lock:
            if self._closing:
                raise WebServiceError(503, "service_closing", "本地服务正在关闭。")
            if self.busy_run_id is not None:
                raise WebServiceError(409, "run_busy", "已有任务正在运行，请等待完成或取消。")
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
            identifier = uuid4()
            prepared: ProjectReview | None = None  # 已创建但尚未交给 Runner，预检失败时可安全清理。
            try:
                merged = self.run_config.model_dump(mode="python")
                patch = request.config.model_dump(exclude_none=True)
                for section in ("limits", "context"):
                    if section in patch:
                        merged[section] = {**merged[section], **patch.pop(section)}
                merged.update(patch)
                config = load_config(overrides=merged)
                source = preset.path if preset is not None else workspace.path
                task = prepare_task(
                    request.prompt, source, verifier_path, config.limits.verifier_timeout_seconds,
                    python_paths=(preset or workspace).python_paths,
                    python_executable=(preset or workspace).python_executable,
                )
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
            except (ConfigurationError, ProjectError, SnapshotError, ValidationError, PythonExecutionError) as error:
                if prepared is not None:
                    try:
                        await asyncio.to_thread(self.projects.capture, identifier)
                        await asyncio.to_thread(self.projects.decide, identifier, "discard")
                    except ProjectError:
                        raise WebServiceError(503, "project_prepare_cleanup_failed", "任务尚未启动，项目工作树未能清理；请检查本地项目审阅记录。") from None
                if isinstance(error, (ProjectError, SnapshotError)):
                    raise WebServiceError(400, error.code, error.message) from None
                message = str(error) if isinstance(error, (ConfigurationError, PythonExecutionError)) else "项目测试工作目录或导入路径无效。"
                raise WebServiceError(400, "invalid_run", message) from None
            return await self._launch(manifest, api_key)

    @staticmethod
    def project_verifier(preset: ProjectPreset, root: Path) -> VerifierSpec | None:
        """按preset当前测试设置为实际root构建验收；空命令返回None，不启动进程。"""
        return VerifierSpec(
            kind="project_tests",
            argv=python_command([part.replace("{workspace}", str(root)) for part in preset.test_argv], prepare_python_execution(root, preset.python_paths, python_executable=preset.python_executable)),
            cwd=root, timeout_seconds=preset.test_timeout_seconds,
        ) if preset.test_argv else None

    async def _launch(self, manifest: RunManifest, api_key: str) -> RunView:
        """在已持有单槽锁时启动 manifest 并等待记录建立，返回初始视图；不另建执行循环。

        api_key 只传 Runner；本方法由首次提交和追加共同使用。启动记录失败时返回受控错误，
        会话中已提交的请求不会因重试再次执行。
        """
        item = ActiveRun(manifest, CancellationState(), asyncio.get_running_loop().create_future())
        self._active = item
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
            if self._active is item:
                self._active = None
            # 超预算快照只为活动订阅保留；任务结束后立即释放这个特例。
            cached = self._history_cache.get(item.manifest.run_id)
            if cached is not None and cached[2] > 32 * 1024 * 1024:
                self._history_cache.pop(item.manifest.run_id)

    def _read_history(self, identifier: UUID) -> RunHistory:
        """按三份记录文件的签名缓存 identifier 历史；返回可信快照，读取失败仍抛 StorageError。

        每次检查文件设备、标识、纳秒修改时间和字节数，追加事件/终态/摘要替换会使缓存失效。
        缓存最近8个运行且记录合计不超过32MiB；唯一活动任务可超过此字节预算独占缓存。
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
        active_id = self.busy_run_id
        if record_bytes > byte_budget and identifier != active_id:
            return history
        self._history_cache[identifier] = (key, history, record_bytes)
        while (
            len(self._history_cache) > 8
            or sum(entry[2] for entry in self._history_cache.values()) > byte_budget
        ):
            removable = next((key for key in self._history_cache if key != active_id), None)
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
        item = self._active
        active = item is not None and item.manifest.run_id == identifier and self.busy_run_id == identifier
        if history.result is not None:
            result = history.result.model_copy(deep=True)
        elif active and item.context is not None:
            result = item.context.result.model_copy(deep=True)
        else:
            result = _unfinished_result(history)
        warnings = [*history.warnings, *self._warnings.get(identifier, [])]
        try:
            context_view = _context_view(history.events)
        except ValidationError:
            context_view = None
            warnings.append("上下文记录格式无效，请检查原始事件；此处显示为未记录。")
        return RunView(
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
        item = self._active
        if view.active and item is not None and item.task is not None and not item.cancelling and not item.finalizing and (item.context is None or item.context.phase != 'artifact_collection'):
            item.cancellation.api_requested = True
            item.cancelling = True
            item.task.cancel()
        return self.get_run(view.run_id)

    async def wait_idle(self) -> None:
        """等待当前后台任务完成并返回 None；shield 防调用方断开将取消传播到任务。"""
        item = self._active
        if item is not None and item.task is not None:
            await asyncio.shield(item.task)

    async def shutdown(self) -> None:
        """停止接纳新任务，最多取消一次活动任务并等待 Runner 清理落盘；返回 None。"""
        self._closing = True
        preparation = self._submission
        if preparation is not None and not preparation.done():
            try:
                await asyncio.shield(preparation)
            except (WebServiceError, SessionError):
                pass
        async with self._lock:
            item = self._active
            if item is not None and item.task is not None and not item.task.done() and not item.cancelling and not item.finalizing and (item.context is None or item.context.phase != 'artifact_collection'):
                item.cancellation.shutdown_requested = True
                item.cancelling = True
                item.task.cancel()
        if item is not None and item.task is not None:
            await asyncio.shield(item.task)
        recoveries = [task for task in self._recovery_tasks.values() if not task.done()]
        if recoveries:
            await asyncio.gather(*(asyncio.shield(task) for task in recoveries), return_exceptions=True)

    def artifact_path(self, run_id: UUID | str, location: str, path: str) -> Path:
        """仅解析已登记产物 path；location 为 run/workspace，越界、链接或未登记抛 404。

        每次访问重新检查真实路径，拒绝文件登记后被链接替换；不返回任意工作区文件。
        run/plans/vN.md 是由本轮已保存版本生成的 Markdown 导出，复用网页和原生下载链路。
        """
        view = self.get_run(run_id)
        plan_export = re.fullmatch(r"plans/v([1-9][0-9]{0,2})\.md", path) if location == "run" else None
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
        base = self.runs_root / str(view.run_id)
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
            if self._active is not None and self._active.manifest.session_id == view.session_id:
                raise WebServiceError(409, "run_active", "会话正在执行或保存证据，不能处理共享工作树。")
            if review.snapshot_status != "ready":
                raise WebServiceError(409, "project_snapshot_unavailable", "代码快照尚未准备完成，不能处理工作树。")
            if decision == "discard" and (view.interrupted or view.result.cleanup_status not in {"not_needed", "complete"}):
                raise WebServiceError(409, "project_cleanup_unconfirmed", "无法确认任务进程已清理，请保留工作树并先处理本地进程。")
            try:
                return await asyncio.to_thread(self.projects.decide, view.run_id, decision)
            except ProjectError as error:
                raise WebServiceError(409, error.code, error.message) from None

    async def project_patch(self, run_id: UUID | str) -> Path:
        """返回终态 run_id 已固化的补丁文件；活动或快照失败时抛受控错误，不重新生成 diff。"""
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
