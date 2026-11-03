"""持久工作项领域：事务、依赖、执行证据与宿主工具，不承担模型执行循环。"""

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import uuid4

from pydantic import Field

from agent_harness.schemas import DataModel
from agent_harness.storage.runs import StorageError, RunStore


def encode(value) -> str:
    """将可序列化 value 转为稳定 JSON 字符串；不访问文件，非法值抛 ValueError。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def now() -> str:
    """返回 UTC ISO 时间字符串，供任务事件与作业排序使用。"""
    return datetime.now(UTC).isoformat()


class TaskError(Exception):
    """任务业务错误；消息可供页面和模型展示。"""

    def __init__(self, code: str, message: str):
        """保存稳定 code 和中文 message；返回 None，不包含原始异常。"""
        self.code = code  # 稳定原因码。
        self.message = message  # 用户可读说明。
        super().__init__(message)


class TaskContent(DataModel):
    """模型可填写的工作项内容；执行操作编号由宿主生成，依赖只允许同清单编号。"""
    subject: str = Field(min_length=1, max_length=200)  # 简洁工作标题。
    description: str = Field(default="", max_length=8000)  # 具体范围；空串表示标题已足够。
    acceptance: list[str] = Field(default_factory=list, max_length=30)  # 可核对的完成条件。
    parent_task_id: str | None = None  # None 表示顶层；父任务只作汇总。
    depends_on: list[str] = Field(default_factory=list, max_length=100)  # 前置任务稳定编号。


class TaskCreate(TaskContent):
    """网页或宿主提交的完整创建请求；request_id 在当前清单内去重。"""

    request_id: str = Field(min_length=1, max_length=100)  # 网页保持稳定；模型工具由宿主按事件生成。


class TaskGet(DataModel):
    """按稳定编号读取工作项；编号只在宿主绑定的会话内解析。"""

    task_id: str = Field(min_length=1, max_length=100)  # 待查看工作项。


class TaskList(DataModel):
    """任务列表分页参数；状态为空时包含全部，最多返回五十项。"""

    status: Literal["pending", "in_progress", "completed", "cancelled"] | None = None  # None 不筛选。
    offset: int = Field(default=0, ge=0)  # 从零开始的列表位置。
    limit: int = Field(default=20, ge=1, le=50)  # 单页数量，默认二十项。


class TaskEvidence(DataModel):
    """宿主工具回执的证据引用，或用户明确人工确认；模型不得伪造人工来源。"""

    run_id: str | None = Field(default=None, max_length=100, description="工具回执evidence_ref.run_id，必须为当前Run；人工确认省略。")
    tool_call_id: str | None = Field(default=None, max_length=200, description="成功工具回执evidence_ref.tool_call_id，原样复制，不能用命令作业编号代替。")
    kind: Literal["manual_confirmation"] | None = Field(default=None, description="仅网页用户人工确认时使用；模型不得设置。")


class TaskEdit(DataModel):
    """模型可填写的变更；None 保持原值，expected_revision 拒绝过期覆盖。"""
    task_id: str = Field(min_length=1, max_length=100)  # 当前清单内的目标编号。
    expected_revision: int = Field(ge=1, description="使用最新任务概览或task_get的revision；冲突后重读，不能重复使用旧版本。")
    status: Literal["pending", "in_progress", "completed", "cancelled"] | None = None  # pending 可重新打开。
    subject: str | None = Field(default=None, min_length=1, max_length=200)  # None 保持标题。
    description: str | None = Field(default=None, max_length=8000)  # 空串明确清空说明。
    acceptance: list[str] | None = Field(default=None, max_length=30)  # None 保持完成条件。
    depends_on: list[str] | None = Field(default=None, max_length=100)  # [] 清空依赖。
    completion_summary: str | None = Field(default=None, max_length=4000)  # 完成时必须非空。
    evidence_refs: list[TaskEvidence] | None = Field(default=None, max_length=30, description="completed时复制本轮成功工具回执的evidence_ref：[{run_id,tool_call_id}]；不得编造编号。None保持旧值。")


class TaskUpdate(TaskEdit):
    """网页或宿主提交的完整更新请求；操作编号与正文一起固定，不由模型选择。"""

    request_id: str = Field(min_length=1, max_length=100)  # 同变更重试保持；不同变更使用新编号。


TASK_TOOLS = {
    "task_create": (TaskContent, "创建当前会话工作项，填写范围和完成条件。操作编号由宿主生成；不增加执行权限或预算。"),
    "task_get": (TaskGet, "读取当前会话工作项完整条件、依赖、版本和证据。"),
    "task_list": (TaskList, "分页查看当前会话任务清单；blocked_reason表示尚不可开工。"),
    "task_update": (TaskEdit, "按expected_revision更新工作项。in_progress认领当前Run，completed须有完成说明和真实tool_call_id证据。操作编号由宿主生成；重新打开用pending，不能伪造测试通过。"),
}  # 固定控制工具；不由网页工具选择授予其他会话权限。


class WorkStore:
    """任务及队列的 SQLite 事务仓储；运行工具和产物事实仍以 RunStore 为准。"""

    def __init__(self, runs_root: Path):
        """绑定受保护的 runs_root；构造不写盘，首次事务初始化数据库。"""
        self.root = runs_root.resolve()  # 当前数据目录，也是模型禁止访问的记录根。
        self.path = self.root / ".work" / "work.sqlite3"  # 新增任务与作业的统一事实源。

    @contextmanager
    def transaction(self):
        """返回独占写事务连接；成功提交，异常回滚，路径替换或磁盘错误抛 StorageError。"""
        connection = None
        try:
            if self.path.parent.resolve() != self.path.parent or self.path.resolve() != self.path:
                raise StorageError("任务记录路径发生变化。")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS lists(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, digest TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS permission_actions(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS integrations(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS integration_anchors(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversation_operations(id TEXT PRIMARY KEY, session TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL);
            """)
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except (sqlite3.Error, OSError):
            raise StorageError("任务或队列记录无法保存，请检查数据目录。") from None
        finally:
            if connection is not None:
                connection.close()  # 未提交的连接关闭会回滚，不发布半份任务图。

    def snapshot(self, session: str) -> dict:
        """返回 session 的最新任务副本；尚无任务返回空清单，不启动模型。"""
        if not self.path.exists():
            return {"session_id": session, "revision": 0, "items": []}
        with self.transaction() as db:
            row = db.execute("SELECT body FROM lists WHERE id=?", (session,)).fetchone()
            state = json.loads(row[0]) if row else {"session_id": session, "revision": 0, "items": []}
        return self._view(state)

    def _view(self, state: dict) -> dict:
        """为 state 副本计算父任务进度和依赖阻塞；返回可展示快照，不写事实。"""
        state = json.loads(encode(state))
        items = {item["id"]: item for item in state["items"]}

        def status(identifier: str) -> str:
            """递归计算 identifier 的汇总状态；任务图已在写入事务内校验无环。"""
            item = items[identifier]
            children = [child for child in items.values() if child["parent_task_id"] == identifier]
            if children and item["status"] != "cancelled":
                values = [status(child["id"]) for child in children]
                item["status"] = "completed" if all(value == "completed" for value in values) else "in_progress" if "in_progress" in values else "pending"
            return item["status"]

        for identifier in items:
            status(identifier)
        valid = {}  # 完成事实仍保留；计算在当前依赖图下是否可以作为下游前置。

        def satisfied(identifier: str) -> bool:
            """返回 identifier 的完成条件是否仍有效；待复核子项不能通过父项间接放行。"""
            if identifier not in valid:
                item = items[identifier]
                children = [child['id'] for child in items.values() if child['parent_task_id'] == identifier]
                valid[identifier] = (item['status'] == 'completed' and not item.get('review_required') and not item.get('hold_reason')
                                     and all(satisfied(key) for key in [*children, *item['depends_on']]))
            return valid[identifier]

        for identifier in items:
            satisfied(identifier)
        for item in items.values():
            unfinished = [items[key]["display_id"] for key in item["depends_on"] if not valid[key]]
            item["blocked_reason"] = ("前置任务未完成或待复核：" + ", ".join(map(str, unfinished))) if unfinished else item.get("hold_reason")
            if item.get("review_required"):
                item["blocked_reason"] = "上游或完成条件变化，请重新打开并核对。"
            parent = item["parent_task_id"]
            while parent is not None:
                ancestor = items[parent]
                if ancestor["status"] == "cancelled" or ancestor.get("hold_reason") or any(not valid[key] for key in ancestor["depends_on"]):
                    item["blocked_reason"] = "父任务已取消、受阻或前置条件未满足。"
                    break
                parent = ancestor["parent_task_id"]
            item["is_group"] = any(child["parent_task_id"] == item["id"] for child in items.values())
            if item['is_group'] and item['status'] == 'completed' and not valid[item['id']]:
                item['status'], item['blocked_reason'] = 'pending', '子任务或其前置条件需要重新核对。'
        return state

    def _check_graph(self, items: list[dict]) -> None:
        """核对 items 的依赖和汇总关系；未知编号、循环或过深图抛 TaskError。"""
        graph = {item["id"]: list(item["depends_on"]) for item in items}
        for item in items:
            parent = item["parent_task_id"]
            if parent is not None:
                if parent not in graph:
                    raise TaskError("task_dependency_missing", "父任务不存在或不属于本清单。")
                graph[parent].append(item["id"])
        visited, active = set(), set()

        def visit(identifier: str, depth: int = 0) -> None:
            """深度优先核对 identifier；depth 防止恶意任务图耗尽调用栈，返回 None。"""
            if identifier not in graph:
                raise TaskError("task_dependency_missing", "前置任务不存在或不属于本清单。")
            if identifier in active or depth > 100:
                raise TaskError("task_dependency_cycle", "任务依赖或父子关系存在循环，或超过一百层。")
            if identifier in visited:
                return
            active.add(identifier)
            for target in graph[identifier]:
                visit(target, depth + 1)
            active.remove(identifier)
            visited.add(identifier)

        for identifier in graph:
            visit(identifier)

    def mutate(self, session: str, operation: str, payload: dict, change) -> dict:
        """在同一事务对 session 执行 change(state)；operation 幂等，返回固定回执。

        payload 用于核对重复请求内容；change 只能更新内存状态并返回 JSON 结果。
        回调异常时整笔回滚；图校验、清单版本和变更事件与回执一起提交。
        """
        key, digest = session + ":" + operation, hashlib.sha256(encode(payload).encode()).hexdigest()
        with self.transaction() as db:
            self.require_active(db, session)
            previous = db.execute("SELECT digest,result FROM operations WHERE id=?", (key,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise TaskError("request_conflict", "请求编号已用于其他内容。")
                return json.loads(previous[1])
            row = db.execute("SELECT body FROM lists WHERE id=?", (session,)).fetchone()
            state = json.loads(row[0]) if row else {"session_id": session, "revision": 0, "items": []}
            before = {item["id"]: encode(item) for item in state["items"]}  # 用差量事件保留完整变更事实。
            result = change(state)
            self._check_graph(state["items"])
            state["revision"] += 1
            db.execute("INSERT OR REPLACE INTO lists VALUES (?,?)", (session, encode(state)))
            db.execute("INSERT INTO operations VALUES (?,?,?)", (key, digest, encode(result)))
            self.event(db, session, {"type": "tasks_updated", "revision": state["revision"], "operation": operation,
                                    "items": [item for item in state["items"] if before.get(item["id"]) != encode(item)]})
            return result

    @staticmethod
    def require_active(db, session: str) -> None:
        """在调用者db事务核对session允许写入；归档、回收站及清理状态抛TaskError，返回None。"""
        row = db.execute("SELECT body FROM conversations WHERE id=?", (session,)).fetchone()
        if row and json.loads(row[0])["state"] != "active":
            raise TaskError("conversation_inactive", "请先恢复会话，再继续执行或修改任务。")

    def event(self, db, scope: str, body: dict) -> None:
        """在调用者 db 事务记录 scope 的 body 事件；补 UTC 时间，不单独提交。"""
        db.execute("INSERT INTO events(scope,body) VALUES (?,?)", (scope, encode({**body, "at": now()})))

    def events(self, after: int, scope: str | None = None) -> list[dict]:
        """返回 after 之后至多一百条事件；scope=None 读取全部，不触发执行。"""
        with self.transaction() as db:
            rows = db.execute("SELECT seq,scope,body FROM events WHERE seq>? AND (? IS NULL OR scope=?) ORDER BY seq LIMIT 100", (after, scope, scope)).fetchall()
            results = []
            for row in rows:
                body = json.loads(row[2])
                owner = row[1]
                job = db.execute("SELECT body FROM jobs WHERE id=?", (owner,)).fetchone()
                if job:
                    owner = json.loads(job[0]).get("session_id") or owner
                metadata = db.execute("SELECT body FROM conversations WHERE id=?", (owner,)).fetchone()
                if metadata and json.loads(metadata[0])["state"] in {"trashed", "purging", "purged"}:
                    body = {"type": "conversation_updated", "session_id": owner, "at": body.get("at")}
                results.append({"seq": row[0], "scope": row[1], **body})
        return results

    def latest_seq(self) -> int:
        """返回当前已提交事件末游标；未建立仓储返回零，不启动作业。"""
        if not self.path.exists():
            return 0
        with self.transaction() as db:
            return db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]


class TaskService:
    """绑定单个会话与宿主执行者，统一网页和模型的工作项规则。"""

    def __init__(self, store: WorkStore, session: str, run_id: str | None = None):
        """绑定 store/session；run_id=None 为人工操作，有值时只接受关联工具证据。"""
        self.store = store  # 事务仓储。
        self.session = session  # 不从模型参数获取的会话边界。
        self.run_id = run_id  # 当前可信执行归属；None 表示用户直接管理。

    def create(self, arguments: TaskCreate, *, source: dict | None = None) -> dict:
        """创建 arguments 指定工作项并返回编号/版本；source 仅供宿主导入方案，默认无来源。"""
        def change(state):
            """向 state 加入待处理工作项；超限或空白标题抛 TaskError，返回创建回执。"""
            if len(state["items"]) >= 500 or not arguments.subject.strip() or any(not value.strip() or len(value) > 2000 for value in arguments.acceptance):
                raise TaskError("task_invalid", "任务标题和完成条件必须有效，清单最多五百项。")
            item = {"id": str(uuid4()), "display_id": len(state["items"]) + 1, "subject": arguments.subject.strip(),
                    "description": arguments.description, "acceptance": arguments.acceptance, "parent_task_id": arguments.parent_task_id,
                    "depends_on": list(dict.fromkeys(arguments.depends_on)), "status": "pending", "owner": None,
                    "active_run_id": None, "revision": 1, "completion_summary": None, "evidence_refs": [],
                    "source": source, "hold_reason": None, "review_required": False, "run_ids": [], "created_at": now(), "updated_at": now()}
            state["items"].append(item)
            return {"id": item["id"], "revision": 1}
        return self.store.mutate(self.session, "create:" + arguments.request_id, arguments.model_dump() | {"source": source}, change)

    def get(self, identifier: str) -> dict:
        """读取 identifier 的最新完整工作项；不存在或跨清单访问抛 TaskError。"""
        item = next((item for item in self.store.snapshot(self.session)["items"] if item["id"] == identifier), None)
        if item is None:
            raise TaskError("task_not_found", "工作项不存在或不属于当前清单。")
        return item

    def _evidence(self, references: list[dict]) -> None:
        """核验 references 指向当前 Run 的成功工具结果；人工确认使用独立类型，不制造检查通过。"""
        if not references:
            raise TaskError("task_evidence_required", "完成任务需要可核对的证据。")
        if self.run_id is None:
            if any(reference != {"kind": "manual_confirmation"} for reference in references):
                raise TaskError("task_invalid_evidence", "人工完成请明确记录人工确认。")
            return
        history = RunStore.read(self.store.root, self.run_id)
        valid = {event.payload.get("call_id") for event in history.events if event.type == "tool_result" and event.payload.get("result", {}).get("ok") and event.payload.get("name") not in {*TASK_TOOLS, "memory_propose"}}
        for reference in references:
            if set(reference) != {"run_id", "tool_call_id"} or reference["run_id"] != self.run_id or reference["tool_call_id"] not in valid:
                raise TaskError("task_invalid_evidence", "完成证据必须指向本轮已保存的成功工具结果。")

    def update(self, arguments: TaskUpdate) -> dict:
        """按 arguments 修改任务并返回新版本；拒绝过期、越权认领和缺少证据的完成。"""
        def change(state):
            """在 state 内核验并更新目标；同时使受影响下游待复核，返回精简回执。"""
            if (arguments.subject is not None and not arguments.subject.strip()) or (arguments.acceptance is not None and any(not value.strip() or len(value) > 2000 for value in arguments.acceptance)):
                raise TaskError("task_invalid", "任务标题和完成条件必须是有效非空文本。")
            item = next((item for item in state["items"] if item["id"] == arguments.task_id), None)
            if item is None:
                raise TaskError("task_not_found", "工作项不存在。")
            if item["revision"] != arguments.expected_revision:
                raise TaskError("task_revision_conflict", "任务已更新，请重新读取后操作。")
            structural = any(getattr(arguments, field) is not None for field in ("subject", "description", "acceptance", "depends_on"))
            if structural and (item["source"] or item["status"] in {"in_progress", "completed"}):
                raise TaskError("task_scope_locked", "批准步骤或进行中/已完成任务不能直接改写范围，请重新规划或先重新打开。")
            view = next(value for value in self.store._view(state)["items"] if value["id"] == item["id"])
            target = arguments.status
            if item['status'] == 'completed' and target == 'in_progress':
                raise TaskError('task_reopen_required', '已完成项须先重新打开，才能重新认领并使下游待复核。')
            if item["status"] == "cancelled" and target in {"in_progress", "completed"}:
                raise TaskError("task_user_cancelled", "请先由用户重新打开已取消工作项。")
            if target == "pending" and item["status"] == "cancelled" and self.run_id is not None:
                raise TaskError("task_user_cancelled", "已取消工作项只能由用户重新打开。")
            if target in {"in_progress", "completed"}:
                if view["is_group"] or view["blocked_reason"]:
                    raise TaskError("task_blocked", view["blocked_reason"] or "父任务进度由子任务汇总。")
                if self.run_id is not None and item["active_run_id"] not in {None, self.run_id}:
                    raise TaskError("task_owned", "任务由其他运行持有。")
                if target == "in_progress" and self.run_id is None:
                    raise TaskError("task_run_required", "请发送继续请求，由 Agent 开始执行。")
                if target == "completed":
                    if self.run_id is not None and item["active_run_id"] != self.run_id:
                        raise TaskError("task_not_claimed", "请先将任务标为进行中。")
                    if not arguments.completion_summary or not arguments.completion_summary.strip():
                        raise TaskError("task_summary_required", "完成任务需要说明交付结果。")
                    self._evidence([reference.model_dump(exclude_none=True) for reference in arguments.evidence_refs or []])
            if target == "pending" or structural:
                changed = {item["id"]}
                for _ in state["items"]:
                    for downstream in state["items"]:
                        if downstream["id"] not in changed and any(key in changed for key in downstream["depends_on"]):
                            changed.add(downstream["id"])
                            downstream["review_required"] = True
                            downstream["revision"] += 1
                item["review_required"], item["hold_reason"] = False, None
            for field in ("subject", "description", "acceptance", "depends_on", "completion_summary", "evidence_refs"):
                value = getattr(arguments, field)
                if field == "evidence_refs" and value is not None:
                    value = [reference.model_dump(exclude_none=True) for reference in value]
                if value is not None:
                    item[field] = value
            if target is not None:
                item["status"] = target
                item["active_run_id"] = self.run_id if target == "in_progress" else None
                item["owner"] = "main:" + self.session if target == "in_progress" else None
                if self.run_id and self.run_id not in item["run_ids"]:
                    item["run_ids"].append(self.run_id)
            item["revision"] += 1
            item["updated_at"] = now()
            return {"id": item["id"], "revision": item["revision"], "status": item["status"]}
        return self.store.mutate(self.session, "update:" + arguments.request_id, arguments.model_dump(), change)

    def import_plan(self, plan) -> None:
        """幂等导入已批准 plan 的步骤；顺序依赖，来源固定；返回 None，不调用模型。"""
        previous = None
        for step in plan.steps:
            result = self.create(TaskCreate(request_id=f"plan:{plan.version}:{step.id}", subject=step.text[:200], description=step.text,
                                           acceptance=plan.verification, depends_on=[previous] if previous else []),
                                 source={"plan_version": plan.version, "step_id": step.id})
            previous = result["id"]

    def release(self, reason: str) -> None:
        """释放本 run_id 尚未完成项并记录 reason；保留完成项，重复收尾幂等，返回 None。"""
        def change(state):
            """将 state 中本轮持有项恢复待处理，返回释放数量。"""
            count = 0
            for item in state["items"]:
                if item["active_run_id"] == self.run_id:
                    item.update(status="pending", active_run_id=None, owner=None, hold_reason=reason, revision=item["revision"] + 1)
                    count += 1
            return {"released": count}
        self.store.mutate(self.session, "release:" + str(self.run_id), {"reason": reason}, change)


class TaskController:
    """每轮任务工具入口；当前状态来自仓储，运行快照随 Runner 固定。"""

    def __init__(self, store: WorkStore, manifest, planning):
        """从可信 manifest 绑定会话/运行及 planning；导入批准步骤，返回 None。"""
        self.service = TaskService(store, str(manifest.session_id or manifest.run_id), str(manifest.run_id))  # 可信边界。
        self.planning = planning  # 执行阶段及当前授权来源。
        def prepare(state):
            """明确启动新轮时清除旧轮持有及接续提示；过期计划任务保留并阻塞，返回 None。"""
            version = planning.state.plans[-1].version if planning.state.plans else None
            for item in state["items"]:
                if item["status"] == "in_progress":
                    item.update(status="pending", active_run_id=None, owner=None, revision=item["revision"] + 1)
                item["hold_reason"] = "来源方案已被替代，请重新规划。" if item["source"] and item["source"]["plan_version"] != version else None
            return None
        store.mutate(self.service.session, "start:" + str(manifest.run_id), {}, prepare)
        if planning.state.plans and planning.state.plans[-1].status == "approved":
            self.service.import_plan(planning.state.plans[-1])

    def execute(self, name: str, arguments) -> dict:
        """执行已校验 name/arguments；返回任务结果，业务失败抛 TaskError。"""
        if name == "task_create":
            return self.service.create(TaskCreate(**arguments.model_dump(), request_id=self.operation_id(name)))
        if name == "task_update":
            return self.service.update(TaskUpdate(**arguments.model_dump(), request_id=self.operation_id(name)))
        if name == "task_get":
            return self.service.get(arguments.task_id)
        state = self.service.store.snapshot(self.service.session)
        items = [item for item in state["items"] if arguments.status is None or item["status"] == arguments.status]
        return {**state, "items": items[arguments.offset:arguments.offset + arguments.limit], "total": len(items)}

    def operation_id(self, name: str) -> str:
        """从当前 Run 最后一条同名 tool_started 生成稳定操作编号；无可信开始事件时拒绝。"""
        history = RunStore.read(self.service.store.root, self.service.run_id)
        event = history.events[-1]
        if event.type != 'tool_started' or event.payload.get('name') != name:
            raise TaskError('task_call_not_recorded', '任务写入必须先保存可信工具开始事件。')
        return self.service.run_id + ':' + str(event.seq)

    def snapshot(self) -> dict:
        """返回当前清单及规划进度投影；不授予权限、不调用模型。"""
        state = self.service.store.snapshot(self.service.session)
        for plan in self.planning.state.plans:
            for step in plan.steps:
                item = next((item for item in state["items"] if item["source"] == {"plan_version": plan.version, "step_id": step.id}), None)
                step.status = ("blocked" if item["blocked_reason"] else item["status"]) if item else "pending"
                if step.status == "cancelled":
                    step.status = "blocked"
        return state

    def instructions(self) -> str:
        """返回最新精简概览；所有字段仍计入输入预算，详情用 task_get 按需回读。"""
        state = self.snapshot()
        items = state["items"]
        def active_first(item: dict) -> bool:
            """将 item 的进行中状态排在前面，返回排序键；不改变任务调度顺序。"""
            return item["status"] != "in_progress"
        visible = sorted((item for item in items if item["status"] not in {"completed", "cancelled"}), key=active_first)
        summary = [{key: item[key] for key in ("id", "display_id", "subject", "status", "revision", "blocked_reason")} for item in visible[:20]]
        return ("\n[宿主任务清单]\n已批准步骤已自动导入，不重复创建等价任务；需要细化时指定parent_task_id创建子项。"
                "用task_update先认领in_progress，完成时填写说明并复制本轮成功工具回执的evidence_ref。"
                "操作编号由宿主生成，不填写request_id；expected_revision使用下方最新revision，冲突先task_get重读。"
                "依赖阻塞不能跳过；未完成项沿用本轮剩余预算，检查通过由宿主记录。\n"
                + encode({"current_run_id": self.service.run_id, "revision": state["revision"], "total": len(items), "completed": sum(item["status"] == "completed" and not item['blocked_reason'] for item in items), "items": summary}))


class ExecutionLease:
    """同一数据目录的进程级执行排他锁；进程崩溃后由操作系统释放。"""

    def __init__(self, root: Path):
        """绑定 root；初始未加锁，不创建文件。"""
        self.root = root.resolve()  # 受保护记录根目录。
        self.handle = None  # 持锁期间打开的文件句柄。

    def acquire(self) -> None:
        """非阻塞申请执行锁；冲突抛 TaskError，成功返回 None，须调用 release。"""
        path = self.root / ".execution.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        if path.resolve() != path:
            raise StorageError("执行锁路径发生变化。")
        self.handle = path.open("a+b")
        self.handle.seek(0)
        try:
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise TaskError("execution_busy", "同一数据目录已有执行者，请等待收尾。") from None

    def release(self) -> None:
        """释放操作系统锁并关闭句柄；未持锁时无操作，返回 None。"""
        if self.handle is not None:
            self.handle.close()
            self.handle = None
