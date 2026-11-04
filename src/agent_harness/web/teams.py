"""团队的Web/桌面共用入口；读取历史、用户指示、停止及明确回收，执行仍走唯一队列。"""

import asyncio
from copy import deepcopy
from typing import Literal
from uuid import UUID

from pydantic import Field
from agent_harness.schemas import DataModel
from agent_harness.teams.store import TeamStore
from agent_harness.work.tasks import TaskError
from agent_harness.permissions.store import PermissionStore


class TeamAction(DataModel):
    """用户的版本化团队决定；网页不能指定运行目录、发送者、预算或批准模型动作。"""
    request_id: UUID = Field(strict=False)  # 同内容重试沿用编号。
    expected_revision: int = Field(ge=1)  # 停止/清理/确认须匹配实际审阅版本；普通消息可并发追加。
    action: Literal["message", "stop", "discard", "confirm_stopped"]  # 确认停止仅处理用户已核对的未知旧段。
    recipient: str = Field(default="lead", max_length=100)  # 消息收件身份；其余操作不使用。
    content: str = Field(default="", max_length=4000)  # 消息正文；不是命令，也不提供审批。
    wake: bool = False  # 仅消息可显式排队继续负责人；活动时只交付消息，不新开执行。


def install_team_routes(app, manager) -> None:
    """为app注册绑定manager的团队路由，返回None；所有副作用复用同源及任务生命周期校验。"""
    store = TeamStore(manager.runs_root)

    def current(run_id: UUID):
        """核对run_id属于最新可写会话并返回视图/团队；历史或缺团队拒绝操作。"""
        view = manager.get_run(run_id)
        session = str(view.session_id or view.run_id)
        manager.management.require_active(session)
        if manager.conversations.store.read(UUID(session)).run_ids[-1] != run_id:
            raise TaskError("team_historical", "历史团队只读，请打开会话最新一轮。")
        state = store.get(session)
        if state is None:
            raise TaskError("team_not_found", "本会话还没有团队，请让主Agent按批准方案创建。")
        return view, state

    @app.get("/api/runs/{run_id}/team")
    async def team_view(run_id: UUID, historical: bool = False) -> dict:
        """返回run_id当前团队或historical固定快照；只读，不恢复模型、未知操作或工作树。"""
        view = manager.get_run(run_id)
        session = str(view.session_id or view.run_id)
        latest = manager.conversations.store.read(UUID(session)).run_ids[-1]
        history = historical or latest != run_id
        state = deepcopy(view.result.team if history else store.get(session))
        if state and not history and not view.active:
            for member in state["members"]:
                if member["status"] == "running":
                    member["status"] = "needs_review"
            for item in state["tasks"]:
                if item["state"] == "running":
                    item["state"] = "needs_review"
                    item["error"] = "执行已失联；先核对原段和进程，不能自动重放。"
        tasks = view.result.tasks if history else store.work.snapshot(session)
        if state and not history:
            store.invalidate(state, {task["id"]: task for task in tasks["items"]})
        stage = manager.projects.get(UUID(state["stage_run_id"])) if state else None
        return {"team": state, "tasks": tasks, "stage": stage.model_dump(mode="json") if stage else None, "historical": history}

    @app.post("/api/runs/{run_id}/team/actions")
    async def team_action(run_id: UUID, body: TeamAction) -> dict:
        """接纳body明确决定并返回持久回执；消息收件人与来源由宿主核验，空闲续轮走现有作业队列。"""
        view, state = current(run_id)
        session, operation = str(view.session_id or view.run_id), "user:" + str(body.request_id)
        if body.action == "message":
            receipt = store.message(session, operation, "user", body.recipient, body.content, request_meta={"wake": body.wake})
            job = None
            if body.wake and not view.active:
                from agent_harness.web.jobs import JobRequest
                from agent_harness.web.schemas import FollowupRequest
                request = FollowupRequest(request_id=body.request_id, prompt="处理团队消息箱内用户指示，在已批准方案和现有权限内继续协作；先核对成员、依赖和已执行事实，不重复旧操作。")
                job = await manager.jobs.enqueue(JobRequest(request_id=body.request_id, parent_run_id=run_id, followup=request))
            return {**receipt, "team": store.get(session), "job": job}
        async with manager._lock:
            return await decide_team(run_id, body)

    async def decide_team(run_id: UUID, body: TeamAction) -> dict:
        """在宿主锁内执行body停止/清理决定，返回事实回执；锁覆盖最后核验至全部文件操作。"""
        view, state = current(run_id)
        session, operation = str(view.session_id or view.run_id), "user:" + str(body.request_id)
        if state["revision"] != body.expected_revision:
            # 同请求已完成允许读取原回执，不能借重试重新执行文件清理。
            with store.work.transaction() as db:
                old = db.execute("SELECT digest,result FROM team_operations WHERE id=?", (session + ":" + operation,)).fetchone()
            if old:
                import json, hashlib
                from agent_harness.work.tasks import encode
                digest = hashlib.sha256(encode(body.model_dump(mode="json")).encode()).hexdigest()
                if old[0] != digest:
                    raise TaskError("request_conflict", "相同团队请求编号不能更换内容。")
                return {**json.loads(old[1]), "team": store.get(session)}
            raise TaskError("team_revision_conflict", "团队已变化，请刷新后重新决定。")
        if body.action == "stop" and view.active:
            active = manager._active
            controller = active.context.subagent_controller if active and active.context else None
            if controller is None or active.manifest.run_id != run_id or controller.team is None:
                raise TaskError("team_not_active", "执行正在准备或收尾，请刷新后重试。")
            await controller.team.stop("controller:" + operation)
            def receipt(value, db):
                """固定本次用户停止回执，重试校验完整正文；返回简单动作事实，不重复取消成员。"""
                return {"action": "stop", "team_id": value["id"]}
            result = store.mutate(session, operation, body.model_dump(mode="json"), receipt)
            return {**result, "team": store.get(session)}
        if view.active or manager.management.busy_reason(session):
            raise TaskError("team_busy", "请先停止执行并处理后台待核对作业。")
        if body.action == "discard":
            if state["status"] != "stopped" or any(m["status"] == "needs_review" for m in state["members"]):
                raise TaskError("team_cleanup_unconfirmed", "请先停止团队并核对全部执行进程。")
            identifiers = [state["stage_run_id"], *state.get("workspace_run_ids", []), *state.get("stage_run_ids", []), *[r["run_id"] for m in state["members"] for r in m["runs"]]]
            latest = set()
            for identifier in identifiers:
                if manager.projects.get(UUID(identifier)):
                    workspace = manager.projects.workspace(UUID(identifier))
                    if workspace.status != "discarded":
                        latest.add(workspace.latest_run_id)
            for identifier in latest:
                review = manager.projects.get(identifier)
                if review.snapshot_status == "pending":
                    await asyncio.to_thread(manager.projects.capture, identifier)
                await asyncio.to_thread(manager.projects.decide, identifier, "discard")
        if body.action == "stop":
            for member in state["members"]:
                for run in member["runs"]:
                    from agent_harness.subagents.service import read_child, SubagentError
                    from agent_harness.storage.runs import StorageError
                    if run["run_id"] in state.get("confirmed_stopped_runs", []):
                        continue
                    try:
                        history = read_child(manager.runs_root, UUID(run["parent_run_id"]), UUID(run["run_id"]))
                    except (SubagentError, StorageError):
                        raise TaskError("team_cleanup_unconfirmed", "旧执行日志无法核对，请先确认原进程已停止。") from None
                    if (history.result is None or history.result.cleanup_status not in {"not_needed", "complete"}) and run["run_id"] not in state.get("confirmed_stopped_runs", []):
                        raise TaskError("team_cleanup_unconfirmed", "旧执行缺可信结束或进程清理，请核对后确认已停止。")
                    if history.result.permissions.pending:
                        PermissionStore(manager.runs_root).invalidate(history.result.permissions.pending["id"], "team_stopped")
        def change(value, db):
            """保存已确认操作的最终事实；不把停止、回收或确认旧段当作任务完成。"""
            if body.action == "confirm_stopped":
                known = set(value.get("confirmed_stopped_runs", []))
                known.update(t["run_id"] for t in value["tasks"] if t.get("run_id") and t["state"] in {"needs_review", "running"})
                value["confirmed_stopped_runs"] = sorted(known)  # 人工确认另存，不改写旧执行的未知清理事实。
                if value["status"] == "stopping":
                    value["status"] = "stopped"
                for member in value["members"]:
                    if member["status"] in {"needs_review", "running"}:
                        member.update(status="failed", active_run_id=None, task_id=None)
                for task in value["tasks"]:
                    if task["state"] in {"needs_review", "running"}:
                        task.update(state="failed", error="用户已核对旧执行和进程停止，等待明确重新分配。")
            elif body.action == "stop":
                value["status"] = "stopped"
                for member in value["members"]:
                    member.update(status="stopped", active_run_id=None)
            elif body.action == "discard":
                value["cleanup_status"] = "complete"
            return {"action": body.action, "team_id": value["id"]}
        result = store.mutate(session, operation, body.model_dump(mode="json"), change, expected_revision=body.expected_revision)
        return {**result, "team": store.get(session)}
