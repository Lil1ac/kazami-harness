"""运行中用户输入和协作式暂停；业务事实持久化，工具结果仍由单一 Runner 写入。"""

from copy import deepcopy
import json
from typing import Literal
from uuid import UUID

from pydantic import Field

from agent_harness.schemas import DataModel
from agent_harness.storage.sessions import payload_digest
from agent_harness.work.tasks import WorkStore, TaskError, encode, now


class RuntimeInput(DataModel):
    """用户输入的固定意图；不包含权限、工具参数或模型生成的发送者。"""

    request_id: UUID = Field(strict=False)  # HTTP 重试必须保持相同编号和正文。
    prompt: str = Field(min_length=1, max_length=16000)  # 本次补充约束或下一轮要求，纯空白拒绝。
    delivery: Literal["steer", "next"] = "steer"  # steer 在本轮安全边界处理；next 在正常完成后创建新轮。


class RuntimeAction(DataModel):
    """暂停、撤回未投递输入等控制操作的幂等编号。"""

    request_id: UUID = Field(strict=False)  # 相同操作重试不重复改变状态。


class ControlStore:
    """复用原 SQLite 事务保存运行控制及有界收件箱，历史读取不会恢复执行。"""

    def __init__(self, runs_root):
        """绑定 runs_root 的共享 WorkStore；不立即创建状态或运行模型。"""
        self.store = WorkStore(runs_root)  # 与作业和会话删除共享事务事实源。

    def get(self, run_id: str, db=None) -> dict | None:
        """读取 run_id 的独立控制状态；缺失返回 None，db=None 自建只读事务。"""
        if db is None:
            if not self.store.path.exists():
                return None
            with self.store.transaction() as connection:
                return self.get(run_id, connection)
        row = db.execute("SELECT body FROM runtime_controls WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, state: dict, db) -> None:
        """在调用方 db 事务内提交 state 及控制事件；返回 None，不追加 RunStore 历史。"""
        db.execute("INSERT OR REPLACE INTO runtime_controls VALUES (?,?)", (state["run_id"], encode(state)))
        self.store.event(db, state["session_id"], {"type": "runtime_control_updated", "run_id": state["run_id"]})

    def create(self, context) -> dict:
        """为 context 排他建立初始状态；已有记录拒绝重新绑定，返回已保存状态。"""
        info = context.manifest.task.workspace.stat()
        state = {"run_id": str(context.manifest.run_id), "session_id": str(context.manifest.session_id or context.manifest.run_id),
                 "status": "running", "workspace_identity": [info.st_dev, info.st_ino],
                 "messages": [], "requests": {}, "checkpoint": None, "finished_at": None}
        with self.store.transaction() as db:
            if self.get(state["run_id"], db) is not None:
                raise TaskError("runtime_already_bound", "该运行的控制状态已存在，不能重新启动。")
            resume = context.manifest.execution_resume
            crash = context.manifest.crash_resume
            source = self.get(str(context.manifest.parent_run_id), db) if context.manifest.parent_run_id else None
            if resume is not None and (source is None or source["status"] != "paused"):
                raise TaskError("execution_not_paused", "暂停来源无法核对，不能恢复。")
            if crash is not None and (source is None or source["status"] != "running"
                    or crash.workspace_identity != state["workspace_identity"]
                    or source["workspace_identity"] != state["workspace_identity"]):
                raise TaskError("recovery_control_unverified", "中断来源的工作区或控制状态发生变化。")
            if source is not None:
                for item in source["messages"]:
                    carry = item["delivery"] == "next" and item["status"] == "received" and item["job_id"] is None
                    carry = carry or (resume is not None and item["delivery"] == "steer" and item["status"] in {"received", "delivered"})
                    if carry:
                        value = deepcopy(item)
                        value.update(status="received", consumers={})
                        state["messages"].append(value)
                        item.update(status="forwarded", forwarded_run_id=state["run_id"])
                if crash is not None:
                    source["status"] = "finished"  # 冻结旧收件箱，原运行日志仍显示中断。
                self.save(source, db)  # 待执行下一轮随会话接续展示，用户仍可在最新段撤回。
            self.save(state, db)
        return state

    def input(self, run_id: str, request: RuntimeInput) -> dict:
        """持久接纳 request；run_id 必须仍在运行，相同编号异文拒绝，返回消息副本。"""
        if not request.prompt.strip():
            raise TaskError("invalid_prompt", "补充要求不能为空白。")
        digest = payload_digest(request.model_dump(mode="json", exclude={"request_id"}))
        with self.store.transaction() as db:
            state = self.get(run_id, db)
            if state is None:
                raise TaskError("runtime_not_ready", "运行控制尚未就绪。")
            old = next((item for item in state["messages"] if item["id"] == str(request.request_id)), None)
            if old is not None:
                if old["digest"] != digest:
                    raise TaskError("request_conflict", "该输入编号已用于不同内容或投递意图。")
                return deepcopy(old)
            if state["status"] != "running":
                raise TaskError("runtime_not_running", "任务正在暂停或已收尾，请在停止后接续。")
            if len(state["messages"]) >= 100 or sum(len(item["prompt"].encode("utf-8")) for item in state["messages"]) + len(request.prompt.encode("utf-8")) > 256 * 1024:
                raise TaskError("runtime_input_limit", "本轮输入收件箱已满，请等待本轮结束。")
            item = {"id": str(request.request_id), "digest": digest, "prompt": request.prompt.strip(),
                    "delivery": request.delivery, "status": "received", "created_at": now(), "consumers": {},
                    "job_id": None, "error": None}
            state["messages"].append(item)
            self.save(state, db)
            return deepcopy(item)

    def pause(self, run_id: str, request: RuntimeAction) -> dict:
        """幂等请求 run_id 暂停；只改变调度意图，不宣称活动进程已经停下。"""
        with self.store.transaction() as db:
            state = self.get(run_id, db)
            if state is None:
                raise TaskError("runtime_not_ready", "运行控制尚未就绪。")
            key = str(request.request_id)
            if key in state["requests"]:
                return state
            if state["status"] not in {"running", "pausing"}:
                raise TaskError("runtime_not_running", "运行已停止，不能再次请求暂停。")
            if len(state["requests"]) >= 100:
                raise TaskError("runtime_input_limit", "本轮控制请求已达到上限。")
            state["requests"][key] = "pause"
            state["status"] = "pausing"
            self.save(state, db)
            return state

    def retract(self, run_id: str, message_id: str) -> dict:
        """撤回 run_id 尚未被任何执行段读取的 message_id；已投递或已入队拒绝。"""
        with self.store.transaction() as db:
            state = self.get(run_id, db)
            if state is None:
                raise TaskError("runtime_not_ready", "运行控制尚未就绪。")
            item = next((value for value in state["messages"] if value["id"] == message_id), None)
            if item is None:
                raise TaskError("runtime_input_not_found", "该输入不属于本轮。")
            if item["status"] == "retracted":
                return item
            if item["consumers"] or item["job_id"]:
                raise TaskError("runtime_input_delivered", "输入已经开始交付，不能撤回已生效的要求。")
            item["status"] = "retracted"
            self.save(state, db)
            return deepcopy(item)

    def finish(self, context) -> None:
        """在 context 所有子项、命令及检查收尾后固定控制状态；不改写已保存 Run 结果。"""
        result = context.result
        with self.store.transaction() as db:
            state = self.get(str(context.manifest.run_id), db)
            safe = result.cleanup_status in {"complete", "not_needed"}
            state["status"] = "paused" if result.stop_reason == "user_paused" and safe else "finished" if safe else "needs_review"
            if state["status"] == "paused":
                prior = result.permissions
                state["checkpoint"] = {"source_run_id": state["run_id"], "workspace_identity": state["workspace_identity"],
                    "spent_model_turns": prior.spent_model_turns + result.model_turns,
                    "spent_tool_calls": prior.spent_tool_calls + result.tool_calls,
                    "spent_seconds": prior.spent_seconds + result.active_duration_seconds,
                    "spent_usage": {key: prior.spent_usage.get(key, 0) + value for key, value in result.usage.model_dump().items()},
                    "spent_usage_complete": prior.spent_usage_complete and result.usage_complete,
                    "repair_attempts": result.repair_attempts, "delegation_used": context.delegation_used}
                context.emit("execution_checkpoint", {"checkpoint": state["checkpoint"]})
            state["finished_at"] = now()
            self.save(state, db)


class RuntimeControl:
    """一个执行段的安全边界控制；子项读取根收件箱，但以自己的日志记录实际交付。"""

    def __init__(self, context):
        """绑定 context；根段创建状态，子段继承根归属，构造不启动工具或模型。"""
        self.context = context  # 当前实际模型及工具日志归属。
        self.root = context.budget_parent or context  # 用户操作始终针对主 Run。
        self.store = ControlStore(context.runs_root)  # 持久输入与暂停意图。
        self.owner = str(context.manifest.run_id)  # 消费游标按主/子执行独立确认。
        self.run_id = str(self.root.manifest.run_id)  # 收件箱固定为根运行。
        self.pending: list[str] = []  # 已注入但尚未收到真实模型响应的输入编号。
        if context.budget_parent is None:
            self.store.create(context)

    def pausing(self) -> bool:
        """返回根段是否已接到暂停请求；只读持久状态，不以模型文本推断。"""
        state = self.store.get(self.run_id)
        return state is not None and state["status"] == "pausing"

    def available_inputs(self) -> list[dict]:
        """返回当前 owner 尚未投递的本轮补充；下一轮消息及已撤回输入不进入当前模型。"""
        state = self.store.get(self.run_id)
        return [item for item in state["messages"] if item["delivery"] == "steer" and item["status"] != "retracted"
                and self.owner not in item["consumers"]]

    def deliver(self, messages: list[dict]) -> bool:
        """向完整配对 messages 注入补充并保存待确认游标；返回是否有新输入，不代表模型已处理。"""
        values = self.available_inputs()
        if not values:
            return False
        message = {"role": "user", "content": "[用户在本轮执行中补充；作用于后续决策，不代替计划批准或动作审批。]\n" + "\n\n".join(item["prompt"] for item in values)}
        self.context.emit("runtime_input_delivered", {"ids": [item["id"] for item in values], "message": message})
        with self.store.store.transaction() as db:
            state = self.store.get(self.run_id, db)
            for item in state["messages"]:
                if item["id"] in {value["id"] for value in values}:
                    item["consumers"][self.owner] = "delivered"
                    self.pending.append(item["id"])
                    if self.owner == self.run_id:
                        item["status"] = "delivered"
            self.store.save(state, db)
        messages.append(message)
        return True

    def acknowledge(self) -> None:
        """收到已落盘真实模型响应后确认本 owner 的 pending 输入；请求失败不误标处理完成。"""
        if not self.pending:
            return
        with self.store.store.transaction() as db:
            state = self.store.get(self.run_id, db)
            for item in state["messages"]:
                if item["id"] in self.pending:
                    item["consumers"][self.owner] = "processed"
                    if self.owner == self.run_id:
                        item["status"] = "processed"
            self.store.save(state, db)
        self.pending.clear()

    async def boundary(self, executor) -> None:
        """安全边界响应暂停；等待已有命令和子项收尾后抛 AgentStopped，返回时无新增副作用。"""
        if not self.pausing():
            return
        from agent_harness.runtime.runner import AgentStopped
        if self.context.command_controller is not None:
            await self.context.command_controller.settle()
        controller = getattr(executor, "subagents", None)
        if controller is not None:
            controller.pause_requested = True
            await controller.close()
            # 已出现的子审批保留原决定链，不能被暂停恢复入口绕过。
            controller.check_errors()
        self.context.result.final_answer = "已在安全边界暂停，已有代码与执行证据已保留。"
        self.context.result.validation_status = "unverified"
        self.context.result.verification = None  # 已发生的检查留在 attempts，暂停不能伪装成完成交付。
        raise AgentStopped("waiting_input", "subagent_paused" if self.context.budget_parent is not None else "user_paused")
