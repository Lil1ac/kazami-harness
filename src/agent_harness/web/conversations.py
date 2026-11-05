"""串行会话的读取、追加准备与幂等调度；每次执行仍交给已有 WebRunManager/Runner。"""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import ValidationError

from agent_harness.config import ConfigurationError, load_config, prepare_task
from agent_harness.projects import ProjectError
from agent_harness.runtime.runner import validate_auto_repair, validate_execution
from agent_harness.execution.python import PythonExecutionError, prepare_python_execution
from agent_harness.schemas import HistorySource, RunManifest, RunOrigin, VerifierSpec, ExecutionResume
from agent_harness.runtime.control import ControlStore
from agent_harness.storage.sessions import SessionError, SessionStore, history_messages, payload_digest
from agent_harness.storage.snapshots import SnapshotError
from agent_harness.storage.runs import StorageError
from agent_harness.work.planning import PlanningError, continue_planning, planning_from_history
from agent_harness.web.schemas import ConversationTurn, ConversationView, FollowupRequest
from agent_harness.permissions.models import PermissionResume
from agent_harness.permissions.store import PermissionStore
from agent_harness.work.tasks import TaskError

if TYPE_CHECKING:
    from agent_harness.web.service import WebRunManager
    from agent_harness.storage.runs import RunHistory


class ConversationService:
    """在现有单槽和生命周期内组织会话；不改写旧轮事件、测试或补丁。"""

    def __init__(self, manager: 'WebRunManager') -> None:
        """绑定 manager 及其受保护存储；构造不创建目录、复制文件或调用模型。"""
        self.manager = manager  # 唯一执行调度器，负责后台任务、取消和收尾。
        self.store = SessionStore(manager.runs_root)  # 会话线性关系与请求幂等。
        self.submission_digest: str | None = None  # 内存中尚未落盘请求的载荷摘要，用于并发幂等。
        self.submission_key: tuple[UUID, UUID] | None = None  # 正在准备的会话/请求，供同进程重复请求共享等待。

    def _origin(self, history: 'RunHistory') -> RunOrigin:
        """核对history的固定宿主来源并返回；缺失或登记变化时抛SessionError，无副作用。"""
        origin = history.manifest.origin
        if origin is None:
            raise SessionError('continuation_source_missing', '运行记录缺少明确的工作区来源。')
        presets = self.manager.catalog.project_presets() if origin.kind == 'project' else self.manager.catalog.workspace_presets()
        preset = next((item for item in presets if item.id == origin.preset_id), None)
        if preset is None or preset.path.resolve() != origin.registered_path.resolve():
            raise SessionError('continuation_source_changed', '宿主工作区登记与前轮来源不一致，请核对配置。')
        return origin

    def _ready(self, history: 'RunHistory') -> RunOrigin:
        """核对可信终态或完整中断前缀并返回来源；当前代码内容在提交准备时核验。"""
        interrupted = history.result is None and history.interrupted
        if history.warnings or (history.result is None and not interrupted):
            raise SessionError('continuation_unverified', '前轮事件无法完整核对，不能直接继续。')
        if not interrupted and history.result.cleanup_status not in ('not_needed', 'complete'):
            raise SessionError('continuation_cleanup', '前轮进程清理状态无法确认，不能继续。')
        history_messages(history, allow_interrupted=interrupted)
        origin = self._origin(history)
        if origin.execution_mode == 'local':
            try:
                self.manager.local_changes.verify(origin, history.manifest.task.workspace)
            except SnapshotError as error:
                raise SessionError(error.code, error.message) from None
            return origin
        if origin.kind == 'project':
            review = self.manager.projects.get(history.manifest.run_id)
            if review is None or (not interrupted and (review.snapshot_status != 'ready' or not review.patch_available)):
                raise SessionError('project_snapshot_required', '前轮代码快照尚未完整可用，不能继续。')
            if review.status == 'discarded':
                raise SessionError('project_already_decided', '最新一轮工作树已经丢弃，不能继续修改。')
        return origin

    def view(self, run_id: UUID) -> ConversationView:
        """只读加载 run_id 所属线性会话与可继续条件，不访问模型或重新捕获代码。"""
        current = self.manager.get_run(run_id)
        session_id = current.session_id or run_id
        record = self.store.read(session_id)
        if run_id not in record.run_ids:
            raise SessionError('session_unreadable', '本轮与会话关联不一致，请检查运行记录。')
        turns: list[ConversationTurn] = []
        latest_history = None
        source = None  # 根运行缺失时不从后续工作树猜测项目来源。
        blocked = None
        for index, identifier in enumerate(record.run_ids):
            expected_parent = record.run_ids[index - 1] if index else None
            ordinal = index + 1
            intent = next((item for item in record.requests.values() if item.run_id == identifier), None)
            try:
                history = self.manager._read_history(identifier)
                manifest = history.manifest
                if index == 0:
                    source = self.manager.conversation_index.source_for(history)
                if (
                    manifest.parent_run_id != expected_parent
                    or (manifest.session_id or identifier) != session_id
                    or manifest.turn_index != ordinal
                ):
                    raise SessionError('session_unreadable', '会话运行顺序与用户轮次不一致。')
                view = self.manager.get_run(identifier)
                turn = ConversationTurn(
                    run_id=identifier, parent_run_id=expected_parent, turn_index=ordinal,
                    prompt=view.prompt, final_answer=view.result.final_answer,
                    execution_status=view.result.execution_status,
                    stop_reason=view.result.stop_reason,
                    validation_status=view.result.validation_status,
                    created_at=view.created_at, interrupted=view.interrupted,
                )
                if identifier == record.run_ids[-1]:
                    latest_history = history
            except StorageError:
                if intent is not None and intent.parent_run_id != expected_parent:
                    raise SessionError('session_unreadable', '准备遗留请求与会话执行顺序不一致。') from None
                turn = ConversationTurn(
                    run_id=identifier, parent_run_id=expected_parent, turn_index=ordinal,
                    prompt=intent.prompt if intent else '运行记录不可用', interrupted=True,
                )
                blocked = SessionError('continuation_unverified', '已登记的后续运行尚无可信记录，请核对准备状态；不会自动重放。')
            turns.append(turn)
        busy = self.manager.busy_run_id
        preparing = self.manager._submission is not None and not self.manager._submission.done()
        if busy == record.run_ids[-1] or (preparing and busy == record.run_ids[-1]):
            blocked = SessionError('run_busy', '已有任务正在准备、运行或保存快照，请等待完成或取消。')
        elif any(item.state == 'preparing' for item in record.requests.values()):
            blocked = SessionError('request_uncertain', '上次追加准备尚未确认，请核对本地状态；不会自动重新执行。')
        elif blocked is None and latest_history is not None:
            try:
                self._ready(latest_history)
            except (SessionError, SnapshotError, ProjectError) as error:
                blocked = SessionError(error.code, error.message)
        management = self.manager.management.view(str(session_id))
        if management.state != "active":
            blocked = SessionError("conversation_inactive", "请先恢复会话，再继续发送或处理计划。")
        return ConversationView(title=management.title, management=management, source=source, session_id=session_id, latest_run_id=record.run_ids[-1], turns=turns, can_continue=blocked is None and latest_history is not None, blocked_code=blocked.code if blocked else None, blocked_reason=blocked.message if blocked else None, busy_run_id=busy, continuation_notice='前轮中断。可直接发送“继续”，继承已保存记录和当前代码；结果不明的工具需要先核对，本轮重新测试。' if blocked is None and latest_history is not None and latest_history.interrupted else None)

    async def submit(self, parent_id: UUID, request: FollowupRequest):
        """幂等接纳追加 request；HTTP断开不取消准备，同进程重复请求共享原任务等待。"""
        parent = self.manager._read_history(parent_id)
        session_id = parent.manifest.session_id or parent_id
        self.manager.management.require_active(str(session_id))
        digest = payload_digest({'parent_run_id': str(parent_id), **request.model_dump(mode='json', exclude={'request_id'})})
        if self.submission_key == (session_id, request.request_id) and self.manager._submission is not None and not self.manager._submission.done():
            if self.submission_digest != digest:
                raise SessionError('request_conflict', '该请求编号已用于不同内容。')
            return await asyncio.shield(self.manager._submission)
        intent = self.store.lookup(session_id, request.request_id, digest)
        if intent is not None:
            if intent.state == 'committed':
                try:
                    return self.manager.get_run(intent.run_id)
                except Exception:
                    raise SessionError('request_uncertain', '原请求已接纳但记录尚未完整建立，请核对；不会重复执行。') from None
            if intent.state == 'failed':
                raise SessionError(intent.error_code or 'request_failed', intent.error_message or '原请求准备失败，请核对后重新提交。')
            if self.submission_key == (session_id, request.request_id) and self.manager._submission is not None:
                return await asyncio.shield(self.manager._submission)
            raise SessionError('request_uncertain', '原追加请求准备状态尚未确认，不会自动重放。')
        if self.manager._closing:
            raise SessionError('service_closing', '本地服务正在关闭。')
        if self.manager.busy_run_id is not None or (self.manager._submission is not None and not self.manager._submission.done()):
            raise SessionError('run_busy', '已有任务正在准备或运行，请等待完成或取消。')
        task = asyncio.create_task(self._start(parent_id, session_id, request, digest), name='harness-followup-prepare')
        self.manager._submission = task
        self.submission_key = (session_id, request.request_id)
        self.submission_digest = digest

        def observe(task: asyncio.Task) -> None:
            """取回准备异常供断开连接的请求收尾；正常返回值仍由 shield 原样交付。"""
            if not task.cancelled():
                task.exception()
        task.add_done_callback(observe)
        return await asyncio.shield(task)

    async def _start(self, parent_id: UUID, session_id: UUID, request: FollowupRequest, digest: str):
        """持有全局单槽核验父状态、复用会话目录并启动 Runner；失败不修改父证据。"""
        async with self.manager._lock:
            if self.manager._closing:
                raise SessionError('service_closing', '本地服务正在关闭。')
            if self.manager.busy_run_id is not None:
                raise SessionError('run_busy', '已有任务正在运行。')
            parent = self.manager._read_history(parent_id)
            if self.store.read(session_id).run_ids[-1] != parent_id:
                raise SessionError('session_stale_parent', '只能从会话最新一轮继续，请先打开最新运行。')
            origin = self._ready(parent)
            resume = None
            execution_resume = None
            if request.resume or (parent.result is not None and parent.result.stop_reason == "user_paused"):
                control = ControlStore(self.manager.runs_root).get(str(parent_id))
                if not request.resume or control is None or control["status"] != "paused" or parent.result is None or parent.result.stop_reason != "user_paused":
                    raise SessionError("execution_resume_required", "请使用恢复暂停入口接续，原预算不会重置。")
                checkpoint = dict(control["checkpoint"])
                checkpoint["source_run_id"] = UUID(checkpoint["source_run_id"])
                execution_resume = ExecutionResume.model_validate(checkpoint)
                info = parent.manifest.task.workspace.stat()
                if execution_resume.workspace_identity != [info.st_dev, info.st_ino]:
                    raise SessionError("workspace_changed", "暂停后的目录身份已变化，不能恢复。")
            pending = parent.result.permissions.pending if request.action_decision is not None and parent.result is not None else PermissionStore(self.manager.runs_root).waiting(parent)
            if request.action_decision is not None:
                if pending is None or pending["id"] != request.action_decision.action_id:
                    raise SessionError("permission_stale", "当前轮没有匹配的动作审批。")
                try:
                    PermissionStore(self.manager.runs_root).validate(request.action_decision, str(parent_id), request_id=str(request.request_id))
                except TaskError as error:
                    raise SessionError(error.code, error.message) from None
                previous = parent.result.permissions
                resume = PermissionResume(action_id=pending["id"], decision=request.action_decision.action,
                    spent_model_turns=previous.spent_model_turns + parent.result.model_turns,
                    spent_tool_calls=previous.spent_tool_calls + parent.result.tool_calls,
                    spent_seconds=previous.spent_seconds + parent.result.active_duration_seconds,
                    spent_usage={key: previous.spent_usage.get(key, 0) + getattr(parent.result.usage, key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                    spent_usage_complete=previous.spent_usage_complete and parent.result.usage_complete,
                    repair_attempts=parent.result.repair_attempts,
                    delegation_used=bool(parent.result.subagents) or bool(parent.manifest.permission_resume and parent.manifest.permission_resume.delegation_used),
                    child_parent_id=pending.get("child_parent_id"), child_run_id=pending.get("child_run_id"))
            elif pending is not None:
                raise SessionError("permission_pending", "请先处理当前动作审批，普通消息不会批准动作。")
            try:
                planning = continue_planning(planning_from_history(parent), request.decision, request.prompt.strip(),
                                             parent.manifest.task.workspace, request.mode)
            except PlanningError as error:
                raise SessionError(error.code, error.message) from None
            interrupted = parent.result is None and parent.interrupted
            inherited = history_messages(parent, allow_interrupted=interrupted)
            merged = parent.manifest.config.model_dump(mode='python')
            # 宿主系统规则重新生成；网页可编辑部分默认沿用前轮，覆盖仍严格校验。
            merged['system_prompt'] = self.manager.run_config.system_prompt
            merged['planning_policy'] = self.manager.run_config.planning_policy  # 权限策略始终取当前宿主，旧轮配置不能绕过强制规划。
            merged['permission_policy'] = self.manager.run_config.permission_policy.model_dump()  # 宿主收紧立即应用于接续，不能从旧轮继承放宽。
            merged['skill_roots'] = list(self.manager.run_config.skill_roots)  # 技能来源使用当前宿主登记，选择默认沿用前轮。
            patch = request.config.model_dump(exclude_none=True, exclude_unset=True)
            for section in ('limits', 'context', 'memory'):
                if section in patch:
                    merged[section] = {**merged[section], **patch.pop(section)}
            merged.update(patch)
            if self.manager._queued_memory_policy is not None:
                merged["memory"] = self.manager._queued_memory_policy
            try:
                config = load_config(overrides=merged)
                if (resume is not None or execution_resume is not None) and config.limits != parent.manifest.config.limits:
                    raise SessionError("permission_stale", "动作审批接续不能修改原预算，请先处理等待再提交新要求。")
                if config.planning_policy == "required" and not (planning.plans and planning.plans[-1].status == "approved"):
                    planning.phase, planning.reason = "planning", "宿主要求先批准计划"
                prompt = request.prompt.strip()
                if not prompt:
                    raise SessionError('invalid_prompt', '追加要求不能为空白。')
                identifier = self.manager._queued_run_id or uuid4()
                task = parent.manifest.task.model_copy(update={'prompt': prompt})
                manifest = RunManifest(run_id=identifier, session_id=session_id, parent_run_id=parent_id, turn_index=parent.manifest.turn_index + 1, task=task, config=config, origin=origin, planning=planning, human_decision=request.decision, permission_resume=resume, execution_resume=execution_resume, history_source=HistorySource(last_seq=parent.events[-1].seq, messages_sha256=payload_digest(inherited), interrupted=interrupted))
                import os
                api_key = self.manager.api_key(config)
                validate_execution(manifest, api_key)
            except (ConfigurationError, ValidationError) as error:
                raise SessionError('invalid_run', str(error) if isinstance(error, ConfigurationError) else '追加任务配置无效。') from None
            self.store.begin(session_id, request.request_id, parent_id, identifier, digest, prompt)
            try:
                sources = self.manager.catalog.project_presets() if origin.kind == 'project' else self.manager.catalog.workspace_presets()
                current_source = next(item for item in sources if item.id == origin.preset_id)
                if origin.execution_mode == 'local':
                    root = self.manager.local_changes.verify(origin, parent.manifest.task.workspace)
                    if origin.kind == 'project':
                        preset = next(item for item in self.manager.catalog.project_presets() if item.id == origin.preset_id)
                        await asyncio.to_thread(self.manager.projects.inspect, preset)
                        verifier = self.manager.project_verifier(preset, root)
                    else:
                        verifier_path = None
                        if origin.verifier_id is not None:
                            selected = next((item for item in self.manager.web_config.verifiers if item.id == origin.verifier_id), None)
                            if selected is None:
                                raise SessionError('continuation_verifier_missing', '前轮验收预设已移除，请核对宿主配置。')
                            verifier_path = selected.path
                        verifier = prepare_task(prompt, root, verifier_path, config.limits.verifier_timeout_seconds, python_paths=current_source.python_paths, python_executable=current_source.python_executable).verifier
                else:  # 来源模型保证隔离执行只能属于 Git 项目。
                    preset = next(item for item in self.manager.catalog.project_presets() if item.id == origin.preset_id)
                    def prepare_project():
                        """复用会话工作树；中断源仅在明确提交时固定证据，不重放旧补丁。"""
                        if interrupted:
                            self.manager.projects.capture(parent_id)
                        return self.manager.projects.prepare_turn(preset, parent_id, identifier)
                    preparation = asyncio.create_task(asyncio.to_thread(prepare_project))
                    try:
                        review = await asyncio.shield(preparation)
                    except asyncio.CancelledError:
                        await asyncio.shield(preparation)
                        raise
                    root = Path(review.worktree)
                    verifier = self.manager.project_verifier(preset, root)
                execution = prepare_python_execution(root, current_source.python_paths, python_executable=current_source.python_executable)
                task = task.model_copy(update={'workspace': root, 'verifier': verifier, 'python_execution': execution})
                manifest = manifest.model_copy(update={'task': task})
                validate_auto_repair(manifest)
                self.store.commit(session_id, request.request_id)
                return await self.manager._launch(manifest, api_key)
            except (SessionError, SnapshotError, ProjectError, ConfigurationError, StorageError, ValidationError, PythonExecutionError, OSError) as error:
                code = error.code if isinstance(error, (SessionError, SnapshotError, ProjectError)) else 'followup_prepare_failed'
                message = error.message if isinstance(error, (SessionError, SnapshotError, ProjectError)) else str(error) if isinstance(error, (ConfigurationError, PythonExecutionError)) else '后续运行准备失败，已有父结果保持不变。'
                self.store.fail(session_id, request.request_id, code, message, rollback_unstarted=True)
                if not (self.manager.runs_root / str(identifier) / 'run.json').exists() and self.manager.projects.get(identifier) is not None:
                    try:
                        await asyncio.to_thread(self.manager.projects.abandon_turn, identifier)
                    except (ProjectError, OSError):
                        message += ' 新建工作树保留供核对。'
                raise SessionError(code, message) from None
