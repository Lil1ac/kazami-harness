"""选择当前记忆、压缩历史并构造只读交接输入。"""

from functools import partial

from agent_harness.runtime.model_gateway import request_model
from agent_harness.runtime.pipeline import Proceed
from agent_harness.runtime.session import Session


async def select_memory(session: Session) -> Proceed:
    """按规划、执行或修复阶段选择记忆，超出输入上限时按整条裁剪。"""
    context, builder, executor = session.context, session.builder, session.executor
    planning = getattr(executor, "planning", None)
    if context.manifest.delegation is not None:
        phase = builder.memory.phase
    elif planning is not None and planning.state.phase == "planning":
        phase = "planning"
    else:
        phase = "verification" if context.result.repair_attempts else "execution"
    executor.memory_phase = phase  # 变更门禁沿用本轮真实阶段，不依赖展示事件覆盖的阶段。
    builder.memory.select(phase)
    while builder.memory.selected and not builder.build(session.messages, executor.definitions, context.result.model_turns + 1).check.allowed:
        removed = builder.memory.selected.pop()
        builder.memory.skipped.append({"id": removed["id"], "title": removed["title"], "reason": "整体输入剩余预算不足，可搜索回读"})
    return Proceed()


async def compress_input(session: Session) -> Proceed:
    """投影原始历史，辅助模型请求沿用统一网关并按需为交接留一轮。"""
    session.turn.input = await session.compressor.prepare(
        session.messages, session.executor.definitions,
        partial(request_model, session.context, session.client),
        reserve_model_turns=1 if session.turn.delegated else 0,
    )
    return Proceed()


async def handoff_input(session: Session) -> Proceed:
    """交接模式只投影总结说明和空工具表，完整历史及压缩索引保持原样。"""
    turn, context = session.turn, session.context
    if turn.handoff:
        prepared = turn.input
        if prepared is None:
            raise RuntimeError("交接前缺少压缩投影")
        notice = {"role": "system", "content": (
            "宿主收尾交接：本次只总结已有信息，禁止新工具调用；不得为写出结论而编造完成或检查通过。"
            + ("请按角色JSON格式交接；研究未完成时complete=false，列明限制与未完成事项。" if context.manifest.delegation is not None
               else "请明确已交付内容、失败及未完成事项，等待宿主按真实代码独立检查。"))}
        turn.input = session.builder.build([*prepared.messages, notice], [], context.result.model_turns + 1,
                                           raw_input_bytes=prepared.check.raw_input_bytes)
        limits = context.manifest.config.limits
        context.emit("budget_handoff_started", {"message": notice, "remaining_model_turns": limits.max_model_turns - context.result.model_turns,
                                                "remaining_tool_calls": limits.max_tool_calls - context.result.tool_calls})
    return Proceed()
