"""恢复已中断的手动 Git 运行。接纳、资源交接与普通队列使用同一事务。"""

from datetime import datetime
import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import Field

from agent_harness.execution.python import prepare_python_execution
from agent_harness.projects import ProjectError
from agent_harness.runtime.control import ControlStore
from agent_harness.runtime.crash_recovery import inspect_crash
from agent_harness.schemas import CrashResume, DataModel
from agent_harness.storage.runs import RunStore, StorageError
from agent_harness.storage.sessions import SessionError, history_messages, payload_digest
from agent_harness.tools.files import FileToolError
from agent_harness.web.conversation_management import ConversationError
from agent_harness.web.schemas import FollowupRequest
from agent_harness.work.tasks import TaskError, encode, now

if TYPE_CHECKING:
    from agent_harness.web.jobs import JobQueue


class RecoveryRequest(DataModel):
    """用户明确接续某个中断作业。请求只携带幂等编号与所见恢复版本。"""

    request_id: UUID = Field(strict=False)  # 同一按钮请求断线重试时保持原编号。
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")  # 绑定用户看到的恢复证据版本。


class CrashRecovery:
    """把只读崩溃核对结果接入持久作业队列，并在执行前重新验证。"""

    def __init__(self, queue: "JobQueue") -> None:
        """绑定拥有调度锁和 SQLite 存储的 queue。构造不启动或接管作业。"""
        self.queue = queue  # 创建、取消与转移资源使用相同的作业表。
        self.manager = queue.manager  # 复用会话、工作树和宿主配置。

    def checkpoint(self, job: dict, *, clean: bool = False) -> tuple[CrashResume, str]:
        """从 job 直接父段重算恢复输入与版本。clean 只在接管后用于清理有证据的暂存文件。"""
        from agent_harness.web.jobs import directory_identity, source_policy
        from agent_harness.web.service import WebServiceError

        try:
            history = RunStore.read(self.manager.runs_root, job["run_id"])
            manifest = history.manifest
            identifier = manifest.run_id
            if job.get("stop_requested") or not manifest.crash_recovery or history.result is not None:
                raise TaskError("recovery_unavailable", "这次运行没有可接续的崩溃检查点。")
            if self.manager.conversations.store.read(UUID(job["session_id"])).run_ids[-1] != identifier:
                raise TaskError("recovery_stale_run", "中断段已不是会话最新轮次。")
            self.manager.management.require_active(job["session_id"])
            control = ControlStore(self.manager.runs_root).get(job["run_id"])
            if control is None or control["status"] != "running" or any(
                item["delivery"] == "steer" and item["status"] in {"received", "delivered"} for item in control["messages"]
            ):
                raise TaskError("recovery_control_unverified", "运行控制或补充消息尚未确认，请核对原运行。")
            review = self.manager.projects.verify_recovery_workspace(identifier)
            workspace = manifest.task.workspace
            identity = directory_identity(workspace)
            if workspace != Path(review.worktree) or identity != control["workspace_identity"]:
                raise TaskError("recovery_workspace_changed", "工作树身份已变化，请核对原运行。")
            selected = next((item for item in self.manager.catalog.project_presets() if item.id == job["project_id"]), None)
            if (selected is None or selected.path.resolve() != Path(job["source_path"])
                    or source_policy(selected) != job["source_policy"]
                    or self.queue.scope(job) != job["resources"]):
                raise TaskError("recovery_source_changed", "项目登记、解释器、测试设置或资源归属已变化。")
            self.manager.check_model_versions(job["model_versions"])
            current = self.manager.run_config
            if (manifest.config.permission_policy != current.permission_policy or manifest.config.planning_policy != current.planning_policy
                    or manifest.config.system_prompt != current.system_prompt
                    or manifest.config.hooks != current.hooks or current.hooks.rules
                    or current.hooks.project_enabled and (workspace / ".agents" / "hooks.toml").exists()
                    or manifest.task.verifier != self.manager.project_verifier(selected, workspace)):
                raise TaskError("recovery_policy_changed", "权限、Hook 或验收配置需要重新核对。")
            if manifest.task.python_execution != prepare_python_execution(
                workspace, selected.python_paths, python_executable=selected.python_executable
            ):
                raise TaskError("recovery_python_changed", "当前 Python 解释器或源码范围已变化。")
            checkpoint = inspect_crash(history, self.manager.runs_root, datetime.fromisoformat(job["recovery_observed_at"]), identity, clean=clean)
            revision = payload_digest({"run_id": job["run_id"], "last_seq": history.events[-1].seq,
                                       "checkpoint": checkpoint.model_dump(mode="json"), "base": review.base_commit,
                                       "messages": history_messages(history, allow_interrupted=True, recovered=checkpoint.tool_results)})
            return checkpoint, revision
        except TaskError:
            raise
        except (ProjectError, SessionError, FileToolError, ConversationError, WebServiceError) as error:
            raise TaskError(error.code, error.message) from None
        except (StorageError, ValueError, KeyError, TypeError, OSError):
            raise TaskError("recovery_evidence_unreadable", "中断记录或工作区证据无法核对。") from None

    def assess(self, job: dict) -> None:
        """首次观察中断时冻结时间上界，把可继续或待核对原因保存到调用者持有的 job。"""
        job.setdefault("recovery_observed_at", now())
        try:
            checkpoint, revision = self.checkpoint(job)
        except TaskError as error:
            job["recovery"] = {"state": "blocked", "reason": error.message}
            return
        history = RunStore.read(self.manager.runs_root, job["run_id"])
        limits = history.manifest.config.limits
        job["recovery"] = {"state": "ready", "revision": revision,
                           "reason": "已核对原工具批次，可从剩余预算继续。",
                           "recovered_writes": sum(result["ok"] for result in checkpoint.tool_results.values()),
                           "remaining_model_turns": limits.max_model_turns - checkpoint.spent_model_turns,
                           "remaining_tool_calls": limits.max_tool_calls - checkpoint.spent_tool_calls,
                           "remaining_seconds": limits.run_timeout_seconds - checkpoint.spent_seconds,
                           "usage_complete": checkpoint.spent_usage_complete}

    def accept(self, identifier: str, request: RecoveryRequest) -> dict:
        """核对 identifier 的恢复版本并接纳一个后继作业。原资源租约保留至队列认领。"""
        queue = self.queue
        if queue.closed or self.manager._closing:
            raise TaskError("service_closing", "服务正在关闭。")
        source = queue.get(identifier)
        request_id = str(request.request_id)
        digest = payload_digest({"source": identifier, "revision": request.expected_revision})
        with queue.store.transaction() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=?", (request_id,)).fetchone()
            if row is not None:
                existing = json.loads(row[0])
                if existing.get("digest") != digest or existing.get("recovery_from") != identifier:
                    raise TaskError("request_conflict", "请求编号已用于其他内容。")
                return queue.public(existing, db)
        summary = source.get("recovery") or {}
        if source["status"] != "needs_review" or summary.get("state") != "ready" or summary.get("revision") != request.expected_revision:
            raise TaskError("recovery_stale", "恢复状态已变化，请刷新作业。")
        try:
            _, revision = self.checkpoint(source)
            if revision != request.expected_revision:
                raise TaskError("recovery_stale", "恢复证据已变化，请刷新作业。")
        except TaskError:
            updated = dict(source)
            self.assess(updated)
            with queue.store.transaction() as db:
                current = db.execute("SELECT body FROM jobs WHERE id=?", (identifier,)).fetchone()
                if current is not None and json.loads(current[0]) == source:
                    queue.save(updated, db)
            raise
        followup = FollowupRequest(request_id=request.request_id, prompt="继续中断的运行。请依据已核对的历史和当前工作树完成原任务。")
        job = {key: source[key] for key in ("session_id", "project_id", "source_path", "workspace_identity",
                                           "model_versions", "memory_policy", "project_label", "source_policy", "resources")}
        job.update(id=request_id, status="queued", run_id=str(uuid4()), title=source["title"], created_at=now(),
                   recovery_from=identifier, recovery_revision=revision, digest=digest, error=None,
                   validation_status="unverified", execution_status=None,
                   payload={"request_id": request_id, "parent_run_id": source["run_id"], "followup": followup.model_dump(mode="json")})
        with queue.store.transaction() as db:
            retry = db.execute("SELECT body FROM jobs WHERE id=?", (request_id,)).fetchone()
            if retry is not None:
                existing = json.loads(retry[0])
                if existing.get("digest") != digest or existing.get("recovery_from") != identifier:
                    raise TaskError("request_conflict", "请求编号已用于其他内容。")
                return queue.public(existing, db)
            current = json.loads(db.execute("SELECT body FROM jobs WHERE id=?", (identifier,)).fetchone()[0])
            claim = db.execute("SELECT body FROM execution_claims WHERE id=?", (identifier,)).fetchone()
            if current != source or claim is None or json.loads(claim[0])["status"] != "unknown":
                raise TaskError("recovery_stale", "中断资源或恢复状态已变化，请刷新作业。")
            self.manager.management.require_active(source["session_id"], db)
            count = db.execute("SELECT count(*) FROM jobs WHERE json_extract(body,'$.status') IN ('queued','preparing','running')").fetchone()[0]
            if count >= 100:
                raise TaskError("queue_full", "后台队列最多接纳一百个未结束请求。")
            source["recovery"] = {**summary, "state": "queued", "next_job_id": request_id, "next_run_id": job["run_id"]}
            queue.save(source, db)
            queue.save(job, db)
        queue.kick()
        return queue.public(job)
