"""显式检查点恢复的只读能力视图与幂等提交；复用单槽，不自动重放旧工具。"""

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import ValidationError

from ..checkpoints import CheckpointError, CheckpointStore, accumulated_budget
from ..config import ConfigurationError
from ..projects import ProjectError
from ..runner import validate_execution, validate_output_paths
from ..schemas import ResumeSource, RunManifest, VerifierSpec
from ..sessions import SessionError, payload_digest
from ..store import StorageError
from .schemas import CheckpointView, RecoveryView, ResumeRequest, RunView

if TYPE_CHECKING:
    from .service import WebRunManager
    from ..store import RunHistory


class RecoveryService:
    """协调恢复来源、请求意图与独立工作树；所有执行仍交给现有Runner。"""

    def __init__(self, manager: "WebRunManager") -> None:
        """绑定manager及不可变检查点存储；构造不读取Git、不创建运行或目录。"""
        self.manager = manager  # 唯一Web调度器，提供全局单槽和生命周期。
        self.checkpoints = CheckpointStore(manager.runs_root, manager.projects)  # 原运行的封存检查点与来源核验。
        self.sessions = manager.conversations.store  # 与追加要求共用请求编号和线性会话记录。

    def _source(self, history: "RunHistory"):
        """核对history属于最新Git尝试及宿主来源；返回固定项目预设，不运行Git命令。"""
        manifest = history.manifest
        origin = self.manager.conversations._origin(history)
        if origin.kind != "project":
            raise SessionError("recovery_unsupported", "当前仅支持Git代码任务的检查点恢复。")
        session_id = manifest.session_id or manifest.run_id
        record = self.sessions.read(session_id)
        if record.run_ids[-1] != manifest.run_id:
            raise SessionError("recovery_stale_attempt", "只能恢复会话最新一次执行，请先打开最新运行。")
        if any(item.state == "preparing" for item in record.requests.values()):
            raise SessionError("request_uncertain", "会话存在尚未确认的准备请求，请核对本地状态；不会自动重放。")
        preset = next(item for item in self.manager.web_config.projects if item.id == origin.preset_id)
        old_root = manifest.task.workspace
        expected = VerifierSpec(
            argv=[part.replace("{workspace}", str(old_root)) for part in preset.test_argv],
            cwd=old_root, timeout_seconds=preset.test_timeout_seconds,
            python_paths=[old_root / part for part in preset.test_python_paths],
        )
        if manifest.task.verifier != expected:
            raise SessionError("recovery_verifier_changed", "宿主测试预设已改变，无法按原检查点固定配置恢复。")
        return preset

    def view(self, run_id: UUID) -> RecoveryView:
        """只读初检run_id及最新检查点；不拍摄代码或运行Git，提交时仍需强核验。"""
        view = self.manager.get_run(run_id)
        history = self.manager._read_history(run_id)
        supported = bool(view.project is not None or (history.manifest.origin is not None and history.manifest.origin.kind == "project"))
        checkpoint = None
        blocked = None
        try:
            if not supported:
                raise SessionError("recovery_unsupported", "当前仅支持Git代码任务的检查点恢复。")
            self._source(history)
            checkpoint = self.checkpoints.latest(history)
            if checkpoint is None:
                raise SessionError("checkpoint_missing", "此运行尚未保存完整的安全检查点。")
            checkpoint = self.checkpoints.check_readiness(history, checkpoint)
            if self.manager.busy_run_id is not None or (self.manager._submission is not None and not self.manager._submission.done()):
                raise SessionError("run_busy", "已有任务正在准备、运行或保存快照，请等待完成或取消。")
        except (CheckpointError, SessionError, ProjectError) as error:
            blocked = error
        if checkpoint is not None:
            checkpoint = checkpoint.model_copy(update={"budget": accumulated_budget(history)}, deep=True)
        checkpoint_view = None if checkpoint is None else CheckpointView(
            checkpoint_id=checkpoint.checkpoint_id, created_at=checkpoint.created_at,
            event_seq=checkpoint.last_seq, model_turns=checkpoint.budget.model_turns,
            tool_calls=checkpoint.budget.tool_calls, active_duration_seconds=checkpoint.budget.active_duration_seconds,
            summary_calls=checkpoint.budget.summary_calls,
        )
        return RecoveryView(supported=supported, can_resume=blocked is None, blocked_code=blocked.code if blocked else None, blocked_reason=blocked.message if blocked else None, checkpoint=checkpoint_view, busy_run_id=self.manager.busy_run_id)

    async def submit(self, run_id: UUID, request: ResumeRequest) -> RunView:
        """幂等接纳run_id的显式恢复请求；相同载荷共享原准备任务，断网不取消准备。"""
        history = self.manager._read_history(run_id)
        session_id = history.manifest.session_id or run_id
        digest = payload_digest({"kind": "resume", "source_run_id": str(run_id), "checkpoint_id": str(request.checkpoint_id)})
        conversation = self.manager.conversations
        if conversation.submission_key == (session_id, request.request_id) and self.manager._submission is not None and not self.manager._submission.done():
            if conversation.submission_digest != digest:
                raise SessionError("request_conflict", "该请求编号已用于不同内容。")
            return await asyncio.shield(self.manager._submission)
        intent = self.sessions.lookup(session_id, request.request_id, digest)
        if intent is not None:
            if intent.state == "committed":
                try:
                    return self.manager.get_run(intent.run_id)
                except Exception:
                    raise SessionError("request_uncertain", "原恢复请求已接纳但记录尚未完整建立，请核对；不会重复执行。") from None
            if intent.state == "failed":
                raise SessionError(intent.error_code or "request_failed", intent.error_message or "原恢复准备失败，请核对后重新提交。")
            raise SessionError("request_uncertain", "原恢复请求准备状态尚未确认，不会自动重放。")
        if self.manager._closing:
            raise SessionError("service_closing", "本地服务正在关闭。")
        if self.manager.busy_run_id is not None or (self.manager._submission is not None and not self.manager._submission.done()):
            raise SessionError("run_busy", "已有任务正在准备或运行，请等待完成或取消。")
        preparation = asyncio.create_task(self._start(run_id, session_id, request, digest), name="harness-resume-prepare")
        self.manager._submission = preparation
        conversation.submission_key = (session_id, request.request_id)
        conversation.submission_digest = digest

        def observe(task: asyncio.Task[RunView]) -> None:
            """取回断开连接后的准备异常；正常结果仍由shield返回，不再次执行请求。"""
            if not task.cancelled():
                task.exception()
        preparation.add_done_callback(observe)
        return await asyncio.shield(preparation)

    async def _start(self, run_id: UUID, session_id: UUID, request: ResumeRequest, digest: str) -> RunView:
        """持有单槽强核验来源、先登记意图后派生工作树，提交关系后只启动一次Runner。"""
        async with self.manager._lock:
            if self.manager._closing:
                raise SessionError("service_closing", "本地服务正在关闭。")
            if self.manager.busy_run_id is not None:
                raise SessionError("run_busy", "已有任务正在运行。")
            history = self.manager._read_history(run_id)
            preset = self._source(history)
            latest = self.checkpoints.latest(history)
            if latest is None:
                raise SessionError("checkpoint_missing", "此运行尚未保存完整的安全检查点。")
            if latest.checkpoint_id != request.checkpoint_id:
                raise SessionError("checkpoint_stale", "请使用此运行最新检查点，旧检查点不会自动回退执行。")
            checkpoint = await asyncio.to_thread(self.checkpoints.verify, history, request.checkpoint_id)
            identifier = uuid4()
            source = history.manifest
            manifest = RunManifest(
                run_id=identifier, task=source.task.model_copy(deep=True), config=source.config.model_copy(deep=True),
                session_id=session_id, parent_run_id=run_id, turn_index=source.turn_index,
                attempt_index=source.attempt_index + 1, origin=source.origin,
                resume_source=ResumeSource(source_run_id=run_id, checkpoint_id=checkpoint.checkpoint_id, checkpoint_sha256=checkpoint.sha256),
            )
            api_key = os.environ.get(manifest.config.api_key_env, "")
            validate_execution(manifest, api_key)
            validate_output_paths(manifest.task, self.manager.runs_root)
            self.sessions.begin(session_id, request.request_id, run_id, identifier, digest, manifest.task.prompt, kind="resume", checkpoint_id=request.checkpoint_id, turn_index=manifest.turn_index, attempt_index=manifest.attempt_index)
            try:
                preparation = asyncio.create_task(asyncio.to_thread(self.checkpoints.prepare_resume, preset, history, checkpoint, identifier))
                try:
                    review = await asyncio.shield(preparation)
                except asyncio.CancelledError:
                    # Git线程不可强停；等待归属固定后保留未确认意图，重启禁止盲目重放。
                    await asyncio.shield(preparation)
                    raise
                root = Path(review.worktree)
                verifier = VerifierSpec(argv=[part.replace("{workspace}", str(root)) for part in preset.test_argv], cwd=root, timeout_seconds=preset.test_timeout_seconds, python_paths=[root / part for part in preset.test_python_paths])
                manifest = manifest.model_copy(update={"task": manifest.task.model_copy(update={"workspace": root, "verifier": verifier})})
                validate_output_paths(manifest.task, self.manager.runs_root)
                self.sessions.commit(session_id, request.request_id)
                return await self.manager._launch(manifest, api_key)
            except (CheckpointError, SessionError, ProjectError, ConfigurationError, StorageError, ValidationError, OSError) as error:
                code = error.code if isinstance(error, (CheckpointError, SessionError, ProjectError)) else "recovery_prepare_failed"
                message = error.message if isinstance(error, (CheckpointError, SessionError, ProjectError)) else "恢复准备失败，原运行与检查点保持不变。"
                self.sessions.fail(session_id, request.request_id, code, message)
                if not (self.manager.runs_root / str(identifier) / "run.json").exists() and self.manager.projects.get(identifier) is not None:
                    try:
                        await asyncio.to_thread(self.manager.projects.capture, identifier)
                        await asyncio.to_thread(self.manager.projects.decide, identifier, "discard")
                    except (ProjectError, OSError):
                        message += " 新建工作树保留供核对。"
                raise SessionError(code, message) from None
