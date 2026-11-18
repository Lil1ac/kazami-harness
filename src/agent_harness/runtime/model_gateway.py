"""统一执行请求与辅助模型请求的调度、计量和审计。"""

import asyncio
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Protocol

from agent_harness.execution.scheduling import capacity
from agent_harness.providers.base import ModelClient, ModelResponse, ModelError
from agent_harness.providers.chat import ChatCompletionsClient
from agent_harness.runtime.handoff import recheck_verified_handoff_request, start_verified_root_handoff

if TYPE_CHECKING:
    from agent_harness.runtime.runner import RunContext
    from agent_harness.runtime.session import Session


class ModelRequestGuard(Protocol):
    """为宿主评测在执行和摘要共用的入口接纳请求与核对回执。"""

    def authorize(self, run_id: str, turn: int, purpose: str) -> None:
        """为 run_id 的第 turn 轮保存预留；purpose区分执行和摘要，拒绝时抛AgentStopped。"""
        ...

    def settle(self, run_id: str, turn: int, usage: dict | None, model: str | None,
               error_code: str | None = None) -> str | None:
        """结算 run_id/turn 对应回执。usage/model缺失保留预留；返回立即阻断执行的原因或None。"""
        ...

    def release_not_sent(self, run_id: str, turn: int, error_code: str) -> None:
        """仅在 run_id/turn 明确未向供应商发送时释放预留，保存 error_code 供审计。"""
        ...


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


async def request_model(context: "RunContext", client: ModelClient, messages: list[dict], tools: list[dict], purpose: str,
                        *, input_check_seq: int | None = None,
                        before_reserve: Callable[[], None] | None = None) -> ModelResponse:
    """通过容量与截止时间门禁发送已投影的 messages/tools，返回计量后的响应。

    purpose 为 execution 或 summary，input_check_seq 是执行请求对应的放行事件序号，None 用于未关联的辅助请求。
    before_reserve 在获得模型槽位后、记账前执行可选的状态复核；None 表示无需额外复核。
    仅实际可能发出的请求缺少可信回执时标记用量未知。
    仓库评测的预留在进入供应商前持久化；未发送时释放，已尝试未结算时保留。
    """
    from agent_harness.runtime.runner import AgentStopped, REQUEST_CONTEXT

    guard = context.model_request_guard
    run_id = str(context.manifest.run_id)
    tracks_attempts = isinstance(client, ChatCompletionsClient) and client.tracks_http_attempts

    def check_deadline() -> None:
        """超过所属 Run 的绝对截止时间时拒绝新请求，返回 None 表示仍可发送。"""
        if asyncio.get_running_loop().time() >= context.deadline:
            raise AgentStopped("timed_out", "run_timeout")

    check_deadline()
    async with capacity("models"):
        check_deadline()
        if before_reserve is not None:
            before_reserve()
            check_deadline()
        turn = context.result.model_turns + 1
        if guard is not None:
            if context.available("model_turns") <= 0:
                raise AgentStopped("limit_exceeded", "max_model_turns")
            guard.authorize(run_id, turn, purpose)
        context.request_attempted = False
        entered = False  # 注入客户端无发送回调时，只在进入其 complete 后保守计量。
        token = REQUEST_CONTEXT.set(context)

        def settle_error(usage: dict | None, model: str | None, code: str) -> None:
            """用有效回执或真实发送边界结算本轮；仅已接纳且明确未发送时释放预留。"""
            if usage is not None or context.request_attempted or (entered and not tracks_attempts):
                _collect_usage(context, usage)
                if guard is not None:
                    guard.settle(run_id, turn, usage, model, code)
            elif guard is not None:
                guard.release_not_sent(run_id, turn, code)

        try:
            context.consume("model")
            context.request_turn = turn
            context.request_purpose = purpose
            context.emit("model_request_started", {
                "model_turn": turn, "provider": context.manifest.config.provider,
                "requested_model": context.manifest.config.requested_model,
                "message_count": len(messages), "purpose": purpose,
                **({"input_check_seq": input_check_seq} if input_check_seq is not None else {}),
            })
            check_deadline()  # 同步保存账本和事件也消耗截止时间。
            entered = True
            response = await client.complete(messages, tools)
        except ModelError as error:
            settle_error(error.usage, error.response_model, error.code)
            error.usage_accounted = True
            if purpose == "execution" and error.response_model:
                context.result.response_model = error.response_model
            if error.usage is not None:
                context.emit("model_response", {"model_turn": turn, "response_id": None,
                    "provider": context.manifest.config.provider, "response_model": error.response_model,
                    "system_fingerprint": None, "finish_reason": "length" if error.code == "model_output_truncated" else "error",
                    "usage": error.usage, "purpose": purpose})
            raise
        except (asyncio.CancelledError, Exception):
            settle_error(None, None, "model_interrupted")
            raise
        finally:
            REQUEST_CONTEXT.reset(token)

        # 在归还模型槽位前核销已收到的回执，之后的工具取消不会改写本轮计量。
        _collect_usage(context, response.usage)
        if purpose == "execution":
            context.result.response_model = response.response_model
        reason = guard.settle(run_id, turn, response.usage, response.response_model) if guard is not None else None
        context.emit("model_response", {
            "model_turn": turn, "response_id": response.response_id,
            "provider": context.manifest.config.provider,
            "response_model": response.response_model, "system_fingerprint": response.system_fingerprint,
            "finish_reason": response.finish_reason, "usage": response.usage, "purpose": purpose,
        })
        if reason:
            raise AgentStopped("failed", reason)
        return response

async def execute_model(session: "Session") -> ModelResponse:
    """检查最终投影和剩余次数，再返回实际执行响应；压缩可能已经消费本段预算。"""
    from agent_harness.runtime.runner import AgentStopped

    context, turn = session.context, session.turn

    def check_input() -> int:
        """核对当前输入，持久化普通或收尾的检查；返回事件序号供实际发单关联。"""
        if turn.input is None:
            raise RuntimeError("模型输入尚未投影")
        turn.input = session.builder.freeze_projection(turn.input, context.result.model_turns + 1,
                                                       allow_tools=not turn.handoff)
        phase = "handoff" if turn.handoff else "execution"
        context.emit("context_checked", {**turn.input.check.model_dump(mode="json"), "phase": phase})
        return context.event_count

    input_check_seq = check_input()
    if not turn.input.check.allowed:
        original = turn.input.check
        reason = "max_model_input_bytes" if original.input_bytes > original.max_input_bytes else "max_model_input_tokens"
        policy = context.manifest.config.context
        if (turn.handoff or turn.delegated or context.manifest.delegation is not None
                or not policy.root_handoff or not policy.root_handoff_after_input_rejection
                or not getattr(session.executor, "public_self_test_available", False)
                or not await start_verified_root_handoff(session, "input_rejected", reason)):
            raise AgentStopped("limit_exceeded", reason)
        # 尚在发单及费用预留之前。紧凑交接必须再次经过完全相同的门禁。
        from agent_harness.runtime.input_steps import handoff_input

        await handoff_input(session)
        input_check_seq = check_input()
        if not turn.input.check.allowed:
            raise AgentStopped("limit_exceeded", reason)
    if context.available("model_turns") <= 0:
        raise AgentStopped("limit_exceeded", "max_model_turns")
    session.builder.memory.record_use(context.result.model_turns + 1, turn.input.messages)
    verified_handoff = turn.handoff and (turn.root_handoff_facts or {}).get("public_self_test") is not None
    return await request_model(context, session.client, turn.input.messages, turn.input.tools, "execution",
                               input_check_seq=input_check_seq,
                               before_reserve=partial(recheck_verified_handoff_request, session) if verified_handoff else None)


def commit_response(session: "Session", response: ModelResponse) -> None:
    """保存 response 的用量投影和完整消息；保留调用编号，后续由工具批次配齐回执。"""
    prepared = session.turn.input
    if prepared is None:
        raise RuntimeError("模型响应缺少实际输入")
    session.builder.observe_usage(prepared.check.input_bytes, response.usage)
    session.append(response.message, "assistant_message", model_turn=session.context.request_turn)
    session.turn.response_seq = session.context.event_count
