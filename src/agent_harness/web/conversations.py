"""串行会话的读取、追加准备与幂等调度；每次执行仍交给已有 WebRunManager/Runner。"""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import ValidationError

from ..config import ConfigurationError, load_config, prepare_task
from ..projects import ProjectError
from ..runner import validate_execution, validate_output_paths
from ..schemas import HistorySource, RunManifest, RunOrigin, VerifierSpec
from ..sessions import SessionError, SessionStore, history_messages, payload_digest
from ..snapshots import FileSnapshotStore, SnapshotError
from ..store import StorageError
from .schemas import ConversationTurn, ConversationView, FollowupRequest

if TYPE_CHECKING:
    from .service import WebRunManager
    from ..store import RunHistory


class ConversationService:
    """在现有单槽和生命周期内组织会话；不改写旧轮事件、测试或补丁。"""

    def __init__(self, manager: 'WebRunManager') -> None:
        """绑定 manager 及其受保护存储；构造不创建目录、复制文件或调用模型。"""
        self.manager = manager  # 唯一执行调度器，负责后台任务、取消和收尾。
        self.store = SessionStore(manager.runs_root)  # 会话线性关系与请求幂等。
        self.files = FileSnapshotStore(manager.runs_root)  # 普通工作区的受限完整副本。
        self.submission_digest: str | None = None  # 内存中尚未落盘请求的载荷摘要，用于并发幂等。
        self.submission_key: tuple[UUID, UUID] | None = None  # 正在准备的会话/请求，供同进程重复请求共享等待。

    def _origin(self, history: 'RunHistory') -> RunOrigin:
        """核对 history 的宿主来源，返回固定登记项；兼容已有可核验的旧 Git 运行。"""
        origin = history.manifest.origin
        if origin is None:
            review = self.manager.projects.get(history.manifest.run_id)
            if review is not None:
                origin = RunOrigin(kind='project', preset_id=review.project_id, registered_path=Path(review.repository))
        if origin is None:
            raise SessionError('continuation_source_missing', '旧记录没有可核对的工作区来源，请新建任务。')
        presets = self.manager.web_config.projects if origin.kind == 'project' else self.manager.web_config.workspaces
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
        if origin.kind == 'project':
            review = self.manager.projects.get(history.manifest.run_id)
            if review is None or (not interrupted and (review.snapshot_status != 'ready' or not review.patch_available)):
                raise SessionError('project_snapshot_required', '前轮代码快照尚未完整可用，不能继续。')
            if review.status == 'discarded':
                raise SessionError('project_already_decided', '最新一轮工作树已经丢弃，不能继续修改。')
        else:
            info = self.files.info(history.manifest.run_id)
            if info is None and interrupted:
                workspace = history.manifest.task.workspace
                if not workspace.is_dir() or workspace.is_symlink() or workspace.is_junction() or workspace.resolve() != workspace:
                    raise SessionError('workspace_source_changed', '中断工作区缺失或身份无法核对。')
                return origin
            if info is None:
                raise SessionError('workspace_snapshot_missing', '前轮没有可继续的文件快照，请新建任务。')
            if info['status'] != 'ready':
                raise SessionError(info.get('error_code', 'workspace_snapshot_missing'), info.get('error_message', '前轮文件快照无法核对。'))
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
        blocked = None
        for index, identifier in enumerate(record.run_ids):
            expected_parent = record.run_ids[index - 1] if index else None
            ordinal = index + 1
            intent = next((item for item in record.requests.values() if item.run_id == identifier), None)
            try:
                history = self.manager._read_history(identifier)
                manifest = history.manifest
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
        if busy is not None or preparing:
            blocked = SessionError('run_busy', '已有任务正在准备、运行或保存快照，请等待完成或取消。')
        elif any(item.state == 'preparing' for item in record.requests.values()):
            blocked = SessionError('request_uncertain', '上次追加准备尚未确认，请核对本地状态；不会自动重新执行。')
        elif blocked is None and latest_history is not None:
            try:
                self._ready(latest_history)
            except (SessionError, SnapshotError, ProjectError) as error:
                blocked = SessionError(error.code, error.message)
        return ConversationView(session_id=session_id, latest_run_id=record.run_ids[-1], turns=turns, can_continue=blocked is None and latest_history is not None, blocked_code=blocked.code if blocked else None, blocked_reason=blocked.message if blocked else None, busy_run_id=busy, continuation_notice='前轮中断。可直接发送“继续”，继承已保存记录和当前代码；结果不明的工具需要先核对，本轮重新测试。' if blocked is None and latest_history is not None and latest_history.interrupted else None)

    async def submit(self, parent_id: UUID, request: FollowupRequest):
        """幂等接纳追加 request；HTTP断开不取消准备，同进程重复请求共享原任务等待。"""
        parent = self.manager._read_history(parent_id)
        session_id = parent.manifest.session_id or parent_id
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
        """持有全局单槽核验父状态、派生新工作区并启动 Runner；失败不修改父证据。"""
        async with self.manager._lock:
            if self.manager._closing:
                raise SessionError('service_closing', '本地服务正在关闭。')
            if self.manager.busy_run_id is not None:
                raise SessionError('run_busy', '已有任务正在运行。')
            parent = self.manager._read_history(parent_id)
            if self.store.read(session_id).run_ids[-1] != parent_id:
                raise SessionError('session_stale_parent', '只能从会话最新一轮继续，请先打开最新运行。')
            origin = self._ready(parent)
            interrupted = parent.result is None and parent.interrupted
            inherited = history_messages(parent, allow_interrupted=interrupted)
            merged = parent.manifest.config.model_dump(mode='python')
            # 宿主系统规则重新生成；网页可编辑部分默认沿用前轮，覆盖仍严格校验。
            merged['system_prompt'] = self.manager.run_config.system_prompt
            patch = request.config.model_dump(exclude_none=True)
            for section in ('limits', 'context'):
                if section in patch:
                    merged[section] = {**merged[section], **patch.pop(section)}
            merged.update(patch)
            try:
                config = load_config(overrides=merged)
                prompt = request.prompt.strip()
                if not prompt:
                    raise SessionError('invalid_prompt', '追加要求不能为空白。')
                identifier = uuid4()
                expected = parent.manifest.task.expected_outputs if request.expected_outputs is None else request.expected_outputs
                task = parent.manifest.task.model_copy(update={'prompt': prompt, 'expected_outputs': expected})
                manifest = RunManifest(run_id=identifier, session_id=session_id, parent_run_id=parent_id, turn_index=parent.manifest.turn_index + 1, task=task, config=config, origin=origin, history_source=HistorySource(last_seq=parent.events[-1].seq, messages_sha256=payload_digest(inherited), interrupted=interrupted))
                import os
                api_key = os.environ.get(config.api_key_env, '')
                validate_execution(manifest, api_key)
                validate_output_paths(task, self.manager.runs_root)
            except (ConfigurationError, ValidationError) as error:
                raise SessionError('invalid_run', str(error) if isinstance(error, ConfigurationError) else '追加任务配置无效。') from None
            self.store.begin(session_id, request.request_id, parent_id, identifier, digest, prompt)
            try:
                if origin.kind == 'project':
                    preset = next(item for item in self.manager.web_config.projects if item.id == origin.preset_id)
                    def prepare_project():
                        """沿用原逐轮代码副本流程；中断来源只在明确发送时捕获一次当前代码。"""
                        if interrupted:
                            self.manager.projects.capture(parent_id)
                        return self.manager.projects.prepare_followup(preset, parent_id, identifier)
                    preparation = asyncio.create_task(asyncio.to_thread(prepare_project))
                    try:
                        review = await asyncio.shield(preparation)
                    except asyncio.CancelledError:
                        await asyncio.shield(preparation)
                        raise
                    root = Path(review.worktree)
                    verifier = VerifierSpec(argv=[part.replace('{workspace}', str(root)) for part in preset.test_argv], cwd=root, timeout_seconds=preset.test_timeout_seconds, python_paths=[root / part for part in preset.test_python_paths])
                else:
                    root = self.manager.web_config.runtime_root.resolve() / str(identifier)
                    def prepare_workspace():
                        """为缺少收尾快照的中断目录补一次有界副本，再复用已有派生校验。"""
                        if interrupted and self.files.info(parent_id) is None:
                            self.files.capture(parent_id, parent.manifest.task.workspace)
                        self.files.restore(parent_id, parent.manifest.task.workspace, root)
                    preparation = asyncio.create_task(asyncio.to_thread(prepare_workspace))
                    try:
                        await asyncio.shield(preparation)
                    except asyncio.CancelledError:
                        await asyncio.shield(preparation)
                        raise
                    verifier_path = None
                    if origin.verifier_id is not None:
                        preset = next((item for item in self.manager.web_config.verifiers if item.id == origin.verifier_id), None)
                        if preset is None:
                            raise SessionError('continuation_verifier_missing', '前轮验收预设已移除，请核对宿主配置。')
                        verifier_path = preset.path
                    verifier = prepare_task(prompt, root, verifier_path, config.limits.verifier_timeout_seconds, expected).verifier
                task = task.model_copy(update={'workspace': root, 'verifier': verifier})
                manifest = manifest.model_copy(update={'task': task})
                validate_output_paths(task, self.manager.runs_root)
                self.store.commit(session_id, request.request_id)
                return await self.manager._launch(manifest, api_key)
            except (SessionError, SnapshotError, ProjectError, ConfigurationError, StorageError, ValidationError, OSError) as error:
                code = error.code if isinstance(error, (SessionError, SnapshotError, ProjectError)) else 'followup_prepare_failed'
                message = error.message if isinstance(error, (SessionError, SnapshotError, ProjectError)) else '后续运行准备失败，已有父结果保持不变。'
                self.store.fail(session_id, request.request_id, code, message)
                if not (self.manager.runs_root / str(identifier) / 'run.json').exists() and self.manager.projects.get(identifier) is not None:
                    try:
                        await asyncio.to_thread(self.manager.projects.capture, identifier)
                        await asyncio.to_thread(self.manager.projects.decide, identifier, 'discard')
                    except (ProjectError, OSError):
                        message += ' 新建工作树保留供核对。'
                raise SessionError(code, message) from None
