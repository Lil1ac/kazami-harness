"""单机作业接纳与持久排队；只调度现有 Runner，不把工作项拆成独立运行。"""

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import Field

from agent_harness.schemas import DataModel
from agent_harness.storage.sessions import payload_digest, SessionError
from agent_harness.storage.runs import RunStore, StorageError
from agent_harness.work.tasks import WorkStore, TaskError, ExecutionLease, encode, now
from agent_harness.web.schemas import CreateRunRequest, FollowupRequest
from agent_harness.config import load_config, prepare_task, ConfigurationError
from agent_harness.schemas import RunManifest
from agent_harness.runtime.runner import validate_execution, validate_auto_repair
from agent_harness.work.planning import continue_planning, planning_from_history
from agent_harness.execution.python import prepare_python_execution, PythonExecutionError


def directory_identity(path: Path) -> list[int]:
    """返回已由宿主核对归属的 path 目录身份；允许会话工作树，替换/链接抛 TaskError。"""
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir() or path.is_symlink() or path.is_junction():
            raise OSError
        stat = path.stat()
        return [stat.st_dev, stat.st_ino]
    except OSError:
        raise TaskError("workspace_changed", "排队目录不可用或已被替换，请核对后重新打开。") from None


def source_policy(source) -> dict:
    """固定宿主登记 source 的解释器、源码范围及公开测试设置，返回可比较快照，不含密钥。"""
    return {"python_executable": source.python_executable, "python_paths": source.python_paths,
            "test_argv": getattr(source, "test_argv", []), "test_timeout_seconds": getattr(source, "test_timeout_seconds", None)}


class JobRequest(DataModel):
    """统一接纳请求；parent_run_id 非空时按续轮核验，首次请求使用 create。"""

    request_id: UUID = Field(strict=False)  # 客户端重试必须保留的唯一编号。
    create: CreateRunRequest | None = None  # 首次运行参数；与 followup 二选一。
    parent_run_id: UUID | None = Field(default=None, strict=False)  # 续轮绑定的最新父记录，HTTP 使用 UUID 文本。
    followup: FollowupRequest | None = None  # 用户回答、决定或普通接续。


class JobQueue:
    """宿主唯一队列；排队状态和请求内容事务保存，失联运行需要人工核对。"""

    def __init__(self, manager):
        """绑定 manager；构造不恢复执行，已有队列默认暂停等待用户明确恢复。"""
        self.manager = manager  # 复用现有工作区准备、会话和 Runner。
        self.store = manager.management.store  # 会话、任务与队列共用事务，避免删除与接纳竞态。
        self.worker: asyncio.Task | None = None  # 唯一队列循环句柄。
        self.paused = any(job["status"] in {"queued", "preparing", "running", "needs_review"} for job in self.list())  # 重启只核对，不自动重放历史动作。
        self.closed = False  # 正在退出时停止出队。
        self._ready: dict[str, asyncio.Event] = {}  # 新提交首项建立 Run 前的响应同步。
        self.reconcile()

    def list(self) -> list[dict]:
        """返回按接纳时间排序的作业副本；不启动执行，最多展示最近五百项。"""
        if not self.store.path.exists():
            return []
        with self.store.transaction() as db:
            rows = db.execute("SELECT body FROM jobs WHERE id IN (SELECT id FROM jobs ORDER BY rowid DESC LIMIT 500) OR json_extract(body,'$.status') IN ('queued','preparing','running','needs_review') ORDER BY rowid DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, identifier: str) -> dict:
        """读取 identifier 作业；不存在抛 TaskError，不回显内部请求载荷。"""
        with self.store.transaction() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise TaskError("job_not_found", "后台作业不存在。")
        return json.loads(row[0])

    def public(self, job: dict, db=None) -> dict:
        """返回 job 可展示字段；排除固定内部提交参数，保留归属、结果和错误。"""
        value = {key: value for key, value in job.items() if key not in {"payload", "digest", "memory_policy"}}
        session = job.get("session_id")
        if session:
            record = self.manager.management.stored(session, db)
            if record is None:
                # 未人工整理的续轮也使用首条需求标题，不能把本轮输入当成会话名。
                from agent_harness.web.conversation_management import ConversationError
                try:
                    record = self.manager.management.record(session, db)
                except ConversationError as error:
                    if error.code != "conversation_not_found":
                        raise
                    # 首轮尚在排队准备、尚无Run时，只展示接纳时固定的作业输入。
            if record:
                if record["state"] in {"trashed", "purging", "purged"}:
                    return {key: value for key, value in {**value, "title": "已删除对话", "project_id": "", "project_label": "已删除对话", "assigned_project": None, "source_path": None, "error": None}.items() if key not in {"source_policy", "workspace_identity"}}
                value.update(conversation_title=record["title_override"] or record["default_title"], assigned_project=self.manager.management.project(record))
        return value

    def save(self, job: dict) -> None:
        """原子保存 job 并提交状态事件；返回 None，不修改运行结果。"""
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?)", (job["id"], encode(job)))
            self.store.event(db, job.get("session_id") or job["id"], {"type": "job_updated", "job": self.public(job, db)})

    def reconcile(self) -> None:
        """仅在没有其他宿主执行时核对遗留归属；活动锁存在则只暂停本队列，不改他人状态。"""
        if not any(job["status"] in {"preparing", "running"} for job in self.list()):
            return
        lease = ExecutionLease(self.manager.runs_root)
        try:
            lease.acquire()
        except TaskError:
            self.paused = True
            return
        try:
            self._reconcile_unowned()
        finally:
            lease.release()

    def _reconcile_unowned(self) -> None:
        """启动时将已领作业与 RunStore 核对；无可信终态标待核对，不调用模型或旧工具。"""
        for job in self.list():
            if job["status"] not in {"preparing", "running"}:
                continue
            try:
                history = RunStore.read(self.manager.runs_root, job["run_id"])
                if history.result is not None:
                    self.finish(job, history.result)
                    continue
            except StorageError:
                pass
            job.update(status="needs_review", error="上次执行中断，请先确认进程已停止并核对工作区。")
            self.save(job)

    async def enqueue(self, request: JobRequest) -> dict:
        """持久接纳 request 并返回作业；空闲时等待首次准备，忙碌时立即返回排队状态。"""
        if self.closed or self.manager._closing:
            raise TaskError("service_closing", "服务正在关闭。")
        if (request.create is None) == (request.followup is None) or (request.followup is not None and request.parent_run_id is None) or (request.create is not None and request.parent_run_id is not None):
            raise TaskError("job_invalid", "首次和追加运行参数必须二选一。")
        payload = request.model_dump(mode="json")
        if request.create is not None:
            payload["create"]["config"] = request.create.config.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        elif request.followup is not None:
            payload["followup"]["config"] = request.followup.config.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        identifier, digest = str(request.request_id), payload_digest(payload)
        old = None
        if self.store.path.exists():
            with self.store.transaction() as db:
                old = db.execute("SELECT body FROM jobs WHERE id=?", (identifier,)).fetchone()
        if old:
            value = json.loads(old[0])
            if value["digest"] != digest:
                raise TaskError("request_conflict", "请求编号已用于其他内容。")
            return self.public(value)
        session = None
        if request.followup is not None:
            if request.followup.request_id != request.request_id:
                raise TaskError("request_conflict", "作业和续轮请求编号必须一致。")
            self.manager.get_run(request.parent_run_id)  # 统一不存在与损坏运行的 HTTP 语义。
            parent = self.manager._read_history(request.parent_run_id)
            session = str(parent.manifest.session_id or request.parent_run_id)
            self.manager.management.require_active(session)
            intent_digest = payload_digest({'parent_run_id': str(request.parent_run_id), **request.followup.model_dump(mode='json', exclude={'request_id'})})
            intent = self.manager.conversations.store.lookup(UUID(session), request.request_id, intent_digest)
            if intent is not None:
                if intent.state == 'failed':
                    raise SessionError(intent.error_code or 'request_failed', intent.error_message or '原请求准备失败，请核对。')
                raise SessionError('request_uncertain', '原请求已登记但作业归属无法核对，不会自动重放。')
            if self.manager.busy_run_id == request.parent_run_id:
                raise TaskError("session_busy", "当前会话仍在执行，请结束后发送。")
            if self.manager.conversations.store.read(UUID(session)).run_ids[-1] != request.parent_run_id:
                raise TaskError("session_stale_parent", "请从最新一轮继续。")
            self.manager.conversations._ready(parent)
            continue_planning(planning_from_history(parent), request.followup.decision, request.followup.prompt, parent.manifest.task.workspace, request.followup.mode)
        # 入队只冻结公开配置及目录身份，项目准备和预算计时留到真正出队。
        if request.create is not None:
            selected = next((item for item in [*self.manager.catalog.project_presets(), *self.manager.catalog.workspace_presets()] if item.id == (request.create.project_id or request.create.workspace_id)), None)
            if selected is None:
                raise TaskError("unknown_workspace", "请选择已登记的项目目录。")
            identity = directory_identity(selected.path)
            source_path = str(selected.path.resolve())
            overrides = self.manager.run_config.model_dump(mode="json", include={"requested_model", "tools", "limits", "context", "memory", "skills", "auto_repair"})
            patch = payload["create"]["config"]
            for section in ("limits", "context", "memory"):
                overrides[section].update(patch.get(section) or {})
            overrides.update({key: value for key, value in patch.items() if key not in {"limits", "context", "memory"} and value is not None})
            payload["create"]["config"] = {**overrides, "memory": {key: overrides["memory"][key] for key in ("use", "capture")}}
            config_data = self.manager.run_config.model_dump(mode="python")
            config_data.update(overrides)
            config = load_config(overrides=config_data)
            verifier = None
            if request.create.verifier_id is not None:
                verifier = next((item.path for item in self.manager.web_config.verifiers if item.id == request.create.verifier_id), None)
                if verifier is None:
                    raise TaskError("unknown_verifier", "请选择已登记的验收器。")
            task = prepare_task(request.create.prompt, selected.path, verifier, config.limits.verifier_timeout_seconds, python_paths=selected.python_paths, python_executable=selected.python_executable)
            manifest = RunManifest(run_id=uuid4(), task=task, config=config)
            validate_execution(manifest, self.manager.api_key(config))
            if config.auto_repair and not getattr(selected, "test_argv", None):
                validate_auto_repair(manifest)
            if request.create.execution_mode == "isolated":
                if request.create.project_id is None:
                    raise TaskError("workspace_copy_not_supported", "普通目录直接在原目录执行。")
                _, commit, _ = await asyncio.to_thread(self.manager.projects._inspect_base, selected, request.create.base_ref)
                payload["create"]["base_ref"] = commit  # 队列期间分支移动不会改变已选隔离基线。
        else:
            source_path = str(parent.manifest.task.workspace)
            identity = directory_identity(parent.manifest.task.workspace)
            selected = next(item for item in [*self.manager.catalog.project_presets(), *self.manager.catalog.workspace_presets()] if item.id == parent.manifest.origin.preset_id)
            try:
                prepare_python_execution(parent.manifest.task.workspace, selected.python_paths, python_executable=selected.python_executable)
            except PythonExecutionError as error:
                raise SessionError('invalid_run', str(error)) from None  # 已知无效配置在入队前拒绝，允许更正后用新请求重试。
        if session:
            # Runner 已落盘结束、队列回调尚未执行的短窗口内，先对账旧作业再接纳续轮。
            for previous in self.list():
                if previous['session_id'] == session and previous['status'] == 'running' and str(self.manager.busy_run_id) != previous['run_id']:
                    history = RunStore.read(self.manager.runs_root, previous['run_id'])
                    if history.result is not None:
                        self.finish(previous, history.result)
        job = {"id": identifier, "status": "queued", "run_id": str(uuid4()), "session_id": session,
               "title": (request.create.prompt if request.create else request.followup.prompt)[:200], "project_id": request.create.project_id or request.create.workspace_id if request.create else parent.manifest.origin.preset_id,
               "created_at": now(), "payload": payload, "digest": digest, "source_path": source_path, "workspace_identity": identity,
               "memory_policy": config.memory.model_dump() if request.create is not None else {**parent.manifest.config.memory.model_dump(), **(request.followup.config.memory.model_dump(exclude_unset=True) if request.followup.config.memory else {})},
               "project_label": selected.label, "source_policy": source_policy(selected), "error": None, "validation_status": "unverified", "execution_status": None}
        with self.store.transaction() as db:
            old = db.execute("SELECT body FROM jobs WHERE id=?", (identifier,)).fetchone()
            if old:
                value = json.loads(old[0])
                if value["digest"] != digest:
                    raise TaskError("request_conflict", "请求编号已用于其他内容。")
                return self.public(value, db)
            if session:
                self.manager.management.require_active(session, db)
            count = db.execute("SELECT count(*) FROM jobs WHERE json_extract(body,'$.status') IN ('queued','preparing','running')").fetchone()[0]
            if count >= 100:
                raise TaskError("queue_full", "后台队列最多接纳一百个未结束请求，请先完成或取消已有工作。")
            if session and any(item["session_id"] == session and item["status"] in {"queued", "preparing", "running", "needs_review"} for item in (json.loads(row[0]) for row in db.execute("SELECT body FROM jobs"))):
                raise TaskError("session_job_pending", "会话已有待处理作业，请先处理或取消。")
            db.execute("INSERT INTO jobs VALUES (?,?)", (identifier, encode(job)))
            self.store.event(db, session or identifier, {"type": "job_updated", "job": self.public(job, db)})
        self._ready[identifier] = asyncio.Event()
        idle = self.worker is None or self.worker.done()
        self.kick()
        if idle and not self.paused and not self.manager.busy_run_id:
            await self._ready[identifier].wait()
        return self.public(self.get(identifier))

    def kick(self) -> None:
        """需要时启动唯一队列循环；暂停或退出时无操作，返回 None。"""
        if not self.closed and not self.paused and (self.worker is None or self.worker.done()):
            self.worker = asyncio.create_task(self.run(), name="harness-job-queue")

    async def run(self) -> None:
        """按接纳顺序串行执行排队请求；存储或清理不明时暂停，返回 None。"""
        while not self.closed and not self.paused:
            candidates = [job for job in reversed(self.list()) if job["status"] == "queued"]
            if not candidates:
                return
            job = candidates[0]
            lease = ExecutionLease(self.manager.runs_root)  # 覆盖工作树准备及全部证据收尾。
            try:
                while self.manager.busy_run_id and not self.closed:
                    await asyncio.sleep(.05)
                if self.closed:
                    return
                lease.acquire()
                self.manager._execution_lease = lease
                if job.get("session_id"):
                    self.manager.management.require_active(job["session_id"])
                if directory_identity(Path(job["source_path"])) != job["workspace_identity"]:
                    raise TaskError("workspace_changed", "排队期间目录身份发生变化，请重新打开项目。")
                selected = next((item for item in [*self.manager.catalog.project_presets(), *self.manager.catalog.workspace_presets()] if item.id == job["project_id"]), None)
                if selected is None or source_policy(selected) != job["source_policy"]:
                    raise TaskError("job_source_changed", "排队期间项目解释器或测试设置已变化，请核对后重新提交。")
                job["status"] = "preparing"
                self.save(job)
                self.manager._queued_run_id = UUID(job["run_id"])
                self.manager._queued_memory_policy = job["memory_policy"]  # 接纳时固定策略，记忆内容在实际执行时读取。
                payload = JobRequest.model_validate(job["payload"])
                view = await self.manager.submit(payload.create) if payload.create is not None else await self.manager.conversations.submit(payload.parent_run_id, payload.followup)
                job.update(status="running", run_id=str(view.run_id), session_id=str(view.session_id or view.run_id), execution_status="running")
                self.save(job)
                self._ready.get(job["id"], asyncio.Event()).set()
                await self.manager.wait_idle()
                view = self.manager.get_run(view.run_id)
                self.finish(job, view.result)
                if view.result.cleanup_status in {"unknown", "incomplete"}:
                    self.paused = True
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if isinstance(error, TaskError) and error.code == "execution_busy":
                    self.paused = True  # 另一宿主仍持锁，本队列无权改写其作业归属。
                    return
                job.update(status="needs_review", error=getattr(error, "message", "准备或记录未能确认，请核对后重新提交。"), error_code=getattr(error, "code", "job_prepare_failed"), error_status=getattr(error, "status", 409))
                self.save(job)
                self.paused = True
            finally:
                self.manager._execution_lease = None
                lease.release()
                self.manager._queued_run_id = None
                self.manager._queued_memory_policy = None
                self._ready.get(job["id"], asyncio.Event()).set()

    def finish(self, job: dict, result) -> None:
        """用可信 result 固定 job 展示状态；等待用户和检查结果分别记录，返回 None。"""
        status = "waiting_input" if result.execution_status == "waiting_input" else "cancelled" if result.execution_status == "cancelled" else "completed" if result.execution_status == "completed" else "failed"
        job.update(status=status, execution_status=result.execution_status, validation_status=result.validation_status, finished_at=now())
        self.save(job)

    async def cancel(self, identifier: str) -> dict:
        """取消 identifier 的排队或活动作业；活动项等待 Runner 清理，不回滚已有项目修改。"""
        job = self.get(identifier)
        if job["status"] == "preparing":
            raise TaskError("job_preparing", "正在准备执行目录，请稍后停止运行。")
        if job["status"] == "running":
            await self.manager.cancel(UUID(job["run_id"]))
            await self.manager.wait_idle()
            return self.public(self.get(identifier))
        if job["status"] == "waiting_input":
            raise TaskError("job_waiting_decision", "请进入会话取消当前问题或拒绝计划，宿主需要保存明确决定。")
        if job["status"] == "queued":
            job.update(status="cancelled", finished_at=now())
            self.save(job)
        return self.public(job)

    def resolve(self, identifier: str) -> dict:
        """记录用户已核对 identifier 的进程和工作区；不重放作业，只解除该项待核对阻塞。"""
        job = self.get(identifier)
        if job["status"] != "needs_review":
            raise TaskError("job_not_uncertain", "该作业不处于待核对状态。")
        job.update(status="failed", error="用户已确认进程停止并核对工作区；原请求不重放。", finished_at=now())
        self.save(job)
        return self.public(job)

    def resume(self) -> None:
        """用户明确恢复未执行队列；存在待核对作业时拒绝，不恢复旧工具。"""
        if any(job["status"] == "needs_review" for job in self.list()):
            raise TaskError("queue_needs_review", "请先核对中断作业的进程和工作区。")
        self.paused = False
        self.kick()
