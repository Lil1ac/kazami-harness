"""统一执行请求与辅助模型请求的调度、计量和审计。"""

from typing import TYPE_CHECKING

from agent_harness.providers.base import ModelClient, ModelResponse

if TYPE_CHECKING:
    from agent_harness.runtime.runner import RunContext
    from agent_harness.runtime.session import Session


def _collect_usage(context: "RunContext", usage: dict | None) -> None:
    """累加本轮已知的 token 数；任何计数缺失都将本次用量标为不完整。"""
    if usage is None:
        context.result.usage_complete = False
        if context.budget_parent is not None:
            context.budget_parent.result.usage_complete = False
        return
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(field)
        if type(value) is int and value >= 0:
            setattr(context.result.usage, field, getattr(context.result.usage, field) + value)
            if context.budget_parent is not None:
                total = context.budget_parent.result.usage
                setattr(total, field, getattr(total, field) + value)
        else:
            context.result.usage_complete = False
            if context.budget_parent is not None:
                context.budget_parent.result.usage_complete = False


async def request_model(context: "RunContext", client: ModelClient, messages: list[dict], tools: list[dict], purpose: str) -> ModelResponse:
    """统一发送执行/摘要请求，累加模型次数、HTTP与token；返回供应商响应，不执行工具。

    messages/tools必须先经上下文检查；purpose为execution或summary。达到总模型上限
    时在请求前抛AgentStopped；供应商超时和错误原样交给Runner。摘要也受总截止时间约束。
    """
    context.consume("model")
    context.request_turn = context.result.model_turns
    context.request_purpose = purpose
    context.emit("model_request_started", {
        "model_turn": context.request_turn,
        "provider": context.manifest.config.provider,
        "requested_model": context.manifest.config.requested_model,
        "message_count": len(messages), "purpose": purpose,
    })
    from agent_harness.runtime.runner import REQUEST_CONTEXT
    token = REQUEST_CONTEXT.set(context)
    try:
        from agent_harness.execution.scheduling import capacity
        async with capacity("models"):
            response = await client.complete(messages, tools)
    finally:
        REQUEST_CONTEXT.reset(token)
    _collect_usage(context, response.usage)
    if purpose == "execution":
        context.result.response_model = response.response_model
    context.emit("model_response", {
        "model_turn": context.request_turn, "response_id": response.response_id,
        "provider": context.manifest.config.provider,
        "response_model": response.response_model, "system_fingerprint": response.system_fingerprint,
        "finish_reason": response.finish_reason, "usage": response.usage, "purpose": purpose,
    })
    return response


async def execute_model(session: "Session") -> ModelResponse:
    """检查最终投影和剩余次数，再返回实际执行响应；压缩可能已经消费本段预算。"""
    from agent_harness.runtime.runner import AgentStopped

    context, prepared = session.context, session.turn.input
    if prepared is None:
        raise RuntimeError("模型输入尚未投影")
    prepared = session.builder.freeze_projection(prepared, context.result.model_turns + 1,
                                                 allow_tools=not session.turn.handoff)
    session.turn.input = prepared  # 以最后一次检查对应的副本校准实际用量。
    context.emit("context_checked", prepared.check.model_dump(mode="json"))
    if not prepared.check.allowed:
        reason = "max_model_input_bytes" if prepared.check.input_bytes > prepared.check.max_input_bytes else "max_model_input_tokens"
        raise AgentStopped("limit_exceeded", reason)
    if context.available("model_turns") <= 0:
        raise AgentStopped("limit_exceeded", "max_model_turns")
    session.builder.memory.record_use(context.result.model_turns + 1, prepared.messages)
    return await request_model(context, session.client, prepared.messages, prepared.tools, "execution")


def commit_response(session: "Session", response: ModelResponse) -> None:
    """保存 response 的用量投影和完整消息；保留调用编号，后续由工具批次配齐回执。"""
    prepared = session.turn.input
    if prepared is None:
        raise RuntimeError("模型响应缺少实际输入")
    session.builder.observe_usage(prepared.check.input_bytes, response.usage)
    session.append(response.message, "assistant_message", model_turn=session.context.request_turn)
