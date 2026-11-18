"""在宿主队列接纳和审阅项目记忆整理；来源只能由已登记项目解析。"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from agent_harness.memory.consolidation import MAX_SOURCE_RUNS, consolidate, consolidation_receipt, publish
from agent_harness.memory.evidence import EvidenceIndex, hidden_sessions
from agent_harness.memory.service import MemoryError, MemoryService, MemoryStore, digest, suppressed_evidence
from agent_harness.memory.topics import project_topics
from agent_harness.schemas import DataModel, RunConfig
from agent_harness.work.tasks import TaskError, encode, now


class ConsolidationRequest(DataModel):
    """接纳人工选择的历史运行；重复提交同一编号须使用相同项目和来源。"""

    request_id: UUID = Field(strict=False)  # 客户端幂等编号，同时作为整理作业编号。
    run_ids: list[Annotated[UUID, Field(strict=False)]] = Field(min_length=1, max_length=MAX_SOURCE_RUNS)  # 可回读的同项目 Run。


class ConsolidationReview(DataModel):
    """提交明确勾选的草案项；空选择表示拒绝整份草案。"""

    request_id: UUID = Field(strict=False)  # 与勾选内容绑定，网络重试使用同一编号。
    selected: list[int] = Field(max_length=12)  # 从零开始的草案位置，由宿主核对范围。


class ConsolidationSettings(DataModel):
    """项目级空闲整理开关；默认只接受人工发起。"""

    idle_enabled: bool  # True 时宿主空闲且有新历史才接纳整理。


class MemoryJobs:
    """协调整理作业、项目索引及审阅，正式条目继续通过 MemoryStore 提交。"""

    def __init__(self, manager):
        """绑定 manager 的队列与项目目录；构造不扫描历史或启动模型。"""
        self.manager = manager  # 宿主模型配置、会话事务和作业调度入口。
        self.work = manager.management.store  # 与会话生命周期共享短事务。
        self.index = EvidenceIndex(manager.runs_root)  # 可重建历史索引。
        self.stop_requested: set[str] = set()  # 已认领但尚未启动的停止请求。

    def scope(self, job: dict) -> dict:
        """为 job 计算单一项目记忆资源；目录身份变化抛 TaskError，不占用源码锁。"""
        service = MemoryService(MemoryStore(self.manager.runs_root), Path(job["source_path"]))
        try:
            scope = service.store.scope(None, service.source)
        except MemoryError as error:
            raise TaskError(error.code, error.message) from None
        if scope != job["memory_scope"]:
            raise TaskError("memory_scope_changed", "项目目录身份已变化，请重新整理。")
        return {"paths": [], "repositories": [], "groups": ["memory:" + scope], "identities": {}}

    def _service(self, project_id: str) -> MemoryService:
        """按宿主登记 project_id 解析领域服务；未知项目拒绝。"""
        from agent_harness.web.memory import project_memory_service

        return project_memory_service(self.manager, project_id)

    def _job(self, project_id: str, identifier: str, service: MemoryService, db=None) -> dict:
        """读取项目内的整理 job；db 可为当前生命周期事务，越界或缺失抛 MemoryError。"""
        if db is None:
            try:
                job = self.manager.jobs.get(identifier)
            except TaskError:
                raise MemoryError("memory_not_found", "找不到此整理作业。") from None
        else:
            row = db.execute("SELECT body FROM jobs WHERE id=?", (identifier,)).fetchone()
            job = json.loads(row[0]) if row else {}
        if job.get("kind") != "memory_consolidation" or job.get("project_id") != project_id or job.get("memory_scope") != service.store.scope(None, service.source):
            raise MemoryError("memory_not_found", "当前项目没有此整理作业。")
        return job

    def _public(self, job: dict, service: MemoryService, *, detail: bool = False) -> dict:
        """返回 job 的可展示状态；detail=True 时包含差异，已隐藏来源不返回草案正文。"""
        result = job.get("memory_draft") or {}
        snapshot = result.get("snapshot") or {}
        receipt = consolidation_receipt(service, job["id"]) if result.get("state") == "draft" else result.get("receipt")
        hidden = hidden_sessions(service.store.root) if snapshot.get("inputs") else set()
        unavailable = any(ref["session_id"] in hidden for ref in snapshot.get("inputs", []))
        state = receipt["state"] if receipt else "source_unavailable" if unavailable else result.get("state", job["status"])
        value = {key: job.get(key) for key in ("id", "status", "created_at", "finished_at", "trigger")}
        value.update(state=state, error=job.get("error") or result.get("error"), error_code=job.get("error_code") or result.get("error_code"),
                     change_count=len(result.get("changes", [])) if not unavailable else 0, usage=result.get("usage", {}),
                     usage_complete=result.get("usage_complete", True), model_calls=result.get("model_calls", 0),
                     http_attempts=result.get("http_attempts", 0), duration_seconds=result.get("duration_seconds", 0),
                     input_bytes=snapshot.get("input_bytes", 0), response_model=result.get("response_model"), receipt=receipt)
        if detail:
            base = service.store.read(service.source, sequence=snapshot["base_sequence"]) if snapshot and result.get("changes") and not unavailable else {"items": []}
            targets = {item["id"]: item for item in base["items"]}
            value.update(inputs=[] if unavailable else snapshot.get("inputs", []),
                         changes=[] if unavailable else [{**change, "before": [targets[target["id"]] for target in change["targets"] if target["id"] in targets]} for change in result.get("changes", [])],
                         base_sequence=snapshot.get("base_sequence"))
        return value

    def library(self, project_id: str) -> dict:
        """返回 project_id 的主题、可选历史、最近整理及空闲设置；无模型或写入副作用。"""
        service = self._service(project_id)
        state = service.list(status="active")
        scope = state["scope_id"]
        idle = False
        rows = []
        if self.work.path.exists():
            with self.work.transaction() as db:
                idle = db.execute("SELECT 1 FROM scheduler_settings WHERE id=?", ("memory:" + scope,)).fetchone() is not None
                rows = db.execute("SELECT body FROM jobs WHERE json_extract(body,'$.kind')='memory_consolidation' AND json_extract(body,'$.memory_scope')=? ORDER BY rowid DESC LIMIT 20", (scope,)).fetchall()
        return {"scope_id": scope, "sequence": state["sequence"], "topics": project_topics(state["items"])[:40],
                "sources": self.index.runs(scope, self.index.cutoff(scope)), "idle_enabled": idle,
                "consolidations": [self._public(json.loads(row[0]), service) for row in rows]}

    def detail(self, project_id: str, identifier: str) -> dict:
        """读取 project_id 内 identifier 的草案、前值、证据和计量；不发起后台整理。"""
        service = self._service(project_id)
        return self._public(self._job(project_id, identifier, service), service, detail=True)

    def evidence(self, project_id: str, reference: str) -> dict:
        """从项目索引回读 reference 并核对可见性、撤回和原始指纹。"""
        service = self._service(project_id)
        state = service.store.read(service.source)
        scope = state["scope_id"]
        return self.index.get(scope, self.index.cutoff(scope), reference, suppressed=suppressed_evidence(state["items"]))

    def configure(self, project_id: str, idle: bool) -> dict:
        """显式切换 project_id 的空闲整理；启用后由队列在无前台作业时探测。"""
        service = self._service(project_id)
        scope = service.store.scope(None, service.source)
        with self.work.transaction() as db:
            key = "memory:" + scope
            if idle:
                db.execute("INSERT OR REPLACE INTO scheduler_settings VALUES(?,?)", (key, encode({"project_id": project_id, "scope": scope})))
            else:
                db.execute("DELETE FROM scheduler_settings WHERE id=?", (key,))
        self.manager.jobs.kick()
        return {"idle_enabled": idle}

    def _enqueue(self, project_id: str, service: MemoryService, identifier: str, run_ids: list[str], *,
                 trigger: str, cutoff: int) -> dict:
        """持久接纳同项目 run_ids；trigger 标记人工或空闲，cutoff 是此次触发的索引上界。"""
        if self.manager._closing or self.manager.jobs.closed:
            raise MemoryError("memory_job_unavailable", "宿主正在关闭，无法接纳整理。")
        scope = service.store.scope(None, service.source)
        fingerprint = digest([scope, sorted(run_ids), trigger])
        with self.work.transaction() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=?", (identifier,)).fetchone()
            if row:
                existing = json.loads(row[0])
                if existing.get("digest") != fingerprint:
                    raise MemoryError("memory_request_conflict", "整理请求编号已用于其他来源。")
                return self.manager.jobs.public(existing, db)
            available = {row["run_id"] for row in self.index.runs(scope, cutoff)}
            if len(run_ids) != len(set(run_ids)) or not set(run_ids) <= available:
                raise MemoryError("memory_source_unavailable", "部分所选运行不在当前项目的可用历史中。")
            config = self.manager.run_config.freeze_models()
            config.limits.max_model_turns = min(config.limits.max_model_turns, 4)
            config.limits.max_http_attempts_per_turn = 1  # 整理重试由用户显式发起并单独计量。
            project = next((item for item in self.manager.catalog.recent() if item["id"] == project_id), None)
            job = {"id": identifier, "kind": "memory_consolidation", "title": "整理项目记忆", "digest": fingerprint,
                   "run_id": identifier, "session_id": None, "project_id": project_id,
                   "project_label": project["label"] if project else service.source.name,
                   "source_path": str(service.source), "memory_scope": scope, "trigger": trigger,
                   "memory_run_ids": sorted(run_ids), "memory_cutoff": cutoff, "memory_config": config.model_dump(mode="json"),
                   "model_versions": config.model_versions(), "status": "queued", "created_at": now(),
                   "execution_status": None, "validation_status": "unverified", "error": None}
            job["resources"] = self.scope(job)
            db.execute("INSERT INTO jobs VALUES(?,?)", (identifier, encode(job)))
            public = self.manager.jobs.public(job, db)
            self.work.event(db, identifier, {"type": "job_updated", "job": public})
        self.manager.jobs.kick()
        return public

    async def enqueue(self, project_id: str, body: ConsolidationRequest) -> dict:
        """刷新可补录证据后持久接纳人工 body；返回排队回执，不等待模型完成。"""
        service = self._service(project_id)
        scope = service.store.scope(None, service.source)
        await asyncio.to_thread(self.index.refresh, scope)
        return self._enqueue(project_id, service, str(body.request_id), [str(run) for run in body.run_ids],
                             trigger="manual", cutoff=self.index.cutoff(scope))

    def idle(self) -> bool:
        """仅在队列空闲时接纳一个新历史批次；没有开启的项目或新输入返回 False。"""
        if not self.work.path.exists() or not self.index.path.exists():
            return False
        with self.work.transaction() as db:
            settings = [json.loads(row[0]) for row in db.execute("SELECT body FROM scheduler_settings WHERE id LIKE 'memory:%' ORDER BY id")]
            last = {row[0]: row[1] for row in db.execute("SELECT json_extract(body,'$.memory_scope'),MAX(json_extract(body,'$.memory_cutoff')) FROM jobs WHERE json_extract(body,'$.kind')='memory_consolidation' GROUP BY 1")}
        for setting in settings:
            scope, project = setting["scope"], setting["project_id"]
            try:
                service = self._service(project)
                cutoff = self.index.cutoff(scope)
                if service.store.scope(None, service.source) != scope or cutoff <= (last.get(scope) or 0):
                    continue
                if self.manager.run_config.requires_model_key and not self.manager.api_key(self.manager.run_config) and self.manager._model_client is None:
                    continue
                runs = [row["run_id"] for row in self.index.runs(scope, cutoff)[:4]]
                if runs:
                    identifier = str(uuid5(NAMESPACE_URL, "harness:memory:" + scope + ":" + str(cutoff)))
                    self._enqueue(project, service, identifier, runs, trigger="idle", cutoff=cutoff)
                    return True
            except MemoryError:
                continue
        return False

    def reconcile(self, job: dict) -> None:
        """把无活跃宿主的 job 固定为中断，保留检查点和计量；不重发模型请求。"""
        result = job.get("memory_draft") or {}
        if result.get("state") in {"draft", "no_changes"}:
            job.update(status="completed", execution_status="completed", finished_at=now())
            return
        if result.get("state") in {"failed", "cancelled"}:
            job.update(status=result["state"], execution_status=result["state"], finished_at=now(),
                       error=result.get("error"), error_code=result.get("error_code"))
            return
        if result:
            result.update(state="interrupted", changes=[], usage_complete=not bool(result.get("model_calls")))
        job.update(status="failed", execution_status="failed", error_code="memory_interrupted",
                   error="整理因宿主中断而停止，可以重新选择历史发起整理。", finished_at=now())

    async def run(self, job: dict) -> None:
        """执行已认领的整理 job；检查模型与项目身份，原子保存草案和用量。"""
        if job["id"] in self.stop_requested:
            job.update(status="cancelled", execution_status="cancelled", finished_at=now())
            self.manager.jobs.save(job)
            return
        self.manager.check_model_versions(job["model_versions"])
        self.scope(job)
        service = self._service(job["project_id"])
        if service.store.scope(None, service.source) != job["memory_scope"]:
            raise MemoryError("memory_scope_changed", "排队期间项目登记已变更。")
        config = RunConfig.model_validate(job["memory_config"])
        job.update(status="running", execution_status="running")
        self.manager.jobs.save(job)

        def progress(result: dict) -> None:
            """把 result 的当前检查点落入 job；公开队列只展示进度和计量摘要。"""
            job["memory_draft"] = deepcopy(result)
            self.manager.jobs.save(job)

        result = await consolidate(service, job["memory_run_ids"], config, self.manager.api_key(config),
                                   client=self.manager._model_client, transport=self.manager._transport, progress=progress)
        status = "completed" if result["state"] in {"draft", "no_changes"} else "cancelled" if result["state"] == "cancelled" else "failed"
        job.update(status=status, execution_status=status, finished_at=now(), error=result["error"], error_code=result["error_code"])
        self.manager.jobs.save(job)

    def stop(self, job: dict) -> asyncio.Task | None:
        """请求停止正在认领或发送请求的 job，返回可等待的句柄；未启动时由执行入口收尾。"""
        task = self.manager.jobs.executing.get(job["id"])
        if task is not None and not task.done():
            self.stop_requested.add(job["id"])
            if job["status"] == "running":
                task.cancel()
        return task

    async def review(self, project_id: str, identifier: str, body: ConsolidationReview) -> dict:
        """锁定会话生命周期后确认草案；请求重试使用已提交事务，返回最新可展示结果。"""
        service = self._service(project_id)
        async with self.manager._lock:
            with self.work.transaction() as db:
                job = self._job(project_id, identifier, service, db)
                draft = job.get("memory_draft") or {}
                if job["status"] != "completed" or draft.get("state") != "draft":
                    raise MemoryError("memory_draft_unavailable", "此整理作业没有可审阅草案。")
                receipt = publish(service, identifier, draft, body.selected, str(body.request_id), lifecycle_db=db)
                job["memory_review_state"] = receipt["state"]
                db.execute("UPDATE jobs SET body=? WHERE id=?", (encode(job), identifier))
                self.work.event(db, identifier, {"type": "memory_consolidation_reviewed", "id": identifier})
        return self._public(job, service, detail=True)

    def purge(self, session: str) -> None:
        """清除 session 的索引和未发布草案正文；正式记忆仍保留独立确认及不可用来源。"""
        self.index.purge(session)
        if not self.work.path.exists():
            return
        with self.work.transaction() as db:
            rows = db.execute("SELECT body FROM jobs WHERE json_extract(body,'$.kind')='memory_consolidation'").fetchall()
            for row in rows:
                job = json.loads(row[0])
                draft = job.get("memory_draft") or {}
                if any(ref["session_id"] == session for ref in (draft.get("snapshot") or {}).get("inputs", [])):
                    service = MemoryService(MemoryStore(self.manager.runs_root), Path(job["source_path"]))
                    try:
                        receipt = consolidation_receipt(service, job["id"]) if draft.get("state") == "draft" else None
                    except MemoryError:
                        receipt = None  # 项目目录已移走也应允许清理会话的派生正文。
                    draft.update(state=receipt["state"] if receipt else job.get("memory_review_state", "source_unavailable"),
                                 changes=[], snapshot=None)
                    if receipt:
                        draft["receipt"] = receipt
                    db.execute("UPDATE jobs SET body=? WHERE id=?", (encode(job), job["id"]))
