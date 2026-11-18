"""执行内核只编排阶段、模型和工具批次。"""

import asyncio
from typing import TYPE_CHECKING

from agent_harness.context.builder import ContextBuilder
from agent_harness.providers.base import ModelClient
from agent_harness.runtime.assembly import assemble
from agent_harness.runtime.completion import finish_candidate
from agent_harness.runtime.environment import execution_environment
from agent_harness.runtime.handoff import finish_root_handoff
from agent_harness.runtime.model_gateway import commit_response, execute_model
from agent_harness.runtime.pipeline import Finish, Pipeline, Stage, apply_decision
from agent_harness.runtime.runner import AgentStopped
from agent_harness.runtime.session import Session, Turn, prepare_session
from agent_harness.runtime.tool_batch import execute_batch
from agent_harness.tools.registry import ToolExecutor

if TYPE_CHECKING:
    from agent_harness.runtime.runner import RunContext
    from agent_harness.runtime.verifier import Verifier


async def _run_agent(session: Session, pipeline: Pipeline) -> None:
    """按 pipeline 执行 session；正常交付返回 None，停止与中断交由宿主收尾。"""
    context = session.context
    startup = await pipeline.run(Stage.START, session)
    if isinstance(startup, Finish):
        if not apply_decision(session, await finish_candidate(session, pipeline)):
            return
    else:
        apply_decision(session, startup)

    previous_delivery_feedback = None  # 同一次未解决的入口问题只主动退回一次，避免循环催促。
    while context.available("model_turns") > 0:
        await asyncio.sleep(0)  # 本地组件可能立即返回，模型请求前仍要响应取消。
        session.turn = Turn()
        apply_decision(session, await pipeline.run(Stage.INPUT, session))
        response = await execute_model(session)
        session.turn.response = response
        apply_decision(session, await pipeline.run(Stage.RESPONSE, session))
        commit_response(session, response)

        if response.tool_calls:
            apply_decision(session, await execute_batch(session, pipeline, response.tool_calls))
            apply_decision(session, await pipeline.run(Stage.BATCH_END, session))
            check_progress = getattr(session.executor, "progress_feedback", None)
            if check_progress and context.available("model_turns") > 0:
                feedback = await check_progress(context.available("model_turns"))
                if feedback:
                    session.append({"role": "user", "content": feedback}, "pipeline_feedback")
            continue

        if apply_decision(session, await pipeline.run(Stage.CANDIDATE, session)):
            continue
        answer = response.message["content"]
        check_delivery = getattr(session.executor, "delivery_feedback", None)
        if check_delivery and not session.turn.handoff and context.available("model_turns") > 0:
            feedback = await check_delivery()
            if feedback and feedback != previous_delivery_feedback:
                previous_delivery_feedback = feedback
                session.append({"role": "user", "content": feedback}, "pipeline_feedback")
                continue
        if session.turn.handoff and session.turn.root_handoff_facts is not None:
            answer = finish_root_handoff(session, answer)
        context.result.final_answer = answer
        if not apply_decision(session, await finish_candidate(session, pipeline)):
            return
    raise AgentStopped("limit_exceeded", "max_model_turns")


async def run_agent(context: "RunContext", client: ModelClient, executor: ToolExecutor, *,
                    verifier: "Verifier | None" = None, inherited_context: ContextBuilder | None = None) -> None:
    """用 context、client 与 executor 执行一个父段或子段，无正常返回值。

    verifier 默认 None，根段无宿主验收；子段始终走角色交接协议。
    inherited_context 默认 None，子段继承固定输入但独立核验历史。
    无论如何退出，宿主都等待当前子执行与后台命令清理。
    """
    delegation = context.manifest.delegation
    pipeline = assemble("root" if delegation is None else delegation.role)
    async with execution_environment(context, executor):
        session = prepare_session(context, client, executor, inherited_context, verifier)
        await _run_agent(session, pipeline)
