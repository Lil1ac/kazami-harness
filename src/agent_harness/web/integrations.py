"""成果采纳的人工作业入口；复用唯一队列、权限事实、进程验收和持久事件。"""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
import threading
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from agent_harness.permissions.store import PermissionStore
from agent_harness.projects.integration_files import IntegrationFiles, fingerprint, git, identity
from agent_harness.projects.integrations import IntegrationStore
from agent_harness.runtime.verifier import Verifier
from agent_harness.execution.python import prepare_python_execution, resolve_python_executable
from agent_harness.schemas import DataModel, VerifierSpec
from agent_harness.storage.sessions import payload_digest
from agent_harness.work.tasks import TaskError, encode, now


logger = logging.getLogger(__name__)  # 非预期内部异常仅写宿主日志，不把底层详情回显页面。


class IntegrationCreate(DataModel):
    """用户明确选择的来源目标；路径由宿主登记项解析，不接受网页绝对路径。"""

    request_id: UUID = Field(strict=False)  # 同一创建请求的稳定幂等编号。
    target_id: str = Field(min_length=1, max_length=64)  # 已登记的同仓库Git工作区。


class IntegrationAction(DataModel):
    """版本化人工操作；批准仅针对fingerprint绑定的具体写回或检查。"""

    request_id: UUID = Field(strict=False)  # 网络重试沿用，不得改换参数。
    expected_revision: int = Field(ge=1)  # 页面实际审阅的集成版本。
    action: str = Field(pattern="^(check_candidate|apply|check_target|undo|reconcile|abandon)$")  # 宿主提供的固定动作集合。
    fingerprint: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")  # apply必须绑定最终差异指纹。
    confirm_effect: bool = False  # 对账时先仅核对；True明确确认当前全部预期内容已落地。


class ConflictResolution(DataModel):
    """一个冲突路径的明确选择；edit内容可为空字符串，delete显式删除。"""

    path: str = Field(min_length=1, max_length=4096)  # 当前冲突清单中的相对路径。
    choice: str = Field(pattern="^(target|source|edit|delete)$")  # 选择目标、成果、文本编辑或删除。
    content: str | None = Field(default=None, max_length=512 * 1024)  # 只有edit必须提供，None不能冒充空文件。

    @model_validator(mode="after")
    def validate_content(self):
        """核对edit所需正文并返回自身；非edit携带正文或edit缺正文时抛ValueError。"""
        if (self.choice == "edit") != (self.content is not None):
            raise ValueError("仅编辑冲突时需要正文")
        return self


class IntegrationResolutions(DataModel):
    """一次候选冲突解决；页面断开后同请求可读取原结果。"""

    request_id: UUID = Field(strict=False)  # 同一次解决操作的稳定编号。
    expected_revision: int = Field(ge=1)  # 提交时仍须匹配的候选版本。
    resolutions: list[ConflictResolution] = Field(min_length=1, max_length=2000)  # 本次明确解决的路径，不隐式接受其他冲突。


class IntegrationService:
    """绑定当前宿主与队列，为采纳作业保存明确批准，后台执行不再调用模型。"""

    def __init__(self, manager):
        """绑定manager的现有项目、权限及持久队列；构造不启动任何作业。"""
        self.manager = manager  # 当前Web/桌面共用宿主。
        self.store = IntegrationStore(manager.projects)  # 固定成果及写回协议。
        self.cancel_event = threading.Event()  # 当前同步准备/写回在文件边界响应停止。
        self.check_task: asyncio.Task | None = None  # 当前真实验收协程，取消后等待进程树清理。
        self.active_job: str | None = None  # 当前持有唯一队列槽的集成作业。

    def source(self, run_id: str):
        """核对run_id是已完整收尾的工作树运行；返回视图，等待/取消/未知清理不得采纳。"""
        view = self.manager.get_run(UUID(run_id))
        if (view.execution_mode != "isolated" or view.active or view.interrupted
                or view.result.execution_status != "completed" or view.result.cleanup_status not in {"complete", "not_needed"}
                or view.project is None or view.project.snapshot_status != "ready"):
            raise TaskError("integration_source_unavailable", "请先完成工作树运行和代码快照，再采纳成果。")
        self.manager.management.require_active(str(view.session_id or view.run_id))
        return view

    def targets(self, run_id: str) -> list[dict]:
        """返回run_id同仓库、区别于来源的已登记项目；无效目录省略，不扫描磁盘或切分支。"""
        view = self.source(run_id)
        owned = self.manager.projects._ownership(UUID(run_id))
        result = []
        for preset in self.manager.catalog.project_presets():
            try:
                common = Path(git(preset.path, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve()
                if str(common) != owned["common_git_dir"] or str(preset.path) == str(view.workspace):
                    continue
                identity(preset.path)
                result.append({"id": preset.id, "label": preset.label, "path": str(preset.path), "repository": str(common),
                               "head": git(preset.path, "rev-parse", "HEAD").decode().strip(),
                               "branch": git(preset.path, "symbolic-ref", "-q", "HEAD", allowed=(0, 1)).decode().strip() or None})
            except (OSError, TaskError):
                continue
        return result

    def preset(self, target_id: str):
        """解析target_id的当前登记配置；目录或设置不再可用时拒绝，不接受客户端补全。"""
        preset = next((item for item in self.manager.catalog.project_presets() if item.id == target_id), None)
        if preset is None:
            raise TaskError("integration_target_invalid", "目标工作区未登记或已不可用，请重新打开。")
        return preset

    async def create(self, run_id: str, body: IntegrationCreate) -> dict:
        """为明确来源和目标登记准备作业并返回记录；重试复用原记录，不重新物化来源。"""
        view = self.source(run_id)
        policy = self.manager.run_config.permission_policy
        if policy.read != "allow" or not any(Path(item) == Path(".") for item in policy.read_roots):
            raise TaskError("permission_denied", "集成准备需要读取完整项目，当前宿主读取范围不允许。")
        if body.target_id not in {item["id"] for item in await asyncio.to_thread(self.targets, run_id)}:
            raise TaskError("integration_target_invalid", "目标必须是同一仓库内已登记的其他工作区。")
        async with self.manager._lock:
            with self.store.work.transaction() as db:
                existing = db.execute("SELECT 1 FROM integrations WHERE id=?", (str(body.request_id),)).fetchone()
                active = db.execute("SELECT 1 FROM jobs WHERE json_extract(body,'$.session_id')=? AND json_extract(body,'$.status') IN ('queued','preparing','running','needs_review') LIMIT 1", (str(view.session_id or view.run_id),)).fetchone()
            if active and not existing:
                raise TaskError("integration_job_pending", "会话已有未完成作业，请先完成或核对。")
            value = self.store.create(run_id, str(view.session_id or view.run_id), body.target_id, str(body.request_id))
            if "source_test" not in value:
                value["source_test"] = {**(view.result.verification.model_dump(mode="json") if view.result.verification else {"status": "unverified"}), "run_id": run_id, "applicable": False}
                self.store.save(value)
            await self.enqueue(value, "prepare", str(body.request_id), {"run_id": run_id, "target_id": body.target_id})
        return self.store.public(self.store.get(value["id"]))

    async def action(self, identifier: str, body: IntegrationAction) -> dict:
        """接纳版本化body人工决定；批准与入队同事务，重试只返回原归属，不自动重放。"""
        async with self.manager._lock:
            payload = {**body.model_dump(mode="json"), "integration_id": identifier}
            old = self._existing_job(str(body.request_id), payload_digest(payload))
            if old:
                return self.store.public(self.store.get(identifier))
            value = self.store.expect(identifier, body.expected_revision)
            self.manager.management.require_active(value["session_id"])
            if body.action in {"abandon", "reconcile"} and any(job.get("integration_id") == identifier and job["status"] == "needs_review" and job.get("recovery_kind") == "process" for job in self.manager.jobs.list()):
                raise TaskError("integration_process_unconfirmed", "请先到后台工作确认检查进程已停止；该阶段没有待重放的文件写回。")
            if body.action == "abandon":
                if any(job.get("integration_id") == identifier and job["status"] in {"queued", "running", "preparing"} for job in self.manager.jobs.list()):
                    raise TaskError("integration_busy", "请先停止后台作业，再放弃原操作。")
                if value["status"] not in {"ready", "conflicts", "failed", "needs_review", "stale"}:
                    raise TaskError("integration_busy", "请先停止活动作业，再放弃未完成集成。")
                value.update(status="abandoned", revision=value["revision"] + 1,
                             error="用户核对并保留当前现场，未完成的原动作不重放，增量锚点未推进。")
                self.store.save(value)
                self.resolve_jobs(value)
                return self.store.public(value)
            if body.action == "reconcile":
                if value["status"] != "needs_review":
                    raise TaskError("integration_not_uncertain", "当前记录不需要写回对账。")
                value = await asyncio.to_thread(self.store.reconcile, identifier, body.confirm_effect)
                if value["status"] == "applied":
                    self.resolve_jobs(value)
                return self.store.public(value)
            if body.action in {"check_candidate", "apply"} and value["status"] != "ready":
                raise TaskError("integration_busy", "请先解决全部冲突并准备好当前候选。")
            if body.action in {"check_target", "undo"} and value["status"] != "applied":
                raise TaskError("integration_not_applied", "只有已采纳记录可复验目标或准备撤销。")
            if body.action == "reconcile" and value["status"] != "needs_review":
                raise TaskError("integration_not_uncertain", "当前记录不需要写回对账。")
            if body.action == "apply" and body.fingerprint != value["fingerprint"]:
                raise TaskError("integration_stale", "请重新审阅最终文件差异后确认。")
            if body.action == "apply":
                self.store.authorize(value, self.manager.run_config.permission_policy)
            if body.action.startswith("check_") and self.manager.run_config.permission_policy.verification == "deny":
                raise TaskError("permission_denied", "宿主禁止运行项目检查。")
            await self.enqueue(value, body.action, str(body.request_id), payload)
        return self.store.public(self.store.get(identifier))

    async def resolve(self, identifier: str, body: IntegrationResolutions) -> dict:
        """幂等保存body的冲突选择；同编号重试不再执行，后台活动时禁止改变候选。"""
        digest = payload_digest({"integration_id": identifier, **body.model_dump(mode="json")})
        key = "integration:" + identifier + ":" + str(body.request_id)
        async with self.manager._lock:
            with self.store.work.transaction() as db:
                old = db.execute("SELECT digest FROM operations WHERE id=?", (key,)).fetchone()
            if old:
                if old[0] != digest:
                    raise TaskError("request_conflict", "冲突解决请求编号已用于不同内容。")
                return self.store.public(self.store.get(identifier))
            value = self.store.get(identifier)
            self.manager.management.require_active(value["session_id"])
            if any(job.get("integration_id") == identifier and job["status"] in {"queued", "running", "preparing", "needs_review"} for job in self.manager.jobs.list()):
                raise TaskError("integration_busy", "请等待当前集成作业结束后再修改候选。")
            value = await asyncio.to_thread(self.store.resolve, identifier, body.expected_revision, [item.model_dump() for item in body.resolutions], str(body.request_id))
            with self.store.work.transaction() as db:
                db.execute("INSERT INTO operations VALUES (?,?,?)", (key, digest, encode({"revision": value["revision"]})))
            return self.store.public(value)

    def resolve_jobs(self, value: dict) -> None:
        """按已完成对账或明确放弃的value解除旧作业阻塞；不恢复队列，不重放旧许可。"""
        for job in self.manager.jobs.list():
            if job.get("integration_id") == value["id"] and job["status"] == "needs_review":
                job.update(status="completed" if value["status"] == "applied" else "failed", error="用户已核对写回现场，原作业不重放。")
                self.manager.jobs.save(job)
                if job.get("action_id"):
                    permissions = PermissionStore(self.manager.runs_root)
                    action = permissions.get(job["action_id"])
                    action.update(status="executed" if value["status"] == "applied" else "failed")
                    permissions.save(action)

    def _existing_job(self, request_id: str, digest: str) -> dict | None:
        """查询request_id是否已登记相同digest；内容变更抛冲突，没有则返回None。"""
        with self.store.work.transaction() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=?", (request_id,)).fetchone()
        if row is None:
            return None
        import json
        value = json.loads(row[0])
        if value["digest"] != digest:
            raise TaskError("request_conflict", "操作编号已用于其他确认内容。")
        return value

    async def enqueue(self, value: dict, operation: str, request_id: str, payload: dict) -> None:
        """把具体operation加入现有单槽队列；写回和测试的本次许可与作业同事务保存。"""
        if self.manager._closing or self.manager.jobs.closed:
            raise TaskError("service_closing", "服务正在关闭。")
        digest = payload_digest(payload)
        if self._existing_job(request_id, digest):
            return
        policy = self.manager.run_config.permission_policy.model_dump()
        job = {"id": request_id, "kind": "integration", "integration_id": value["id"], "operation": operation,
               "status": "queued", "run_id": value["run_id"], "session_id": value["session_id"],
               "title": {"prepare": "准备成果集成", "apply": "采纳成果", "check_candidate": "检查集成候选", "check_target": "复验目标代码", "undo": "准备撤销采纳", "reconcile": "核对写回结果"}[operation],
               "project_id": value["target_id"], "project_label": self.preset(value["target_id"]).label,
               "source_path": str(self.preset(value["target_id"]).path), "created_at": now(), "payload": payload,
               "digest": digest, "permission_policy": policy, "error": None, "validation_status": "unverified", "execution_status": None}
        with self.store.work.transaction() as db:
            self.manager.management.require_active(value["session_id"], db)
            active = db.execute("SELECT 1 FROM jobs WHERE json_extract(body,'$.session_id')=? AND json_extract(body,'$.status') IN ('queued','preparing','running','needs_review') LIMIT 1", (value["session_id"],)).fetchone()
            if active:
                raise TaskError("integration_job_pending", "会话已有未结束作业，请先完成或核对。")
            if operation in {"apply", "check_candidate", "check_target", "reconcile"}:
                created = datetime.now(UTC).timestamp()
                action = {"id": request_id, "owner_kind": "integration", "owner_id": value["id"],
                          "session_id": value["session_id"], "source_run_id": value["run_id"], "name": "integration_" + operation,
                          "call_id": request_id, "arguments": payload, "inputs": {"fingerprint": value.get("fingerprint"), "test_config": value.get("test_config")},
                          "policy_fingerprint": payload_digest(policy), "workspace": job["source_path"], "created_at": created,
                          "expires_at": created + policy["approval_ttl_seconds"], "status": "approved", "decision": "approve", "request_id": request_id,
                          "execution_run_id": None}
                action["fingerprint"] = payload_digest(action)
                PermissionStore(self.manager.runs_root).save(action, db)
                job["action_id"] = request_id
            db.execute("INSERT INTO jobs VALUES (?,?)", (request_id, encode(job)))
            self.store.work.event(db, value["session_id"], {"type": "job_updated", "job": self.manager.jobs.public(job, db)})
        self.manager.jobs.kick()

    async def run_job(self, job: dict) -> None:
        """执行已出队job并持久保存真实结果；中断写回需核对，准备失败不会伪造目标修改。"""
        self.active_job = job["id"]
        self.cancel_event = threading.Event()
        job.update(status="running", execution_status="running")
        self.manager.jobs.save(job)
        permissions = PermissionStore(self.manager.runs_root)
        try:
            if job["permission_policy"] != self.manager.run_config.permission_policy.model_dump():
                raise TaskError("permission_stale", "排队期间宿主权限改变，请重新确认。")
            if job.get("action_id"):
                permissions.claim(job["action_id"], job["id"])
            value = self.store.get(job["integration_id"])
            preset = self.preset(value["target_id"])
            operation = job["operation"]
            if operation != "prepare":
                self.store.expect(value["id"], job["payload"]["expected_revision"])
                current_config = {"argv": preset.test_argv, "timeout_seconds": preset.test_timeout_seconds,
                                  "python_executable": preset.python_executable, "python_paths": preset.python_paths}
                if (operation != "undo" and current_config != value["test_config"]) or str(preset.path) != value["target"]["path"]:
                    raise TaskError("integration_target_changed", "排队期间项目登记或测试配置已变化，请重新准备。")
            if operation == "prepare":
                await asyncio.to_thread(self.store.prepare, value["id"], preset, self.cancel_event)
            elif operation.startswith("check_"):
                self.check_task = asyncio.create_task(self.check(value, operation.removeprefix("check_")))
                await self.check_task
            elif operation == "apply":
                await asyncio.to_thread(self.store.apply, value["id"], job["payload"]["expected_revision"], job["payload"]["fingerprint"], self.manager.run_config.permission_policy, self.cancel_event)
            elif operation == "undo":
                created = await asyncio.to_thread(self.store.undo, value["id"], job["id"], preset, self.cancel_event)
                job["result_integration_id"] = created["id"]
            elif operation == "reconcile":
                await asyncio.to_thread(self.store.reconcile, value["id"], True)
            job.update(status="completed", execution_status="completed", finished_at=now())
            latest = self.store.get(job.get("result_integration_id") or value["id"])
            if latest["checks"]:
                job["validation_status"] = latest["checks"][-1]["result"]["status"]
        except (Exception, asyncio.CancelledError) as error:
            if not isinstance(error, (TaskError, asyncio.CancelledError)):
                logger.exception("成果集成内部异常，作业=%s", job["id"])
            value = self.store.get(job["integration_id"])
            uncertain = value["status"] in {"applying", "needs_review"}
            cancelled = isinstance(error, asyncio.CancelledError) or getattr(error, "code", None) == "integration_cancelled"
            if not uncertain and job["operation"] in {"prepare", "apply"}:
                value.update(status="cancelled" if cancelled else "stale" if getattr(error, "code", None) in {"integration_target_changed", "integration_stale"} else "failed",
                             error=getattr(error, "message", "集成操作未完成，请核对记录。"))
                self.store.save(value)
            job.update(status="needs_review" if uncertain else "cancelled" if cancelled else "failed", execution_status="cancelled" if cancelled else "failed",
                       error=getattr(error, "message", "操作中断，请核对现场。"), finished_at=now())
            if uncertain:
                job["recovery_kind"] = "files" if job["operation"] == "apply" else "process"
                self.manager.jobs.paused = True
        finally:
            if job.get("action_id"):
                action = permissions.get(job["action_id"])
                action.update(status="unknown" if job["status"] == "needs_review" else "executed" if job["status"] == "completed" else "cancelled" if job["status"] == "cancelled" else "failed")
                permissions.save(action)
            self.manager.jobs.save(job)
            self.active_job, self.check_task = None, None

    async def check(self, value: dict, scope: str) -> None:
        """在候选或实际目标运行固定项目检查；保存前后源码指纹，代码变化使通过结论不可适用。"""
        config = value["test_config"]
        if not config["argv"]:
            manifest = await asyncio.to_thread(self.code_manifest, value, scope)
            value["checks"].append({"scope": scope, "code_fingerprint": fingerprint(manifest), "applicable": manifest == value["candidate"],
                                    "result": {"status": "unverified", "reason": "not_configured", "stdout": "", "stderr": ""}, "created_at": now()})
            self.store.save(value)
            return
        files = IntegrationFiles(self.store.directory(value["id"]))
        workspace = Path(value["workspace"] if scope == "candidate" else value["target"]["path"])
        if scope == "candidate":
            await asyncio.to_thread(self.store.assert_target, value)
            for argument in config["argv"][1:]:
                candidate = Path(argument.split("=", 1)[-1])
                if candidate.is_absolute() and candidate.is_relative_to(Path(value["target"]["path"])):
                    raise TaskError("integration_test_path_invalid", "候选检查命令引用了目标绝对路径；请改用项目相对路径并重新准备。")
        before = await asyncio.to_thread(self.code_manifest, value, scope)
        expected = value["candidate"]
        if scope == "candidate" and before != expected:
            raise TaskError("integration_candidate_changed", "集成目录已被外部修改，请重新准备，不会运行未审阅代码。")
        if scope == "target":
            current = await asyncio.to_thread(self.store.target, workspace, files)
            if current["manifest"] != expected:
                raise TaskError("integration_target_changed", "目标已在采纳后发生变化，不能用原成果名义复验。")
        executable = resolve_python_executable(Path(value["target"]["path"]), config["python_executable"])
        execution = prepare_python_execution(workspace, config["python_paths"], python_executable=str(executable) if executable else None)
        spec = VerifierSpec(kind="project_tests", argv=config["argv"], cwd=workspace, timeout_seconds=config["timeout_seconds"])
        evidence_id = str(uuid4())  # 原始输出按检查独立归属，旧轮不会指向新日志。
        verifier = Verifier(self.manager.run_config.limits, self.store.directory(value["id"]) / ("check-" + evidence_id))
        result = None
        try:
            result = await verifier.run(spec, workspace, UUID(value["id"]), python_execution=execution)
        finally:
            result = result or verifier.last_result
            if result is not None:
                after = await asyncio.to_thread(self.code_manifest, value, scope)
                stable = before == after
                if scope == "target":
                    state_after = await asyncio.to_thread(self.store.target, workspace, files)
                    stable = stable and all(current[key] == state_after[key] for key in ("index", "identity", "repository_identity", "branch", "head"))
                evidence = {"scope": scope, "evidence_id": evidence_id, "code_fingerprint": fingerprint(before), "after_fingerprint": fingerprint(after),
                            "applicable": stable and result.cleanup_status in {"not_needed", "complete"}, "result": result.model_dump(mode="json"), "created_at": now(),
                            "argv": spec.argv, "cwd": str(workspace), "python_execution": execution.model_dump(mode="json")}
                value = self.store.get(value["id"])
                value["checks"].append(evidence)
                self.store.save(value)
                if result.cleanup_status not in {"not_needed", "complete"}:
                    value.update(status="needs_review", error="检查进程清理尚未确认，请核对后再继续操作。")
                    self.store.save(value)
                    raise TaskError("integration_cleanup_unknown", "检查进程清理尚未确认。")

    def code_manifest(self, value: dict, scope: str) -> dict:
        """返回value候选或目标的当前完整源码清单；纳入新增非忽略文件，缓存不冒充代码变化。"""
        files = IntegrationFiles(self.store.directory(value["id"]))
        if scope == "target":
            return self.store.target(Path(value["target"]["path"]), files)["manifest"]
        workspace = Path(value["workspace"])
        names = git(workspace, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
        return files.scan(workspace, sorted(set(value["candidate"]) | {name.decode("utf-8") for name in names if name}))

    def cancel(self, job_id: str) -> None:
        """停止当前job_id；同步写回只在安全文件边界停止，检查协程取消后执行真实进程清理。"""
        if self.active_job == job_id:
            self.cancel_event.set()
            if self.check_task is not None:
                self.check_task.cancel()

    def reconcile_job(self, job: dict) -> None:
        """持执行锁核对失联job；仅写回采用文件回执，检查和准备不能借旧采纳终态冒充完成。

        原地更新作业及许可事实，不重放进程；非写回仍需用户确认进程已停止后解除阻塞。
        """
        value = self.store.get(job["integration_id"])
        writing = job["operation"] == "apply" and value["status"] in {"applying", "needs_review", "applied"}
        if writing and value["status"] in {"applying", "needs_review"}:
            value = self.store.reconcile(value["id"])
        complete = writing and value["status"] == "applied"
        job.update(status="completed" if complete else "needs_review", recovery_kind="files" if writing else "process",
                   error=None if complete else "成果作业上次执行中断，请核对进程及相关现场；原请求不重放。")
        if value["status"] == "preparing":
            value.update(status="failed", error="准备进程曾中断，目标未写回；核对后台作业后可重新准备。")
            self.store.save(value)
        if job.get("action_id"):
            permissions = PermissionStore(self.manager.runs_root)
            action = permissions.get(job["action_id"])
            action.update(status="executed" if complete else "unknown")
            permissions.save(action)

    def resolve_interruption(self, job: dict) -> None:
        """用户确认job进程停止后解除非写回阻塞；写回结果必须从逐文件页面对账。

        保留原采纳和检查历史，仅将清理未知的候选恢复为可重新检查状态，旧许可不重放。
        """
        value = self.store.get(job["integration_id"])
        if job["operation"] == "apply" and value["status"] in {"applying", "needs_review"}:
            raise TaskError("integration_needs_review", "请先在成果采纳页面核对逐文件写回结果。")
        if job["operation"].startswith("check_") and value["status"] == "needs_review":
            value.update(status="applied" if value.get("applied_at") else "ready", revision=value["revision"] + 1,
                         error="用户已确认检查进程停止；请重新执行检查，旧清理未知的结论不可用。")
            self.store.save(value)
        if job.get("action_id"):
            permissions = PermissionStore(self.manager.runs_root)
            action = permissions.get(job["action_id"])
            action.update(status="failed")
            permissions.save(action)

    def has_orphans(self) -> bool:
        """返回是否存在准备中却没有作业的集成；不创建数据库，不自行恢复执行。"""
        if not self.store.work.path.exists():
            return False
        with self.store.work.transaction() as db:
            return db.execute("SELECT 1 FROM integrations i WHERE json_extract(i.body,'$.status')='preparing' AND NOT EXISTS (SELECT 1 FROM jobs j WHERE json_extract(j.body,'$.integration_id')=i.id) LIMIT 1").fetchone() is not None

    def reconcile_orphans(self) -> None:
        """持执行锁启动时标记未入队的准备记录失败；不物化代码或重放任何动作。"""
        with self.store.work.transaction() as db:
            import json
            values = [json.loads(row[0]) for row in db.execute("SELECT body FROM integrations i WHERE json_extract(i.body,'$.status')='preparing' AND NOT EXISTS (SELECT 1 FROM jobs j WHERE json_extract(j.body,'$.integration_id')=i.id)")]
            for value in values:
                value.update(status="failed", error="准备请求未完整入队，目标未写回；请重新准备。")
                self.store.save(value, db)
