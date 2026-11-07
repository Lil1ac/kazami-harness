"""保证整批预算、权限执行入口与逐项回执配对。"""

import asyncio
from dataclasses import dataclass, field

from agent_harness.providers.base import ToolCall
from agent_harness.runtime.pipeline import Abort, Decision, Pipeline, Proceed, Replan, Stage
from agent_harness.runtime.receipts import record_tool, skipped
from agent_harness.runtime.runner import AgentStopped
from agent_harness.runtime.session import Session
from agent_harness.storage.runs import StorageError
from agent_harness.tools.registry import ToolExecutor


@dataclass
class ToolStep:
    """保留一个正在处理的调用及后续批次位置。"""

    call: ToolCall  # 模型或已批准接续指定的唯一调用。
    next_call: ToolCall | None = None  # 同批紧邻后续调用；最后一项时为空。
    remaining: int = 0  # 当前调用之后尚未处理的项目数。
    started: bool = False  # 真实工具边界已通过参数和权限检查。
    result: dict = field(default_factory=dict)  # 工具实际返回值；执行前为空。


async def invoke_tool(session: Session, step: ToolStep, *, action_id: str | None = None) -> dict:
    """执行 step 指定的调用并返回结果；action_id 非空时关联已批准接续动作。

    执行入口统一负责权限和工作区检查，只有实际通过门禁后才记录 tool_started。
    调用方负责消费额度或核验既有动作，并为失败与取消提交配对回执。
    """
    context, executor, call = session.context, session.executor, step.call

    def tool_started() -> None:
        """在执行器通知实际启动时记录调用事实；无返回值，影响取消回执语义。"""
        fields = {"action_id": action_id} if action_id is not None else {}
        context.emit("tool_started", {"call_id": call.call_id, "name": call.name,
                                     "arguments_json": call.arguments_json, **fields,
                                     "title": executor.external_title(call.name) if isinstance(executor, ToolExecutor) else None})
        step.started = True

    if isinstance(executor, ToolExecutor):
        executor.call_id = call.call_id
        executor.before_execute = tool_started
    else:
        tool_started()  # 注入的测试执行器没有门禁回调，调用前按相同事件契约记录。
    try:
        return await executor.execute(call.name, call.arguments_json)
    finally:
        if isinstance(executor, ToolExecutor):
            executor.before_execute = None


def interrupted_result(session: Session, step: ToolStep | None) -> dict:
    """构造取消时的当前或未执行结果；已有真实进程回执优先保留。"""
    timeout = asyncio.get_running_loop().time() >= session.context.deadline
    if step is not None and step.started:
        actual = getattr(session.executor, "cancelled_result", None)
        return actual if actual is not None else skipped("tool_timed_out" if timeout else "tool_cancelled")
    return skipped("skipped_timeout" if timeout else "skipped_cancelled")


async def execute_batch(session: Session, pipeline: Pipeline, batch: list[ToolCall]) -> Decision:
    """执行完整 batch；返回阶段决定，重决策、异常与取消前先为其余调用配齐回执。"""
    context = session.context
    next_index = 0  # 指向第一条尚未保存回执的调用。
    step: ToolStep | None = None

    def commit(result: dict) -> None:
        """为当前未配对调用保存 result，成功后推进索引，避免异常路径产生重复回执。"""
        nonlocal next_index
        record_tool(context, batch[next_index], result, session.messages)
        next_index += 1

    def fill(decision: Decision) -> None:
        """按 decision 为未执行项批量保存原因；未声明原因时使用统一跳过说明。"""
        code = getattr(decision, "skip_code", None) or "skipped_stage"
        message = decision.skip_message if isinstance(decision, Replan) else None
        while next_index < len(batch):
            commit(skipped(code, message))

    if session.turn.handoff:
        blocked = Abort("limit_exceeded", "handoff_tools_disabled", "handoff_tools_disabled")
        fill(blocked)
        return blocked
    if len(batch) > context.available("tool_calls"):
        blocked = Abort("limit_exceeded", "max_tool_calls", "skipped_limit")
        fill(blocked)
        return blocked
    try:
        while next_index < len(batch):
            remaining = len(batch) - next_index - 1
            step = ToolStep(batch[next_index], batch[next_index + 1] if remaining else None, remaining)
            session.tool = step
            decision = await pipeline.run(Stage.BEFORE_TOOL, session)
            if not isinstance(decision, Proceed):
                fill(decision)
                return decision
            await asyncio.sleep(0)  # 即使都是同步完成的本地调用，也在下次副作用前响应取消。
            if context.available("tool_calls") <= 0:
                blocked = Abort("limit_exceeded", "max_tool_calls", "skipped_limit")
                fill(blocked)
                return blocked
            context.consume("tool")
            context.emit("tool_requested", {"call_id": step.call.call_id, "name": step.call.name})
            step.result = await invoke_tool(session, step)
            commit(step.result)
            decision = await pipeline.run(Stage.AFTER_TOOL, session)
            if not isinstance(decision, Proceed):
                fill(decision)
                return decision
        return Proceed()
    except asyncio.CancelledError:
        if next_index < len(batch):
            commit(interrupted_result(session, step if step is not None and step.call is batch[next_index] else None))
        while next_index < len(batch):
            commit(interrupted_result(session, None))
        raise
    except StorageError:
        raise  # 存储故障后没有可信写入者，不补造配对事件。
    except Exception as error:
        if next_index < len(batch) and step is not None and step.call is batch[next_index] and step.started:
            commit(skipped("tool_internal_error"))
        fill(Abort("failed", "tool_internal_error", "skipped_error"))
        if isinstance(error, AgentStopped):
            raise
        raise AgentStopped("failed", "tool_internal_error") from None
    finally:
        session.tool = None
