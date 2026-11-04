"""团队持久事实与原子认领；共用C04事务库，不另建任务清单或自动重放执行。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from agent_harness.work.tasks import WorkStore, TaskError, encode, now


def task_fingerprint(task: dict) -> str:
    """固定task业务范围、完成条件及依赖，返回摘要；认领与释放产生的版本变化不使成果失效。"""
    value = {key: task.get(key) for key in ("subject", "description", "acceptance", "depends_on", "source")}
    return hashlib.sha256(encode(value).encode()).hexdigest()


class TeamStore:
    """一个会话对应一个团队；成员稳定身份、执行段、消息游标及固定成果分别保存。"""

    def __init__(self, root: Path):
        """绑定root下既有工作数据库，返回None；不启动模型、进程或创建工作树。"""
        self.work = WorkStore(root)  # 所有团队、任务及幂等决定共用事务事实源。

    def get(self, session: str, db=None) -> dict | None:
        """读取session团队副本；db=None开启查询事务，未创建返回None，不修改执行状态。"""
        if db is None:
            with self.work.transaction() as connection:
                return self.get(session, connection)
        row = db.execute("SELECT body FROM teams WHERE id=?", (session,)).fetchone()
        return json.loads(row[0]) if row else None

    def mutate(self, session: str, operation: str, payload: dict, change, expected_revision: int | None = None):
        """幂等执行session内change(state,db)，返回其固定回执；expected_revision=None用于宿主内部最新决定。

        operation为事件/网页请求编号；同编号不同payload拒绝，回调异常整笔回滚，不公布半份认领。
        """
        key = session + ":" + operation
        digest = hashlib.sha256(encode(payload).encode()).hexdigest()
        with self.work.transaction() as db:
            self.work.require_active(db, session)
            old = db.execute("SELECT digest,result FROM team_operations WHERE id=?", (key,)).fetchone()
            if old:
                if old[0] != digest:
                    raise TaskError("request_conflict", "相同团队请求编号不能更换内容。")
                return json.loads(old[1])
            state = self.get(session, db)
            if expected_revision is not None and (state or {}).get("revision", 0) != expected_revision:
                raise TaskError("team_revision_conflict", "团队已变化，请刷新后重新决定。")
            result = change(state, db)
            current = self.get(session, db) if state is None else state
            if current is not None:
                current["revision"] += 1
                current["updated_at"] = now()
                db.execute("INSERT OR REPLACE INTO teams VALUES (?,?)", (session, encode(current)))
                self.work.event(db, session, {"type": "team_updated", "team_id": current["id"], "revision": current["revision"]})
            db.execute("INSERT INTO team_operations VALUES (?,?,?)", (key, digest, encode(result)))
            return result

    def create(self, session: str, operation: str, title: str, workspace: str, identity: list[int], parent_id: str):
        """建立session唯一团队并返回编号；workspace/identity/parent_id由宿主固定，不由网页指定。"""
        def change(state, db):
            """在同一db登记团队初态，返回稳定编号；已有团队拒绝重复创建。"""
            if state is not None:
                raise TaskError("team_exists", "本会话已有团队，请读取现有团队继续。")
            state = {"id": str(uuid4()), "session_id": session, "title": title.strip(), "revision": 0,
                     "status": "active", "workspace": workspace, "workspace_identity": identity,
                     "parent_run_id": parent_id, "stage_run_id": str(uuid4()), "stage_ready": False,
                     "generation": 0, "members": [], "tasks": [], "messages": [], "created_at": now(),
                     "updated_at": now(), "error": None, "cleanup_status": "not_needed"}
            if not state["title"]:
                raise TaskError("team_invalid", "团队目标不能为空。")
            db.execute("INSERT INTO teams VALUES (?,?)", (session, encode(state)))
            return {"team_id": state["id"]}
        return self.mutate(session, operation, {"title": title, "workspace": workspace}, change)

    @staticmethod
    def require(state: dict | None) -> dict:
        """返回可执行state；缺团队或已停止时抛业务错误，不自动重建或唤醒。"""
        if state is None:
            raise TaskError("team_not_found", "请先创建团队。")
        if state["status"] != "active":
            raise TaskError("team_stopped", "团队已停止，历史仅供核对。")
        return state

    def add_member(self, session: str, operation: str, name: str, scope: list[str]):
        """为session添加name及scope授权的持久成员，返回编号；不启动请求，最多8个成员。"""
        def change(state, db):
            """校验state名称唯一和成员上限，持久保存固定文件授权；返回成员编号。"""
            self.require(state)
            if not name.strip() or len(state["members"]) >= 8 or any(m["name"] == name.strip() for m in state["members"]):
                raise TaskError("team_member_invalid", "成员名称为空、重复或已达到8人上限。")
            member = {"id": str(uuid4()), "name": name.strip(), "write_scope": scope, "status": "idle",
                      "active_run_id": None, "task_id": None, "runs": [], "summary": "", "message_cursor": 0,
                      "model_turns": 0, "tool_calls": 0, "total_tokens": 0, "usage_complete": True}
            state["members"].append(member)
            return {"member_id": member["id"]}
        return self.mutate(session, operation, {"name": name, "scope": scope}, change)

    def offer(self, session: str, operation: str, arguments: dict):
        """为既有任务登记arguments目标及授权，返回任务编号；失败重新分配须旧执行安全结束。"""
        def change(state, db):
            """从db的C04清单校验归属和条件，更新团队授权并使受影响的下游成果待复核。"""
            self.require(state)
            row = db.execute("SELECT body FROM lists WHERE id=?", (session,)).fetchone()
            tasks = json.loads(row[0])["items"] if row else []
            task = next((t for t in tasks if t["id"] == arguments["task_id"]), None)
            if task is None or not task["acceptance"] or task.get("review_required") or task["status"] in {"completed", "cancelled"} or any(t["parent_task_id"] == task["id"] for t in tasks):
                raise TaskError("team_task_invalid", "工作项不属于本会话、缺完成条件、已结束、待重新打开核对或是分组。")
            if arguments.get("member_id") and not any(m["id"] == arguments["member_id"] for m in state["members"]):
                raise TaskError("team_member_invalid", "成员不属于本团队。")
            previous = next((t for t in state["tasks"] if t["task_id"] == task["id"]), None)
            if previous and previous["state"] in {"running", "waiting_input", "needs_review"}:
                raise TaskError("team_task_owned", "请先停止旧执行并确认进程清理，再重新分配。")
            if not previous and len(state["tasks"]) >= 100:
                raise TaskError("team_task_limit", "团队工作项已达到100项上限。")
            item = {**arguments, "state": "pending", "fingerprint": task_fingerprint(task), "depends_on": task["depends_on"],
                    "run_id": None, "log_parent_id": None, "dependency_versions": {}, "generation": None, "error": None}
            state["tasks"] = [t for t in state["tasks"] if t["task_id"] != task["id"]] + [item]
            affected = {task["id"]}
            for _ in state["tasks"]:
                for downstream in state["tasks"]:
                    if any(key in affected for key in downstream["depends_on"]) and downstream["task_id"] not in affected:
                        if downstream["state"] in {"running", "waiting_input"}:
                            raise TaskError("team_dependency_in_use", "下游仍在执行，先收拢依赖使用者再修改上游。")
                        downstream.update(state="stale", error="依赖成果已修订，请重新分配核对。")
                        affected.add(downstream["task_id"])
            state["stage_ready"] = False
            return {"task_id": task["id"]}
        return self.mutate(session, operation, arguments, change)

    def message(self, session: str, operation: str, sender: str, recipient: str, content: str, *, request_meta: dict | None = None):
        """将content可靠写入session消息箱，返回编号；sender由宿主绑定，request_meta固定网页是否要求续轮，None用于内部消息；消息不提供权限。"""
        def change(state, db):
            """核验state中的收发身份并追加单条有界消息，返回已落盘编号。"""
            self.require(state)
            identities = {"lead", "user", *[m["id"] for m in state["members"]]}
            if sender not in identities or recipient not in identities - {"user"} or not content.strip():
                raise TaskError("team_message_invalid", "消息身份或内容无效。")
            if len(state["messages"]) >= 1000:
                raise TaskError("team_message_limit", "团队已达到1000条消息上限，请先收尾。")
            item = {"seq": len(state["messages"]) + 1, "sender": sender, "recipient": recipient,
                    "content": content, "created_at": now()}
            state["messages"].append(item)
            return {"message_id": item["seq"]}
        return self.mutate(session, operation, {"sender": sender, "recipient": recipient, "content": content, "request_meta": request_meta}, change)

    @staticmethod
    def requirements(task: dict, tasks: dict) -> list[str]:
        """返回task及全部父分组的前置编号；tasks为同源C04视图，最多100层，缺失祖先不提供执行权。"""
        required = list(task.get("depends_on", []))
        parent = task.get("parent_task_id")
        for _ in range(100):
            if parent is None:
                break
            ancestor = tasks.get(parent, {})
            required.extend(ancestor.get("depends_on", []))
            parent = ancestor.get("parent_task_id")
        return list(dict.fromkeys(required))

    @staticmethod
    def invalidate(state: dict, tasks: dict) -> set[str]:
        """按最新C04视图tasks使state失效，返回受影响编号集合；活动段保留认领，供安全边界收拢。"""
        invalid, requirements = set(), {}
        team_ids = {t["task_id"] for t in state["tasks"]}
        for item in state["tasks"]:
            identifier = item["task_id"]
            task = tasks.get(identifier)
            requirements[identifier] = TeamStore.requirements(task or {}, tasks)
            if (task is None or task.get("status") == "cancelled" or task.get("review_required")
                    or task_fingerprint(task) != item["fingerprint"] or (task.get("hold_reason") or "").startswith("来源方案")):
                invalid.add(identifier)
            parent = (task or {}).get("parent_task_id")
            for _ in range(100):
                if parent is None:
                    break
                ancestor = tasks.get(parent)
                if ancestor is None or ancestor.get("status") == "cancelled" or ancestor.get("review_required"):
                    invalid.add(identifier)
                    break
                parent = ancestor.get("parent_task_id")
            if item["state"] == "ready" and any(key not in team_ids and (tasks.get(key, {}).get("status") != "completed" or tasks.get(key, {}).get("blocked_reason")) for key in requirements[identifier]):
                invalid.add(identifier)
        for _ in state["tasks"]:
            for item in state["tasks"]:
                if any(key in invalid for key in requirements[item["task_id"]]):
                    invalid.add(item["task_id"])
        for item in state["tasks"]:
            if item["task_id"] in invalid:
                state["stage_ready"] = False
                if item["state"] not in {"running", "waiting_input", "needs_review"}:
                    item.update(state="stale", error="任务条件、父分组或依赖成果已变化，请重新分配核对。")
        return invalid

    def claim(self, session: str, operation: str, member_id: str, parent_id: str):
        """为空闲member_id原子认领首个授权且依赖就绪的任务；无候选返回None，不启动模型。"""
        from agent_harness.subagents.workspaces import in_scope
        def change(state, db):
            """在db事务内核对任务最新范围与成员授权，记录独占归属并返回固定任务副本。"""
            self.require(state)
            member = next((m for m in state["members"] if m["id"] == member_id), None)
            if member is None or member["status"] not in {"idle", "failed", "stopped"}:
                return None
            row = db.execute("SELECT body FROM lists WHERE id=?", (session,)).fetchone()
            tasks = {t["id"]: t for t in self.work._view(json.loads(row[0]))["items"]} if row else {}
            self.invalidate(state, tasks)
            ready = {t["task_id"]: t["run_id"] for t in state["tasks"] if t["state"] == "ready"}
            for item in state["tasks"]:
                task = tasks.get(item["task_id"])
                if item["state"] != "pending" or item.get("member_id") not in {None, member_id}:
                    continue
                if task is None or task["status"] in {"completed", "cancelled"} or task_fingerprint(task) != item["fingerprint"]:
                    item.update(state="stale", error="任务条件已变化。")
                    continue
                if any(key not in ready and (tasks.get(key, {}).get("status") != "completed" or tasks.get(key, {}).get("blocked_reason")) for key in self.requirements(task, tasks)):
                    continue
                if not all(in_scope(path, member["write_scope"]) for path in item["write_scope"]):
                    continue
                if task.get("active_run_id") not in {None, parent_id} or (task.get("owner") or "").startswith("subagent:"):
                    continue
                item.update(state="running", member_id=member_id, log_parent_id=parent_id,
                            dependency_versions={key: ready[key] for key in item["depends_on"] if key in ready})
                member.update(status="running", task_id=item["task_id"], active_run_id=None)
                return deepcopy(item)
            return None
        return self.mutate(session, operation, {"member_id": member_id, "parent_id": parent_id}, change)
