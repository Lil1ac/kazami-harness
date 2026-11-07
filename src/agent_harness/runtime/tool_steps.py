"""把工具结果中的业务信号翻译为统一阶段决定。"""

from agent_harness.hooks.controller import HALT_HOOK_ERRORS
from agent_harness.runtime.pipeline import Abort, Proceed, Replan, Wait
from agent_harness.runtime.session import Session


# 不确定的外部结果与失效的固定声明都不能让同批后续工具继续。
MCP_HALT_ERRORS = frozenset({"mcp_result_unknown", "mcp_definition_changed", "mcp_selection_stale"})


async def guard_tool_result(session: Session) -> Proceed | Wait | Abort:
    """审批、脚本和 MCP 结果不确定或进程清理失败时，禁止继续调度工具。"""
    step = session.tool
    if step is None:
        raise RuntimeError("工具结果阶段缺少回执")
    result = session.context.result
    error = step.result.get("error") or {}
    code = error.get("code")
    if code == "approval_required":
        executed = (step.result.get("data") or {}).get("tool_executed")
        result.final_answer = ("原工具已执行，Hook 后处理等待批准；后续操作已停止。" if executed else
                               "有待执行动作需要你批准，当前动作及同批后续操作尚未执行。")
        return Wait("action_approval", "skipped_action_approval")
    if code in HALT_HOOK_ERRORS or code in MCP_HALT_ERRORS:
        result.final_answer = error["message"]
        return Abort("failed", code, "skipped_hook_failure")
    if result.cleanup_status in {"unknown", "incomplete"}:
        return Abort("failed", "process_cleanup_failed", "skipped_cleanup")
    return Proceed()


async def review_memory_change(session: Session) -> Proceed | Replan:
    """项目约定变化后要求重新生成剩余参数，防止继续沿用旧规则。"""
    step = session.tool
    if step is not None and (step.result.get("error") or {}).get("code") == "memory_context_updated":
        return Replan(skip_code="memory_context_updated",
                      skip_message="尚未执行：项目约定已更新，请阅读后重新决定本批工具参数。")
    return Proceed()


async def capture_planning(session: Session) -> Proceed | Wait:
    """保存宿主规划事实，等待问题或计划批准时跳过剩余工具。"""
    planning = getattr(session.executor, "planning", None)
    if planning is not None:
        result = session.context.result
        result.planning = planning.state.model_copy(deep=True)
        pending = planning.state.pending
        if pending is not None:
            result.final_answer = pending.question if pending.kind == "question" else "已提交计划，等待你审阅并批准。"
            return Wait("user_question" if pending.kind == "question" else "plan_approval", "skipped_user_input")
    return Proceed()
