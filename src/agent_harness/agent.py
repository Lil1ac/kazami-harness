"""维护本次消息历史，确保每个工具批次配齐结果后才请求下一轮。"""

import asyncio
from collections.abc import Awaitable, Callable
import json
from typing import TYPE_CHECKING

from .context import ContextBuilder, SERIALIZATION
from .compression import ContextCompressor
from .context_archive import ContextArchive
from .providers.base import ModelClient, ModelResponse, ToolCall
from .schemas import ArtifactRef, SkillFailure
from .skills import SkillCatalog, SkillError
from .memory import MemoryController, MemoryError
from .planning import project_plan_history
from .tools.registry import ToolExecutor
from .store import RunStore, StorageError
from .sessions import SessionError, history_messages, payload_digest
from .workspace import Workspace

if TYPE_CHECKING:
    from .runner import RunContext


def _skipped(code: str) -> dict:
    """为未完成或未执行的工具构造统一错误结果，供事件记录和消息配对使用。"""
    return {"ok": False, "data": None, "error": {"code": code, "message": "该工具未能完成，运行已停止。"}, "truncated": False}


def _record_tool(context: "RunContext", call: ToolCall, result: dict, messages: list[dict]) -> None:
    """校验并编码结果后再提交事件，每个调用只留下一个已配对的结果。"""
    if result.get("ok") and call.name not in {"task_create", "task_get", "task_list", "task_update", "memory_propose"}:
        # 回执明确返回可复制的证据引用，模型不必猜测供应商工具编号或当前运行编号。
        result = {**result, "evidence_ref": {"run_id": str(context.manifest.run_id), "tool_call_id": call.call_id}}
    # 命令原始输出引用相对其所属运行，跨轮回读时不能误用新运行目录。
    if context.manifest.config.context.compression != "off" and call.name == "run_command":
        result = {**result, "context_source_run_id": str(context.manifest.run_id)}
    rank = {"not_needed": 0, "complete": 1, "unknown": 2, "incomplete": 3}
    data = result.get("data") or {}
    status = data.get("cleanup_status", "not_needed")
    if status not in rank:
        raise ValueError("工具清理状态无效")
    references = [ArtifactRef.model_validate(raw) for raw in result.get("artifact_refs", [])]
    # 在事件提交前完成所有可能失败的本地转换，避免校验失败后为同一调用写第二条结果。
    content = json.dumps(result, ensure_ascii=False, allow_nan=False)
    context.emit("tool_result", {"call_id": call.call_id, "name": call.name, "result": result})
    if rank[status] > rank[context.result.cleanup_status]:
        context.result.cleanup_status = status
    for reference in references:
        if reference.location == "run" and reference not in context.result.artifacts:
            context.result.artifacts.append(reference)
    messages.append({"role": "tool", "tool_call_id": call.call_id, "content": content})


def _collect_usage(context: "RunContext", usage: dict | None) -> None:
    """累加本轮已知的 token 数；任何计数缺失都将本次用量标为不完整。"""
    if usage is None:
        context.result.usage_complete = False
        return
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(field)
        if type(value) is int and value >= 0:
            setattr(context.result.usage, field, getattr(context.result.usage, field) + value)
        else:
            context.result.usage_complete = False


async def _request_model(context: "RunContext", client: ModelClient, messages: list[dict], tools: list[dict], purpose: str) -> ModelResponse:
    """统一发送执行/摘要请求，累加模型次数、HTTP与token；返回供应商响应，不执行工具。

    messages/tools必须先经上下文检查；purpose为execution或summary。达到总模型上限
    时在请求前抛AgentStopped；供应商超时和错误原样交给Runner。摘要也受总截止时间约束。
    """
    from .runner import AgentStopped

    if context.result.model_turns >= context.manifest.config.limits.max_model_turns:
        raise AgentStopped("limit_exceeded", "max_model_turns")
    context.result.model_turns += 1
    context.emit("model_request_started", {
        "model_turn": context.result.model_turns,
        "requested_model": context.manifest.config.requested_model,
        "message_count": len(messages), "purpose": purpose,
    })
    response = await client.complete(messages, tools)
    _collect_usage(context, response.usage)
    context.emit("model_response", {
        "model_turn": context.result.model_turns, "response_id": response.response_id,
        "response_model": response.response_model, "system_fingerprint": response.system_fingerprint,
        "finish_reason": response.finish_reason, "usage": response.usage, "purpose": purpose,
    })
    return response


async def run_agent(
    context: "RunContext", client: ModelClient, executor: ToolExecutor,
    *, on_completion: Callable[[], Awaitable[str | None]] | None = None,
) -> None:
    """执行多轮模型与工具交互，直接更新 context 中的计数和最终回答。

    client 负责模型通信，executor 负责工具执行；本函数维护消息历史和调用配对。
    同一批工具按响应顺序串行处理，全部写入结果后才进入下一轮模型请求。
    首次冻结项目约定和记忆修订，每轮按实际动作选择记忆并检查压缩投影；摘要同样占模型轮次。
    达到额度或项目约定读取错误时抛出 AgentStopped，取消和存储故障交由 Runner 处理。
    on_completion在无工具回答后执行宿主检查；None返回表示结束，字符串表示已落盘的
    公开测试反馈，追加后沿用同一消息、压缩器、工具执行器和预算继续，绝不重新初始化运行。
    """
    from .runner import AgentStopped

    config = context.manifest.config
    skills = []  # 每轮开始只读取一次；后续模型调用和历史页都使用固定快照。
    skill_error = None  # 加载失败同样落盘受控事实，模型和工具均不执行。
    try:
        skills = SkillCatalog(config.skill_roots).load(config.skills)
    except SkillError as error:
        skill_error = SkillFailure(name=error.name, code=error.code, message=error.message)
    builder = ContextBuilder(
        Workspace(context.manifest.task.workspace, context.store.run_dir.parent),
        context.manifest.task, config, skills=skills,
    )
    try:
        builder.memory = MemoryController(context.store.run_dir.parent, context.manifest, context.emit)
        executor.memory = builder.memory
    except MemoryError as error:
        context.emit("memory_failed", {"code": error.code, "message": error.message})
        raise AgentStopped("failed", error.code) from None
    context.emit("context_prepared", {
        "instruction": builder.instruction.model_dump(mode="json"), "serialization": SERIALIZATION,
        "skills": [item.model_dump(mode="json") for item in builder.skills],
        "skill_error": skill_error.model_dump(mode="json") if skill_error is not None else None,
    })
    if builder.instruction.status == "error":
        raise AgentStopped("failed", builder.instruction.error_code or "project_instructions_unreadable")
    inherited = []
    memory_history_changed = False  # 旧记忆工具被投影后不能复用覆盖旧内容的摘要区间。
    parent = None  # 仅核验后的父记录可提供工作摘要复用来源。
    source = context.manifest.history_source
    if source is not None:
        try:
            if context.manifest.parent_run_id is None:
                raise SessionError("history_unverified", "缺少前轮运行来源。")
            parent = RunStore.read(context.store.run_dir.parent, str(context.manifest.parent_run_id))
            inherited = history_messages(parent, allow_interrupted=source.interrupted)
            if parent.events[-1].seq != source.last_seq or payload_digest(inherited) != source.messages_sha256:
                raise SessionError("history_source_changed", "前轮历史记录发生变化。")
        except (SessionError, StorageError):
            raise AgentStopped("failed", "history_source_changed") from None
        context.emit("history_loaded", {"parent_run_id": str(context.manifest.parent_run_id), "last_seq": source.last_seq, "message_count": len(inherited), "messages_sha256": source.messages_sha256})
        projected = project_plan_history(inherited, frozenset({"memory_search", "memory_get", "memory_propose"}))
        memory_history_changed = projected != inherited
        inherited = projected
        if memory_history_changed:
            context.emit("memory_history_projected", {"reason": "旧轮记忆工具结果由新轮项目记忆替代，旧摘要不直接继承。"})
    archive = ContextArchive(context.store.run_dir, context.manifest)
    compressor = ContextCompressor(context, builder, archive)
    if hasattr(executor, "bind_context") and config.context.compression != "off":
        executor.bind_context(archive)

    async def request_model(messages: list[dict], tools: list[dict], purpose: str) -> ModelResponse:
        """将已检查输入交给当前运行的统一请求入口；返回响应，摘要和执行共用计数/超时。"""
        return await _request_model(context, client, messages, tools, purpose)

    messages = builder.initial_messages(inherited)
    planning = getattr(executor, "planning", None)
    builder.tasks = getattr(executor, "tasks", None)  # 与规划动态前缀一起计量，压缩后重新读取宿主状态。
    if planning is not None:
        # 使用原系统消息追加受控协议，保持历史重建约定的消息偏移不变。
        builder.bind_planning(planning, messages[0]["content"])
        messages[0] = {**messages[0], "content": messages[0]["content"] + planning.instructions()}
    # 先为必需规则、历史、任务状态和新增工具声明留出空间，再按完整条目减少记忆。
    # 不放宽整体上限；即使没有正文仍超限，也由既有检查记录明确失败。
    while builder.memory.selected and not builder.build(messages, executor.definitions, 1).check.allowed:
        removed = builder.memory.selected.pop()
        builder.memory.skipped.append({"id": removed["id"], "title": removed["title"], "reason": "整体输入剩余预算不足，可搜索回读"})
        messages = builder.initial_messages(inherited)
        if planning is not None:
            builder.bind_planning(planning, messages[0]["content"])
            messages[0] = {**messages[0], "content": messages[0]["content"] + planning.instructions()}
    context.emit("memory_prepared", {"memory": builder.memory.snapshot()})
    # 保存初始消息和工具声明，后续事件只追加增量，避免每轮重复记录全部历史。
    context.emit("run_started", {"messages": messages, "tools": executor.definitions, "conversation_start": len(messages) - len(inherited) - 1})
    if context.manifest.human_decision is not None and context.manifest.human_decision.action in {"reject", "cancel"}:
        # 拒绝仍固定完整会话历史，但不调用模型、工具或验收，后续重新规划不会丢失原目标。
        if context.manifest.human_decision.action == "cancel":
            context.result.final_answer = "已取消本次等待，不会自动恢复执行。"
            raise AgentStopped("cancelled", "interaction_cancelled")
        context.result.final_answer = "已拒绝这一版计划，项目执行保持停止。"
        raise AgentStopped("completed", "plan_rejected")
    if skill_error is not None:
        # 仍保存本轮要求与可信历史，便于用户清空失效选择后在同一会话继续。
        raise AgentStopped("failed", skill_error.code)
    if parent is not None and not memory_history_changed:
        compressor.inherit(parent, messages, executor.definitions)
    while context.result.model_turns < config.limits.max_model_turns:
        # 显式让出执行，避免全为立即返回的本地操作时一直不响应取消。
        await asyncio.sleep(0)
        if planning is not None and planning.state.phase == "planning":
            phase = "planning"  # 修复过程中重新规划仍以宿主只读阶段为准。
        else:
            phase = "verification" if context.result.repair_attempts else "execution"
        executor.memory_phase = phase  # 变更前核对沿用实际修复阶段，不依赖会被工具事件覆盖的 context.phase。
        builder.memory.select(phase)
        # 动作触发的新增条目同样受整体预算限制，不能靠动态输入突破原限额。
        while builder.memory.selected and not builder.build(messages, executor.definitions, context.result.model_turns + 1).check.allowed:
            removed = builder.memory.selected.pop()
            builder.memory.skipped.append({"id": removed["id"], "title": removed["title"], "reason": "整体输入剩余预算不足，可搜索回读"})
        # 原始列表永不覆盖；压缩器返回的正是本次计量和实际发送的投影。
        prepared = await compressor.prepare(messages, executor.definitions, request_model)
        context.emit("context_checked", prepared.check.model_dump(mode="json"))
        if not prepared.check.allowed:
            reason = "max_model_input_bytes" if prepared.check.input_bytes > prepared.check.max_input_bytes else "max_model_input_tokens"
            raise AgentStopped("limit_exceeded", reason)
        if context.result.model_turns >= config.limits.max_model_turns:
            # 摘要可能刚用完最后一次额度，不能为不会发送的执行请求记录“实际采用”。
            raise AgentStopped("limit_exceeded", "max_model_turns")
        builder.memory.record_use(context.result.model_turns + 1, prepared.messages)
        response = await request_model(prepared.messages, prepared.tools, "execution")
        builder.observe_usage(prepared.check.input_bytes, response.usage)
        context.emit("assistant_message", {"model_turn": context.result.model_turns, "message": response.message})
        # 完整保留 assistant 消息及其调用 ID，下一轮才能正确对应每个 tool 结果。
        messages.append(response.message)
        if not response.tool_calls:
            context.result.final_answer = response.message["content"]
            feedback = await on_completion() if on_completion is not None else None
            if feedback is None:
                return
            messages.append({"role": "user", "content": feedback})
            continue

        batch = response.tool_calls
        if len(batch) > config.limits.max_tool_calls - context.result.tool_calls:
            # 整批拒绝，避免只执行一半后把残缺配对历史再发给供应商。
            for call in batch:
                _record_tool(context, call, _skipped("skipped_limit"), messages)
            raise AgentStopped("limit_exceeded", "max_tool_calls")

        # index 和 started 用来区分中断时正在执行的工具与尚未启动的同批工具。
        index = 0
        started = False
        try:
            for index, call in enumerate(batch):
                started = False
                await asyncio.sleep(0)
                # 被接纳处理的未知工具和非法参数也占额度，避免错误调用无限循环。
                context.result.tool_calls += 1
                context.emit("tool_started", {
                    "call_id": call.call_id, "name": call.name, "arguments_json": call.arguments_json,
                })
                started = True
                result = await executor.execute(call.name, call.arguments_json)
                _record_tool(context, call, result, messages)
                started = False
                if (result.get("error") or {}).get("code") == "memory_context_updated":
                    # 新约定出现后，整批尚未执行的调用都交回模型重新决定，避免沿用旧参数。
                    for remaining in batch[index + 1:]:
                        pending = _skipped("memory_context_updated")
                        pending["error"]["message"] = "尚未执行：项目约定已更新，请阅读后重新决定本批工具参数。"
                        _record_tool(context, remaining, pending, messages)
                    break
                if planning is not None:
                    context.result.planning = planning.state.model_copy(deep=True)
                    if planning.state.pending is not None:
                        for remaining in batch[index + 1:]:
                            _record_tool(context, remaining, _skipped("skipped_user_input"), messages)
                        pending = planning.state.pending
                        context.result.final_answer = pending.question if pending.kind == "question" else "已提交计划，等待你审阅并批准。"
                        raise AgentStopped("waiting_input", "user_question" if pending.kind == "question" else "plan_approval")
                if context.result.cleanup_status in {"unknown", "incomplete"}:
                    for remaining in batch[index + 1:]:
                        _record_tool(context, remaining, _skipped("skipped_cleanup"), messages)
                    raise AgentStopped("failed", "process_cleanup_failed")
        except asyncio.CancelledError:
            timeout = asyncio.get_running_loop().time() >= context.deadline
            for remaining_index, call in enumerate(batch[index:], start=index):
                actual = getattr(executor, "cancelled_result", None)
                if remaining_index == index and started and actual is not None:
                    # 命令取消已经完成清理，保留真实输出与清理结论，不能合成为未执行。
                    _record_tool(context, call, actual, messages)
                    continue
                code = "tool_cancelled" if remaining_index == index and started else "skipped_cancelled"
                if timeout:
                    code = "tool_timed_out" if remaining_index == index and started else "skipped_timeout"
                _record_tool(context, call, _skipped(code), messages)
            raise
        except AgentStopped:
            raise
        except StorageError:
            raise
        except Exception:
            # 内部执行故障也配齐本批结果，再终止调度；错误正文不进入日志。
            for remaining_index, call in enumerate(batch[index:], start=index):
                code = "tool_internal_error" if remaining_index == index and started else "skipped_error"
                _record_tool(context, call, _skipped(code), messages)
            raise AgentStopped("failed", "tool_internal_error") from None
    raise AgentStopped("limit_exceeded", "max_model_turns")
