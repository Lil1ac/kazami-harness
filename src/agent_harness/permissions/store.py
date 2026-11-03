"""复用工作数据库事务保存不可变动作、幂等决定和单次执行归属。"""

import json
from datetime import datetime, UTC

from agent_harness.permissions.models import ActionDecision
from agent_harness.work.tasks import WorkStore, TaskError, encode


def utc_seconds() -> float:
    """返回当前UTC时间戳秒数，无参数和副作用；审批到期校验统一通过此时钟。"""
    return datetime.now(UTC).timestamp()


class PermissionStore:
    """审批事实仓储；实际执行结果仍由 RunStore 记录，通过动作编号核对。"""

    def __init__(self, root):
        """绑定记录根 root；构造不写盘，事务由 WorkStore 管理。"""
        self.work = WorkStore(root)  # 与队列共用数据库，批准和入队可以在一次事务提交。

    def get(self, action_id: str, db=None) -> dict:
        """返回 action_id 的记录副本；db=None 时独立读取事务，不存在抛 TaskError。"""
        if db is None:
            with self.work.transaction() as connection:
                return self.get(action_id, connection)
        row = db.execute("SELECT body FROM permission_actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise TaskError("permission_action_missing", "动作审批记录不存在或无法核对。")
        return json.loads(row[0])

    def waiting(self, history) -> dict | None:
        """返回可信终态 history 中仍等待处理的动作；已取消排队等终态不阻止普通新要求。"""
        snapshot = history.result.permissions.pending if history.result is not None else None
        if snapshot is None:
            return None
        value = self.get(snapshot["id"])
        return snapshot if value["status"] in {"pending", "approved", "executing", "unknown"} else None

    def save(self, value: dict, db=None) -> None:
        """保存 value 并发布工作事件；db=None 开独立事务，返回 None，不执行动作。"""
        if db is None:
            with self.work.transaction() as connection:
                self.save(value, connection)
            return
        db.execute("INSERT OR REPLACE INTO permission_actions VALUES (?,?)", (value["id"], encode(value)))
        self.work.event(db, value["session_id"], {"type": "permission_updated", "action": value})

    def list(self, session_id: str, *, limit: int = 50, offset: int = 0) -> dict:
        """分页读取 session_id 历史；limit 为1至100，offset非负，不触发调度或修改过期状态。"""
        with self.work.transaction() as db:
            rows = db.execute("SELECT body FROM permission_actions WHERE json_extract(body,'$.session_id')=? ORDER BY rowid DESC LIMIT ? OFFSET ?", (session_id, limit, offset)).fetchall()
            total = db.execute("SELECT count(*) FROM permission_actions WHERE json_extract(body,'$.session_id')=?", (session_id,)).fetchone()[0]
        items = [json.loads(row[0]) for row in rows]
        for item in items:
            if item["status"] in {"pending", "approved"} and utc_seconds() >= item["expires_at"]:
                item["status"] = "expired"  # 到期是派生阅读状态，GET不写盘或自动消费决定。
        return {"items": items, "total": total}

    def invalidate(self, action_id: str, reason: str) -> None:
        """将尚未启动 action_id 标为 reason 指定的失效；已认领动作保留不明状态，不推测未执行。"""
        with self.work.transaction() as db:
            value = self.get(action_id, db)
            if value["status"] == "approved":
                value.update(status="expired" if reason == "permission_expired" else "invalidated", error_code=reason)
                self.save(value, db)

    def reconcile(self) -> None:
        """宿主持执行锁启动时核对 executing 动作；只采用实际回执，否则标unknown，绝不重放。"""
        from agent_harness.storage.runs import RunStore, StorageError
        with self.work.transaction() as db:
            values = [json.loads(row[0]) for row in db.execute("SELECT body FROM permission_actions WHERE json_extract(body,'$.status')='executing'")]
            for value in values:
                if value.get("owner_kind") == "integration":
                    continue  # 集成动作由其逐文件日志核对，不能套用模型Run回执推测。
                status = "unknown"
                try:
                    history = RunStore.read(self.work.root, value["execution_run_id"])
                    if history.manifest.permission_resume is None or history.manifest.permission_resume.action_id != value["id"]:
                        raise StorageError("接续来源无法核对")
                    for event in history.events:
                        if value["name"] == "verification" and event.type == "verification_result":
                            status = "executed" if event.payload["verification"]["status"] == "passed" else "failed"
                        elif event.type == "tool_result" and event.payload.get("call_id") == "resume_" + value["id"]:
                            status = "executed" if event.payload["result"]["ok"] else "failed"
                except (StorageError, KeyError):
                    pass  # 日志缺失或不完整不能解释为动作未执行。
                value.update(status=status)
                self.save(value, db)

    def has_executing(self) -> bool:
        """返回是否存在尚无终态的已认领动作；数据目录不存在时返回False，不初始化数据库。"""
        if not self.work.path.exists():
            return False
        with self.work.transaction() as db:
            return db.execute("SELECT 1 FROM permission_actions WHERE json_extract(body,'$.status')='executing' LIMIT 1").fetchone() is not None

    def validate(self, decision: ActionDecision, parent_id: str, db=None, *, request_id: str | None = None) -> dict:
        """核对 decision 与 parent_id；request_id 非空时允许同一作业已接纳决定，返回原记录。

        仅校验不写状态；指纹、有效期、来源和决定冲突均抛 TaskError，不把过期当成拒绝执行成功。
        """
        value = self.get(decision.action_id, db)
        if value["source_run_id"] != parent_id or value["fingerprint"] != decision.fingerprint:
            raise TaskError("permission_stale", "动作或审阅版本已经变化，请刷新当前请求。")
        if value["status"] != "pending":
            if request_id is None or value.get("request_id") != request_id or value.get("decision") != decision.action:
                raise TaskError("permission_decided", "动作已经处理，不能再次批准或更改决定。")
        if decision.action == "approve" and utc_seconds() >= value["expires_at"]:
            raise TaskError("permission_expired", "动作审批已过期，请核对现场后重新发起任务。")
        return value

    def decide(self, decision: ActionDecision, parent_id: str, request_id: str, db) -> dict:
        """在现有 db 事务内保存决定与 request_id 消费归属，返回记录；调用方同事务插入作业。"""
        value = self.validate(decision, parent_id, db, request_id=request_id)
        value.update(status="approved" if decision.action == "approve" else "rejected" if decision.action == "reject" else "cancelled", decision=decision.action, request_id=request_id)
        self.save(value, db)
        return value

    def claim(self, action_id: str, run_id: str) -> dict:
        """将已批准 action_id 原子认领给 run_id，返回原动作；认领后中断只能核对、禁止再次执行。"""
        with self.work.transaction() as db:
            value = self.get(action_id, db)
            if value["status"] != "approved":
                raise TaskError("permission_consumed", "动作已开始或结果不明，不会重复执行。")
            if utc_seconds() >= value["expires_at"]:
                raise TaskError("permission_expired", "排队期间动作审批已过期，尚未执行。")
            value.update(status="executing", execution_run_id=run_id)
            self.save(value, db)
            return value
