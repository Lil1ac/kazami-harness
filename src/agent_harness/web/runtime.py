"""运行控制 HTTP 契约；所有副作用入口沿用原队列、目录核验和权限模型。"""

import json
from uuid import UUID

from fastapi import Query

from agent_harness.execution.commands import command_records, output_page
from agent_harness.runtime.control import ControlStore, RuntimeInput, RuntimeAction
from agent_harness.web.jobs import JobRequest
from agent_harness.web.schemas import FollowupRequest
from agent_harness.work.tasks import TaskError


async def dispatch_next_input(manager, session_id: str, parent_id: str) -> dict | None:
    """将 session_id 最早一条下一轮要求接入原队列；parent_id 须为可信最新完成段。

    入队前持久绑定父编号，断开或崩溃后相同输入仅能确认原作业；无待投递消息返回 None。
    暂停、审批及未知清理不会自动启动新轮。
    """
    store = ControlStore(manager.runs_root)
    with store.store.transaction() as db:
        rows = db.execute("SELECT body FROM runtime_controls WHERE json_extract(body,'$.session_id')=? ORDER BY rowid", (session_id,)).fetchall()
        selected = None
        for row in rows:
            state = json.loads(row[0])
            selected = next((item for item in state["messages"] if item["delivery"] == "next" and item["status"] == "received"), None)
            if selected:
                selected.setdefault("dispatch_parent_id", parent_id)
                selected["job_id"] = selected["id"]  # 先冻结派发意图，异步接纳期间不能再撤回原消息。
                store.save(state, db)
                break
    if selected is None:
        return None
    try:
        request_id = UUID(selected["id"])
        request = FollowupRequest(request_id=request_id, prompt=selected["prompt"])
        job = await manager.jobs.enqueue(JobRequest(request_id=request_id, parent_run_id=UUID(selected["dispatch_parent_id"]), followup=request))
    except Exception as error:
        # 只标明未自动投递，原消息仍供页面核对；禁止为了绕过待审批或旧父状态改投新轮。
        with store.store.transaction() as db:
            current = store.get(state["run_id"], db)
            item = next(item for item in current["messages"] if item["id"] == selected["id"])
            item["error"] = getattr(error, "message", "下一轮请求尚未确认，请核对后台队列。")
            store.save(current, db)
        return None
    with store.store.transaction() as db:
        current = store.get(state["run_id"], db)
        item = next(item for item in current["messages"] if item["id"] == selected["id"])
        item.update(status="queued", job_id=job["id"], error=None)
        store.save(current, db)
    return job


def install_runtime_routes(app, manager) -> None:
    """给 app 安装运行输入、暂停及命令观察接口；manager 统一核验会话访问与归属。"""
    store = ControlStore(manager.runs_root)

    def readable(run_id: UUID):
        """返回可读 run_id 视图；回收站、删除及无权限记录使用原访问检查。"""
        manager.management.check_run(str(run_id))
        return manager.get_run(run_id)

    def writable(run_id: UUID):
        """核验 run_id 为当前活动最新段；归档、历史、取消和收尾阶段拒绝新控制请求。"""
        view = readable(run_id)
        manager.management.require_active(str(view.session_id or view.run_id))
        item = manager._active.get(run_id)
        if not view.active or item is None or item.cancelling or item.finalizing or item.context is None or item.context.phase == "artifact_collection":
            raise TaskError("runtime_not_running", "运行已经收尾，请查看历史或继续会话。")
        return view

    def histories(run_id: UUID):
        """返回 run_id 本段及直属成员的已核验日志，附实际活动标记，不执行历史动作。"""
        view = readable(run_id)
        values = [(manager._read_history(run_id), view.active)]
        from agent_harness.subagents.service import read_child
        for child in view.result.subagents:
            parent_id = child.log_parent_id or run_id
            history = read_child(manager.runs_root, parent_id, child.run_id)
            values.append((history, view.active and child.status == "running"))
        return view, values

    @app.get("/api/runs/{run_id}/runtime")
    async def runtime_view(run_id: UUID) -> dict:
        """读取 run_id 控制、消息和父子命令快照；重启遗留活动状态仅读投影为待核对。"""
        view, values = histories(run_id)
        state = store.get(str(run_id))
        if state is not None:
            state.pop("requests", None)
            if not view.active and state["status"] in {"running", "pausing"}:
                state["status"] = "needs_review"
        return {"state": state, "commands": [record for history, active in values for record in command_records(history, active=active)], "active": view.active}

    @app.post("/api/runs/{run_id}/inputs", status_code=202)
    async def runtime_input(run_id: UUID, body: RuntimeInput) -> dict:
        """接纳 body 的运行中要求；原编号重试可读回回执，历史不会产生新消息。"""
        readable(run_id)
        state = store.get(str(run_id))
        existing = state is not None and any(item["id"] == str(body.request_id) for item in state["messages"])
        if not existing:
            writable(run_id)
        return store.input(str(run_id), body)

    @app.post("/api/runs/{run_id}/pause", status_code=202)
    async def pause(run_id: UUID, body: RuntimeAction) -> dict:
        """请求 run_id 安全暂停；返回调度意图，只有 Runner 收尾后才发布已暂停状态。"""
        readable(run_id)
        state = store.get(str(run_id))
        if state is None or str(body.request_id) not in state["requests"]:
            writable(run_id)
        return store.pause(str(run_id), body)

    @app.delete("/api/runs/{run_id}/inputs/{message_id}")
    async def retract(run_id: UUID, message_id: UUID) -> dict:
        """撤回 run_id 尚未投递的 message_id；只修改收件箱，不撤销已经发生的动作。"""
        view = readable(run_id)
        manager.management.require_active(str(view.session_id or view.run_id))
        if manager.conversations.store.read(view.session_id or view.run_id).run_ids[-1] != run_id:
            raise TaskError("runtime_historical", "历史输入只读，请在会话最新执行段处理。")
        return store.retract(str(run_id), str(message_id))

    @app.get("/api/runs/{run_id}/commands/{command_id}/output")
    async def command_output(run_id: UUID, command_id: UUID, owner_run_id: UUID | None = None,
                             stream: str = Query(default="stdout", pattern="^(stdout|stderr)$"),
                             offset: int = Query(default=0, ge=0), max_bytes: int = Query(default=8192, ge=4, le=65536)) -> dict:
        """读取 run_id 所属 command_id 双流；owner_run_id 指向本段或直属子段，不接受任意路径。"""
        _, values = histories(run_id)
        for history, active in values:
            if owner_run_id is not None and history.manifest.run_id != owner_run_id:
                continue
            record = next((item for item in command_records(history, active=active) if item["id"] == str(command_id)), None)
            if record is not None:
                directory = manager.runs_root / str(history.manifest.run_id)
                if history.manifest.delegation:
                    directory = manager.runs_root / str(history.manifest.delegation.parent_run_id) / "subagents" / str(history.manifest.run_id)
                return {"command": record, "output": output_page(directory, record, stream, offset, max_bytes)}
        raise TaskError("command_not_found", "该命令不属于当前运行或成员。")

    @app.post("/api/runs/{run_id}/commands/{command_id}/stop")
    async def stop_command(run_id: UUID, command_id: UUID, owner_run_id: UUID | None = None) -> dict:
        """停止当前根 run_id 内 command_id 并等待清理；旧 PID 不作为恢复或停止依据。"""
        view = readable(run_id)
        manager.management.require_active(str(view.session_id or view.run_id))
        item = manager._active.get(run_id)
        if not view.active or item is None or item.context is None:
            raise TaskError("command_not_active", "历史命令只能核对记录，不能按旧 PID 执行停止。")
        for owner, controller in item.context.command_controllers.items():
            if owner_run_id is not None and owner != str(owner_run_id):
                continue
            if str(command_id) in controller.records:
                result = await controller.stop(str(command_id))
                return {"command": controller.get(str(command_id)), "result": result}
        raise TaskError("command_not_found", "该命令不属于当前活动执行。")

    @app.post("/api/runs/{run_id}/dispatch-next", status_code=202)
    async def dispatch_next(run_id: UUID, body: RuntimeAction) -> dict:
        """明确投递已接收但未入队的消息；body 表达操作意图，作业幂等仍使用原消息编号，不批准等待。"""
        view = readable(run_id)
        manager.management.require_active(str(view.session_id or view.run_id))
        if manager.conversations.store.read(view.session_id or view.run_id).run_ids[-1] != run_id:
            raise TaskError("runtime_historical", "只能从会话最新执行段投递下一轮要求。")
        if view.active or view.interrupted or view.result.execution_status != "completed":
            raise TaskError("runtime_not_completed", "请先完成或核对当前执行及审批。")
        job = await dispatch_next_input(manager, str(view.session_id or view.run_id), str(run_id))
        return {"job": job}
