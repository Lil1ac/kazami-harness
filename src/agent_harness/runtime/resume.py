"""接续已核验的宿主决定及审批动作。"""

import asyncio
import json

from agent_harness.providers.base import ToolCall
from agent_harness.runtime.pipeline import Abort, Decision, Finish, Proceed, Wait
from agent_harness.runtime.receipts import record_tool, skipped
from agent_harness.runtime.runner import AgentStopped
from agent_harness.runtime.session import Session
from agent_harness.runtime.tool_batch import ToolStep, interrupted_result, invoke_tool
from agent_harness.storage.runs import StorageError
from agent_harness.work.tasks import TaskError


async def apply_human_decision(session: Session) -> Proceed | Abort:
    """已有拒绝或取消在持久历史中结束本段；通过决定继续宿主门禁。"""
    context = session.context
    decision = context.manifest.human_decision
    if decision is not None and decision.action == "cancel":
        context.result.final_answer = "已取消本次等待，不会自动恢复执行。"
        return Abort("cancelled", "interaction_cancelled")
    if decision is not None and decision.action == "reject":
        context.result.final_answer = "已拒绝这一版计划，项目执行保持停止。"
        return Abort("completed", "plan_rejected")
    return Proceed()


async def restore_projection(session: Session) -> Proceed | Abort:
    """初始历史落盘后核验技能并继承可复用摘要；失败时保留原任务供接续。"""
    if session.skill_error is not None:
        return Abort("failed", session.skill_error.code)
    if session.compression_parent is not None:
        session.compressor.inherit(session.compression_parent, session.messages, session.executor.definitions)
    return Proceed()


async def replay_tool(session: Session, action: dict, hook_action: dict | None) -> Decision:
    """恢复 action 指定的原工具并保存唯一配对回执；保留原额度及脚本执行检查点。"""
    context, executor = session.context, session.executor
    origin = hook_action["origin"] if hook_action is not None else action.get("hook_operation")
    if origin is not None:
        executor.hooks.resume_operation = origin
    name = origin["name"] if origin is not None else action["name"]
    arguments = origin["arguments_json"] if origin is not None else json.dumps(action["arguments"], ensure_ascii=False)
    call = ToolCall("resume_" + action["id"], name, arguments)
    step = ToolStep(call)
    session.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": call.call_id, "type": "function", "function": {"name": name, "arguments": arguments}},
    ]}, "assistant_message", source="host_action_resume", action_id=action["id"])
    try:
        result = await invoke_tool(session, step, action_id=action["id"])
        record_tool(context, call, result, session.messages)
    except (asyncio.CancelledError, TimeoutError) as error:
        result = interrupted_result(session, step, deadline_reached=isinstance(error, TimeoutError))
        record_tool(context, call, result, session.messages)
        if step.started and getattr(executor, "cancelled_result", None) is not None:
            executor.permissions.finish(action, False)
        raise
    except StorageError:
        raise
    except Exception:
        record_tool(context, call, skipped("tool_internal_error" if step.started else "skipped_error"), session.messages)
        raise
    if hook_action is None:
        executor.permissions.finish(action, bool(result.get("ok")) or bool((result.get("data") or {}).get("tool_executed")))
    code = (result.get("error") or {}).get("code")
    if code == "approval_required":
        return Wait("action_approval")
    if not result.get("ok"):
        return Abort("failed", code or "permission_action_failed")
    if context.result.cleanup_status in {"unknown", "incomplete"}:
        return Abort("failed", "process_cleanup_failed")
    return Proceed()


async def resume_action(session: Session) -> Decision:
    """认领当前段的审批动作；分流原事件、原工具或必需验收流水线。"""
    context, executor = session.context, session.executor
    resume = context.manifest.permission_resume
    if resume is None or resume.child_run_id is not None:
        return Proceed()
    permissions = executor.permissions
    try:
        if resume.decision == "approve" and context.deadline <= asyncio.get_running_loop().time():
            permissions.store.invalidate(resume.action_id, "run_timeout")
            return Abort("timed_out", "run_timeout")
        action = permissions.prepare_resume()
        if resume.decision != "approve":
            context.result.final_answer = "已拒绝本次动作。" if resume.decision == "reject" else "已取消动作等待。"
            return Abort("cancelled", "permission_rejected" if resume.decision == "reject" else "permission_cancelled")
        context.emit("permission_resuming", {"action_id": action["id"], "source_run_id": action["source_run_id"]})
        hook_action = action.get("hook")
        if hook_action is not None and hook_action["origin"]["kind"] == "event":
            context.result.final_answer = hook_action.get("final_answer")
            await executor.hooks.dispatch(hook_action["event"], hook_action["event_key"], hook_action["payload"],
                                          origin=hook_action["origin"])
            return Finish() if hook_action["event"] == "Stop" else Proceed()
        if action["name"] == "verification":
            context.result.final_answer = action["final_answer"]
            session.completion_action = action
            return Finish()
        return await replay_tool(session, action, hook_action)
    except TaskError as error:
        permissions.store.invalidate(resume.action_id, error.code)
        context.result.final_answer = error.message
        return Abort("failed", error.code)


def finish_resumed_action(session: Session) -> None:
    """保存已批准验收动作的结果；无接续动作时不写入权限记录。"""
    action = session.completion_action
    if action is not None:
        detail = session.verification_detail
        session.executor.permissions.finish(action, detail is not None and detail.status == "passed")
        session.completion_action = None
