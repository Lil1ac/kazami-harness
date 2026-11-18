"""会话的可变管理状态与受控清理；整理归属和不可变执行证据分别管理。"""

import asyncio
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import shutil
from typing import Literal, TYPE_CHECKING
from uuid import UUID

from pydantic import Field

from agent_harness.schemas import DataModel
from agent_harness.storage.sessions import SessionError
from agent_harness.storage.runs import StorageError
from agent_harness.work.tasks import WorkStore, encode, now

if TYPE_CHECKING:
    from agent_harness.web.service import WebRunManager


class ConversationError(Exception):
    """携带会话管理的稳定原因、HTTP状态和可恢复身份，不回显系统异常。"""

    def __init__(self, code: str, message: str, status: int = 409, session_id: str | None = None):
        """保存code/message/status；session_id非空时供旧链接显示恢复入口，返回None。"""
        super().__init__(message)
        self.code = code  # 前端识别的稳定原因。
        self.message = message  # 可直接展示的中文说明。
        self.status = status  # 输入422、冲突409、回收站410、存储503。
        self.session_id = session_id  # 只公开会话UUID，不包含消息或文件路径。


class ConversationTarget(DataModel):
    """一次管理操作绑定的稳定身份及用户最后读到的版本。"""

    session_id: UUID = Field(strict=False)  # 首轮运行编号，不接受后续轮编号替代。
    expected_revision: int = Field(ge=0)  # 无人工管理状态时版本为0。


class ConversationChange(DataModel):
    """管理动作的完整载荷；同一request_id重试必须保持所有字段相同。"""

    request_id: UUID = Field(strict=False)  # 由浏览器生成，成功或响应丢失后重试沿用。
    expected_revision: int = Field(ge=0)  # 乐观锁，冲突后须重读。
    action: Literal["rename", "move", "pin", "unpin", "archive", "unarchive", "trash", "restore"]  # 单个明确动作。
    title: str | None = Field(default=None, max_length=200)  # rename时None恢复默认；非空须去空白后有效。
    project_id: str | None = Field(default=None, min_length=1, max_length=64)  # move时None移到未分组；不改变执行来源。


class ConversationBatch(DataModel):
    """仅对明确选择的至多50条会话整理；逐项返回结果，不扩展到不可见分页。"""

    request_id: UUID = Field(strict=False)  # 每条会话按此编号独立幂等。
    action: Literal["move", "archive", "unarchive", "trash", "restore"]  # 批量不包含命名和彻底删除。
    items: list[ConversationTarget] = Field(min_length=1, max_length=50)  # 编号不得重复。
    project_id: str | None = Field(default=None, min_length=1, max_length=64)  # move的统一目标；None为未分组。


class ConversationPurge(DataModel):
    """用户明确确认的单条物理清理；失败后仍可用原编号重试同一清单。"""

    request_id: UUID = Field(strict=False)  # 本次清理的稳定操作编号。
    expected_revision: int = Field(ge=0)  # 首次清理须匹配回收站版本。
    confirm: Literal[True]  # 明确确认后才清理，不存在默认值。


class ConversationManagement(DataModel):
    """统一供列表、侧栏和详情使用的会话管理视图。"""

    session_id: str  # 会话首轮UUID。
    title: str  # 人工标题优先，否则使用首条需求的精简标题。
    title_override: str | None  # None表示自动默认标题，无额外模型调用。
    assigned_project_id: str | None  # 整理归属，None表示未分组。
    assigned_project: dict | None  # id/label/path/available；目录移除仍展示原归属。
    state: Literal["active", "archived", "trashed", "purging", "purged"]  # 只控制管理生命周期，不伪造运行状态。
    pinned_at: str | None  # UTC置顶时间，归档和恢复时保留。
    deleted_at: str | None  # UTC移入回收站时间，恢复后清空。
    restore_state: Literal["active", "archived"]  # 从回收站恢复时的原状态。
    revision: int  # 人工变更版本；读取及模型运行不递增。
    can_organize: bool  # 无未完成作业、未知清理或准备意图时允许移动/归档/删除。
    blocked_reason: str | None  # 不可整理的实际原因。
    can_purge: bool  # 回收站且执行、独立工作树均已处理时允许彻底清理。
    purge_blocked_reason: str | None  # 工作树或清理故障的可读原因。
    run_count: int  # 已登记轮次，回收站确认时展示。
    memory_references: int  # 另行保存的项目记忆引用数量，不随对话自动删除。


class ConversationManager:
    """事务管理会话属性、访问限制与清理日志；不改写原轮次或项目代码。"""

    def __init__(self, manager: "WebRunManager"):
        """绑定唯一manager与共享WorkStore；构造不写盘，不读取运行或启动执行。"""
        self.manager = manager  # 可信运行、工作树及会话事实入口。
        self.store = WorkStore(manager.runs_root)  # 与队列及任务使用同一数据库和事务规则。

    def stored(self, session_id: str, db=None) -> dict | None:
        """返回session_id人工管理记录；db可复用事务，缺失返回None且读取不创建仓储。"""
        if db is None:
            if not self.store.path.exists():
                return None
            with self.store.transaction() as connection:
                return self.stored(session_id, connection)
        row = db.execute("SELECT body FROM conversations WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row[0])
            required = {"session_id", "default_title", "title_override", "assigned_project_id", "assigned_path", "assigned_label", "state", "revision", "pinned_at", "deleted_at", "restore_state"}
            if not isinstance(record, dict) or not required.issubset(record) or record["session_id"] != str(UUID(session_id)) or record["state"] not in {"active", "archived", "trashed", "purging", "purged"} or type(record["revision"]) is not int or record["revision"] < 0:
                raise ValueError("invalid metadata")
            if not isinstance(record["default_title"], str) or record["restore_state"] not in {"active", "archived"}:
                raise ValueError("invalid metadata fields")
            if any(record[key] is not None and not isinstance(record[key], str) for key in ("title_override", "assigned_project_id", "assigned_path", "assigned_label", "pinned_at", "deleted_at")):
                raise ValueError("invalid optional fields")
            if record["state"] in {"trashed", "purging", "purged"} and not record["deleted_at"]:
                raise ValueError("missing deletion time")
            for key in ("pinned_at", "deleted_at"):
                if record[key] is not None:
                    datetime.fromisoformat(record[key])
            if record["state"] in {"purging", "purged"} and (not isinstance(record.get("purge_ids"), list) or not record["purge_ids"] or any(not isinstance(identifier, str) or str(UUID(identifier)) != identifier for identifier in record["purge_ids"])):
                raise ValueError("invalid frozen cleanup scope")
            return record
        except (ValueError, TypeError, KeyError):
            raise ConversationError("conversation_metadata_unreadable", "对话管理记录无法核对，已保留原始记录。", 503, session_id) from None

    def record(self, session_id: str, db=None, history=None) -> dict:
        """返回session_id当前管理事实；无人工变更时从首轮推导默认值，不落盘。

        history可复用已读取首轮；首轮损坏由列表传入最早可读轮保留异常入口。
        不存在时抛404，purged墓碑无需再次访问已清理运行。
        """
        stored = self.stored(session_id, db)
        if stored is not None:
            return stored
        try:
            first = history or self.manager._read_history(UUID(session_id))
            if str(first.manifest.session_id or first.manifest.run_id) != session_id:
                raise ValueError("not session root")
        except (StorageError, ValueError):
            raise ConversationError("conversation_not_found", "对话不存在或无法读取。", 404) from None
        source = self.manager.conversation_index.source_for(first)
        return {"session_id": session_id, "default_title": " ".join(first.manifest.task.prompt.split())[:200] or "未命名对话",
                "title_override": None, "assigned_project_id": source.id if source and source.registered else None,
                "assigned_path": source.path if source and source.registered else None,
                "assigned_label": source.label if source and source.registered else None,
                "state": "active", "pinned_at": None, "deleted_at": None, "restore_state": "active", "revision": 0}

    def project(self, record: dict) -> dict | None:
        """返回record的当前整理项目；同编号改指另一目录时标不可用，不混入新项目。"""
        identifier = record["assigned_project_id"]
        if identifier is None:
            return None
        match = next((item for item in self.manager.catalog.recent() if item["id"] == identifier and os.path.normcase(item["path"]) == os.path.normcase(record["assigned_path"] or "")), None)
        return {"id": identifier, "label": match["label"] if match else record["assigned_label"] or "项目已移除",
                "path": record["assigned_path"], "available": match is not None}

    def busy_reason(self, session_id: str, db=None) -> str | None:
        """返回session_id当前不可整理原因；db复用事务，必须包含全部作业而非后台展示的500项。"""
        if db is None:
            if self.store.path.exists():
                with self.store.transaction() as connection:
                    return self.busy_reason(session_id, connection)
        else:
            row = db.execute("SELECT 1 FROM jobs WHERE json_extract(body,'$.session_id')=? AND json_extract(body,'$.status') IN ('queued','preparing','running','needs_review') LIMIT 1", (session_id,)).fetchone()
            if row:
                return "请先在后台工作中完成、停止或核对关联作业。"
        active = self.manager.active_for_session(session_id)
        if active and str(active.manifest.session_id or active.manifest.run_id) == session_id:
            return "对话仍在执行或保存证据，请等待收尾。"
        if any(str(key[0]) == session_id and not task.done() for key, task in self.manager._submissions.items()):
            return "对话正在准备追加运行，请稍后整理。"
        try:
            session = self.manager.conversations.store.read(UUID(session_id))
            if any(intent.state == "preparing" for intent in session.requests.values()):
                return "存在尚未核对的追加准备，请先处理会话状态。"
            latest = self.manager._read_history(session.run_ids[-1])
            if latest.result and latest.result.cleanup_status in {"unknown", "incomplete"}:
                return "前轮进程清理尚未确认，请先核对。"
        except (StorageError, SessionError):
            return "轮次记录无法核对，暂时不能移动或删除。"
        return None

    def purge_reason(self, record: dict, busy: str | None, db=None) -> str | None:
        """核对record物理清理条件，返回阻止原因；db非空复用已有事务，不嵌套申请写锁或删除工作树。"""
        if record["state"] not in {"trashed", "purging"}:
            return "请先将对话移入回收站。"
        if busy and record["state"] != "purging":
            return busy
        if record["state"] == "purging":
            return None  # 清理已获批准并冻结清单；重试不能再依赖可能已删除的父子日志。
        try:
            session = self.manager.conversations.store.read(UUID(record["session_id"]))
            review = self.manager.projects.get(session.run_ids[-1])
            if review is not None and self.manager.projects.workspace(session.run_ids[-1]).status != "discarded":
                return "独立工作树仍保留，请先恢复对话，在文件审阅中处理并明确丢弃工作树。"
            from agent_harness.teams.store import TeamStore
            team = TeamStore(self.manager.runs_root).get(record["session_id"], db)
            if team:
                identifiers = [team["stage_run_id"], *team.get("workspace_run_ids", []), *team.get("stage_run_ids", []), *[r["run_id"] for m in team["members"] for r in m["runs"]]]
                for identifier in identifiers:
                    if self.manager.projects.get(UUID(identifier)) and self.manager.projects.workspace(UUID(identifier)).status != "discarded":
                        return "团队工作树仍保留，请恢复对话，在团队面板停止并明确丢弃工作树。"
            for identifier in session.run_ids:
                view = self.manager.get_run(identifier)
                for child in view.result.subagents:
                    if child.role == "develop" and self.manager.projects.workspace(child.run_id).status != "discarded":
                        return "开发子工作树仍保留，请先恢复对话，在子任务详情明确丢弃工作树。"
        except Exception:
            return "工作树归属无法核对，请保留记录并处理后重试。"
        return None

    def view(self, session_id: str, *, history=None) -> ConversationManagement:
        """聚合session_id的统一管理视图；history仅用于复用首轮读取，返回值不含清理内部路径。"""
        record = self.record(session_id, history=history)
        busy = self.busy_reason(session_id) if record["state"] not in {"purging", "purged"} else "对话正在清理或已被彻底删除。"
        try:
            count = 0 if record["state"] == "purged" else record["purge_run_count"] if "purge_run_count" in record else len(self.manager.conversations.store.read(UUID(session_id)).run_ids)
        except SessionError:
            count = 0
        reason = self.purge_reason(record, busy)
        return ConversationManagement(**{key: record[key] for key in ("session_id", "title_override", "assigned_project_id", "state", "pinned_at", "deleted_at", "restore_state", "revision")},
            title=record["title_override"] or record["default_title"], assigned_project=self.project(record),
            can_organize=busy is None, blocked_reason=busy,
            can_purge=reason is None, purge_blocked_reason=record.get("purge_error") or reason, run_count=count,
            memory_references=self.memory_references(session_id) if record["state"] == "trashed" else record.get("purge_memory_references", 0))

    def memory_references(self, session_id: str) -> int:
        """返回另行保存的记忆引用数量；只读当前记忆库，缺少库或会话时为0。"""
        from agent_harness.memory.service import MemoryStore
        store = MemoryStore(self.manager.runs_root)
        if not store.path.exists():
            return 0
        try:
            ids = {str(item) for item in self.manager.conversations.store.read(UUID(session_id)).run_ids}
            with store.transaction() as db:
                rows = db.execute("SELECT body FROM revisions WHERE seq IN (SELECT MAX(seq) FROM revisions GROUP BY scope,id)").fetchall()
            sources = [json.loads(row[0]).get("source", {}) for row in rows]
            return sum(source.get("run_id") in ids or any(ref["run_id"] in ids for ref in source.get("refs", [])) for source in sources)
        except (SessionError, StorageError):
            return 0

    def require_active(self, session_id: str, db=None) -> None:
        """拒绝session_id归档及删除后的新执行；db可与作业接纳复用同一事务，返回None。"""
        record = self.stored(session_id, db)
        if record and record["state"] != "active":
            raise ConversationError("conversation_inactive", "请先恢复会话，再继续发送或处理计划。", 409, session_id)

    def check_run(self, run_id: str, *, writing: bool = False) -> None:
        """对外部run_id访问执行生命周期检查；writing=True也拒绝归档，返回None，不启动任务。"""
        tombstone = self.stored(run_id)
        if tombstone and tombstone["state"] in {"trashed", "purging", "purged"}:
            raise ConversationError("conversation_deleted", "对话已在回收站或已被彻底删除。", 410, run_id)
        try:
            history = self.manager._read_history(UUID(run_id))
            session_id = str(history.manifest.session_id or history.manifest.run_id)
        except StorageError:
            # 彻底删除后仍能由最小作业回执定位墓碑，而不能复活或重新接纳旧请求。
            if not self.store.path.exists():
                return
            with self.store.transaction() as db:
                row = db.execute("SELECT body FROM jobs WHERE json_extract(body,'$.run_id')=? LIMIT 1", (run_id,)).fetchone()
            if not row:
                return
            session_id = json.loads(row[0]).get("session_id")
            if not session_id:
                return
        record = self.stored(session_id)
        if record and record["state"] in {"trashed", "purging", "purged"}:
            raise ConversationError("conversation_deleted", "对话已在回收站或已被彻底删除。", 410, session_id)
        if writing:
            self.require_active(session_id)

    async def change(self, session_id: str, body: ConversationChange) -> ConversationManagement:
        """按body原子变更session_id管理属性，返回真实视图；同编号幂等，版本冲突拒绝覆盖。"""
        async with self.manager._lock:
            key = session_id + ":" + str(body.request_id)
            fingerprint = hashlib.sha256(encode(body.model_dump(mode="json")).encode()).hexdigest()
            with self.store.transaction() as db:
                previous = db.execute("SELECT digest,result FROM conversation_operations WHERE id=?", (key,)).fetchone()
                if previous:
                    if previous[0] != fingerprint:
                        raise ConversationError("request_conflict", "操作编号已用于其他内容。")
                    return ConversationManagement.model_validate_json(previous[1])
                record = self.record(session_id, db)
                if record["state"] in {"purging", "purged"}:
                    raise ConversationError("conversation_deleted", "对话已进入彻底清理，不能恢复或更改。", 410, session_id)
                if record["revision"] != body.expected_revision:
                    raise ConversationError("conversation_revision_conflict", "对话已在其他窗口更新，请刷新后重试。")
                action = body.action
                if action in {"move", "archive", "trash"}:
                    reason = self.busy_reason(session_id, db)
                    if reason:
                        raise ConversationError("conversation_busy", reason)
                if record["state"] == "trashed" and action != "restore":
                    raise ConversationError("conversation_inactive", "请先从回收站恢复对话。")
                if action == "rename":
                    title = body.title.strip() if body.title is not None else None
                    if title is not None and (not title or any(ord(char) < 32 or ord(char) == 127 for char in title)):
                        raise ConversationError("conversation_title_invalid", "标题须为1至200个字符，且不能包含控制字符。", 422)
                    record["title_override"] = title
                elif action == "move":
                    target = next((item for item in self.manager.catalog.recent() if item["id"] == body.project_id), None)
                    if body.project_id is not None and target is None:
                        raise ConversationError("unknown_project", "目标项目不存在，请刷新项目列表。", 404)
                    record.update(assigned_project_id=body.project_id, assigned_path=target["path"] if target else None, assigned_label=target["label"] if target else None)
                elif action in {"pin", "unpin"}:
                    record["pinned_at"] = now() if action == "pin" else None
                elif action == "archive":
                    record["state"] = "archived"
                elif action == "unarchive":
                    record["state"] = "active"
                elif action == "trash":
                    record.update(restore_state=record["state"], state="trashed", deleted_at=now())
                elif action == "restore":
                    if record["state"] != "trashed":
                        raise ConversationError("conversation_not_trashed", "对话不在回收站中。")
                    record.update(state=record["restore_state"], deleted_at=None)
                record["revision"] += 1
                db.execute("INSERT OR REPLACE INTO conversations VALUES (?,?)", (session_id, encode(record)))
                # 同一事务生成回执，view不能再开连接，避免SQLite写锁递归。
                result = self._receipt(record, db)
                db.execute("INSERT INTO conversation_operations VALUES (?,?,?,?)", (key, session_id, fingerprint, encode(result)))
                self.store.event(db, session_id, {"type": "conversation_updated", "session_id": session_id, "revision": record["revision"]})
            return ConversationManagement(**result)

    def _receipt(self, record: dict, db) -> dict:
        """在db当前事务生成record管理回执；不另开写事务，不包含消息或删除清单。"""
        reason = self.busy_reason(record["session_id"], db)
        purge_reason = self.purge_reason(record, reason, db)
        return {**{key: record[key] for key in ("session_id", "title_override", "assigned_project_id", "state", "pinned_at", "deleted_at", "restore_state", "revision")},
                "title": record["title_override"] or record["default_title"], "assigned_project": self.project(record),
                "can_organize": reason is None, "blocked_reason": reason, "can_purge": purge_reason is None,
                "purge_blocked_reason": purge_reason, "run_count": 0 if record["state"] == "purged" else record.get("purge_run_count", len(self.manager.conversations.store.read(UUID(record["session_id"])).run_ids)), "memory_references": 0}

    async def batch(self, body: ConversationBatch) -> dict:
        """逐项执行body明确选择的会话，返回成功及失败；重复编号整批拒绝，不清空失败项。"""
        if len({item.session_id for item in body.items}) != len(body.items):
            raise ConversationError("conversation_batch_invalid", "批量操作不能重复选择同一对话。", 422)
        results = []
        for item in body.items:
            try:
                result = await self.change(str(item.session_id), ConversationChange(request_id=body.request_id, expected_revision=item.expected_revision, action=body.action, project_id=body.project_id))
                results.append({"session_id": str(item.session_id), "success": True, "conversation": result.model_dump(mode="json")})
            except ConversationError as error:
                results.append({"session_id": str(item.session_id), "success": False, "error": {"code": error.code, "message": error.message}})
        return {"items": results}

    async def purge(self, session_id: str, body: ConversationPurge) -> ConversationManagement:
        """确认后清理session_id专属记录；冻结清单先落盘，失败保留可重试状态，绝不删除项目源码。"""
        async with self.manager._lock:
            with self.store.transaction() as db:
                record = self.record(session_id, db)
                if str(body.request_id) == record.get("purge_request_id") and body.expected_revision != record.get("purge_expected_revision"):
                    raise ConversationError("request_conflict", "同一清理请求编号不能更换确认版本。")
                if record["state"] == "purged":
                    return ConversationManagement(**self._receipt(record, db))
                if record["state"] != "purging":
                    if record["revision"] != body.expected_revision:
                        raise ConversationError("conversation_revision_conflict", "对话已更新，请刷新后确认删除。")
                    reason = self.purge_reason(record, self.busy_reason(session_id, db), db)
                    if reason:
                        raise ConversationError("conversation_purge_blocked", reason)
                    session = self.manager.conversations.store.read(UUID(session_id))
                    ids = {str(identifier) for identifier in session.run_ids} | {str(intent.run_id) for intent in session.requests.values()}
                    child_ids = set()  # 子日志随父目录删除，子工作树元数据和固定文件仍需纳入清理清单。
                    for identifier in ids:
                        if (self.manager.runs_root / identifier).exists():
                            history = self.manager._read_history(UUID(identifier))
                            if str(history.manifest.session_id or history.manifest.run_id) != session_id:
                                raise ConversationError("conversation_purge_blocked", "轮次归属不一致，不能清理记录。")
                            child_ids.update(str(item.run_id) for item in self.manager.get_run(UUID(identifier)).result.subagents if item.role == "develop")
                    team_row = db.execute("SELECT body FROM teams WHERE id=?", (session_id,)).fetchone()
                    if team_row:
                        team = json.loads(team_row[0])
                        child_ids.update(team.get("stage_run_ids", [team["stage_run_id"]]))
                        child_ids.update(team.get("workspace_run_ids", []))
                        if team.get("initial_stage_id"):
                            child_ids.add(team["initial_stage_id"])
                        child_ids.update(r["run_id"] for m in team["members"] for r in m["runs"])
                        record["purge_team_id"] = team["id"]
                    ids.update(child_ids)
                    record.update(state="purging", revision=record["revision"] + 1, purge_ids=sorted(ids), purge_run_count=len(session.run_ids), purge_memory_references=self.memory_references(session_id), purge_error=None,
                                  purge_request_id=str(body.request_id), purge_expected_revision=body.expected_revision)
                    db.execute("INSERT OR REPLACE INTO conversations VALUES (?,?)", (session_id, encode(record)))
                    self.store.event(db, session_id, {"type": "conversation_updated", "session_id": session_id})
            purge_ids = list(record["purge_ids"])  # 重试仍使用已冻结清单，不从残留目录重新推断范围。
            try:
                await asyncio.to_thread(self.manager.memory_jobs.purge, session_id)
                await asyncio.to_thread(self._clean_files, record)
                await asyncio.to_thread(self.manager.integrations.store.cleanup_files, session_id)
            except Exception:
                record["purge_error"] = "部分记录尚未清理完成；已保留清理清单，可重试彻底删除。"
                with self.store.transaction() as db:
                    db.execute("UPDATE conversations SET body=? WHERE id=?", (encode(record), session_id))
                raise ConversationError("conversation_purge_failed", record["purge_error"], 409, session_id) from None
            with self.store.transaction() as db:
                rows = db.execute("SELECT id,body FROM jobs WHERE json_extract(body,'$.session_id')=?", (session_id,)).fetchall()
                for row in rows:
                    job = json.loads(row[1])
                    minimal = {key: job[key] for key in ("id", "digest", "run_id", "session_id", "status", "created_at")}
                    minimal.update(title="已删除对话", purged=True, validation_status="unverified", execution_status=None, error=None)
                    db.execute("UPDATE jobs SET body=? WHERE id=?", (encode(minimal), row[0]))
                    db.execute("DELETE FROM events WHERE scope=?", (row[0],))
                db.execute("DELETE FROM lists WHERE id=?", (session_id,))
                db.execute("DELETE FROM runtime_controls WHERE json_extract(body,'$.session_id')=?", (session_id,))
                db.execute("DELETE FROM hook_state WHERE json_extract(body,'$.session_id')=?", (session_id,))
                db.execute("DELETE FROM teams WHERE id=?", (session_id,))
                db.execute("DELETE FROM team_operations WHERE substr(id,1,?)=?", (len(session_id) + 1, session_id + ":"))
                # 动作中可能包含待写正文；彻底删除必须连同许可一起清理，旧请求也无法再次消费。
                db.execute("DELETE FROM permission_actions WHERE json_extract(body,'$.session_id')=?", (session_id,))
                self.manager.integrations.store.cleanup_records(session_id, db)
                db.execute("DELETE FROM operations WHERE substr(id,1,?)=?", (len(session_id) + 1, session_id + ":"))
                db.execute("DELETE FROM conversation_operations WHERE session=?", (session_id,))
                db.execute("DELETE FROM events WHERE scope=?", (session_id,))
                record.update(state="purged", default_title="已删除对话", title_override=None, assigned_project_id=None, assigned_path=None, assigned_label=None, pinned_at=None, purge_error=None)
                db.execute("UPDATE conversations SET body=? WHERE id=?", (encode(record), session_id))
                self.store.event(db, session_id, {"type": "conversation_updated", "session_id": session_id, "state": "purged"})
                result = self._receipt(record, db)
            for identifier in purge_ids:
                self.manager._history_cache.pop(UUID(identifier), None)
            return ConversationManagement(**result)

    def _clean_files(self, record: dict) -> None:
        """按record冻结UUID清理受保护运行根内的白名单目录；越界、链接、归属异常立即拒绝。

        删除幂等且不跟随符号链接/Windows重解析点；仅删除证据目录及会话JSON，
        worktrees_root和用户项目目录不在白名单中，Git工作树须事先显式丢弃。
        """
        root = self.manager.runs_root
        if root.resolve(strict=True) != root:
            raise OSError("records root changed")
        paths = [root / ".sessions" / (str(UUID(record["session_id"])) + ".json")]
        for identifier in record["purge_ids"]:
            identifier = str(UUID(identifier))
            paths.extend(root / prefix / identifier if prefix else root / identifier for prefix in ("", ".local-before", ".local-after", ".local-changes", ".project-files", ".projects"))
        if record.get("purge_team_id"):
            paths.append(self.manager.projects.metadata_root / ".teams" / str(UUID(record["purge_team_id"])))
        # 先检查完整清单，不能遇到链接后已经删掉一半可恢复记录。
        for path in paths:
            if not path.is_relative_to(root) or path == root or path.resolve() != path:
                raise OSError("unsafe record path")
            for parent in (path, *path.parents):
                if parent == root:
                    break
                if parent.is_symlink() or parent.is_junction():
                    raise OSError("record link")
            if path.is_dir() and any(item.is_symlink() or item.is_junction() for item in path.rglob("*")):
                raise OSError("nested record link")
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
