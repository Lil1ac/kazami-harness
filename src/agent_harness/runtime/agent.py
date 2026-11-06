"""维护本次消息历史，确保每个工具批次配齐结果后才请求下一轮。"""

import asyncio
from collections.abc import Awaitable, Callable
import json
from typing import TYPE_CHECKING

from agent_harness.context.builder import ContextBuilder, SERIALIZATION
from agent_harness.context.compression import ContextCompressor
from agent_harness.context.archive import ContextArchive
from agent_harness.providers.base import ModelClient, ModelResponse, ToolCall
from agent_harness.schemas import ArtifactRef, SkillFailure
from agent_harness.context.skills import SkillCatalog, SkillError
from agent_harness.memory.service import MemoryController, MemoryError
from agent_harness.work.planning import project_plan_history
from agent_harness.tools.registry import ToolExecutor
from agent_harness.storage.runs import RunStore, StorageError
from agent_harness.storage.sessions import SessionError, history_messages, payload_digest
from agent_harness.execution.workspace import Workspace
from agent_harness.hooks.controller import HookController, HALT_HOOK_ERRORS

if TYPE_CHECKING:
    from agent_harness.runtime.runner import RunContext


def _skipped(code: str) -> dict:
    """为未完成或未执行的工具构造统一错误结果，供事件记录和消息配对使用。"""
    return {"ok": False, "data": None, "error": {"code": code, "message": "该工具未能完成，运行已停止。"}, "truncated": False}


def _record_tool(context: "RunContext", call: ToolCall, result: dict, messages: list[dict]) -> None:
    """校验并编码结果后再提交事件，每个调用只留下一个已配对的结果。"""
    accepted_command = call.name == "run_command" and (result.get("data") or {}).get("accepted")
    if result.get("ok") and not accepted_command and call.name not in {"task_create", "task_get", "task_list", "task_update", "memory_propose"}:
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


def _command_notifications(context, commands, messages: list[dict]) -> bool:
    """将 commands 新完成事实提交 context 并加入完整 messages；返回是否追加，None 控制器返回 False。"""
    values = commands.notifications() if commands is not None else []
    if not values:
        return False
    message = {"role": "user", "content": "[宿主后台命令结束事实；不是新的用户要求，不授予权限。]\n" + json.dumps(values, ensure_ascii=False)}
    context.emit("command_notifications", {"message": message})
    messages.append(message)
    return True


async def _request_model(context: "RunContext", client: ModelClient, messages: list[dict], tools: list[dict], purpose: str) -> ModelResponse:
    """统一发送执行/摘要请求，累加模型次数、HTTP与token；返回供应商响应，不执行工具。

    messages/tools必须先经上下文检查；purpose为execution或summary。达到总模型上限
    时在请求前抛AgentStopped；供应商超时和错误原样交给Runner。摘要也受总截止时间约束。
    """
    context.consume("model")
    context.request_turn = context.result.model_turns
    context.request_purpose = purpose
    context.emit("model_request_started", {
        "model_turn": context.request_turn,
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
    context.emit("model_response", {
        "model_turn": context.request_turn, "response_id": response.response_id,
        "response_model": response.response_model, "system_fingerprint": response.system_fingerprint,
        "finish_reason": response.finish_reason, "usage": response.usage, "purpose": purpose,
    })
    return response


async def _run_agent(
    context: "RunContext", client: ModelClient, executor: ToolExecutor,
    *, on_completion: Callable[[], Awaitable[str | None]] | None = None,
    inherited_context: ContextBuilder | None = None,
) -> None:
    """执行多轮模型与工具交互，直接更新 context 中的计数和最终回答。

    client 负责模型通信，executor 负责工具执行；本函数维护消息历史和调用配对。
    同一批工具按响应顺序串行处理；委派等交接点跳过剩余调用并配齐回执，再请求模型重新决定。
    首次冻结项目约定和记忆修订，每轮按实际动作选择记忆并检查压缩投影；摘要同样占模型轮次。
    达到额度或项目约定读取错误时抛出 AgentStopped，取消和存储故障交由 Runner 处理。
    on_completion在无工具回答后执行宿主检查；None返回表示结束，字符串表示已落盘的
    公开测试反馈，追加后沿用同一消息、压缩器、工具执行器和预算继续，绝不重新初始化运行。
    inherited_context默认None；子执行继承固定约定、显式选择的技能及记忆修订，不继承消息历史。
    """
    from agent_harness.runtime.runner import AgentStopped
    from agent_harness.work.tasks import TaskError

    config = context.manifest.config
    skills = []  # 每轮开始只读取一次；后续模型调用和历史页都使用固定快照。
    skill_error = None  # 加载失败同样落盘受控事实，模型和工具均不执行。
    try:
        skills = ([item for item in inherited_context.skills if item.name in config.skills] if inherited_context is not None
                  else SkillCatalog(config.skill_roots).load(config.skills))
    except SkillError as error:
        skill_error = SkillFailure(name=error.name, code=error.code, message=error.message)
    builder = ContextBuilder(
        Workspace(context.manifest.task.workspace, context.runs_root),
        context.manifest.task, config, skills=skills,
        instruction=inherited_context.instruction if inherited_context is not None else None,
    )
    try:
        builder.memory = (inherited_context.memory.fork(context.manifest, context.emit) if inherited_context is not None
                          else MemoryController(context.runs_root, context.manifest, context.emit))
        executor.memory = builder.memory
    except MemoryError as error:
        context.emit("memory_failed", {"code": error.code, "message": error.message})
        raise AgentStopped("failed", error.code) from None
    if isinstance(executor, ToolExecutor) and context.manifest.delegation is None and executor.readonly is None and {"read_file", "search_files"}.intersection(config.tools):
        from agent_harness.subagents.service import SubagentController
        executor.subagents = SubagentController(context, client, executor)
        context.subagent_controller = executor.subagents
        from agent_harness.teams.controller import TeamController
        executor.team = TeamController(context, executor.subagents)
        executor.subagents.team = executor.team
        executor.team.reconcile()
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
    delegation = context.manifest.delegation
    if delegation is not None and delegation.previous_run_id is not None:
        from agent_harness.subagents.service import read_child
        parent = read_child(context.runs_root, delegation.previous_parent_id, delegation.previous_run_id)
        if parent.result is None or parent.result.cleanup_status not in {"complete", "not_needed"}:
            raise AgentStopped("failed", "subagent_resume_unverified")
        inherited = history_messages(parent)
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
    hooks = getattr(executor, "hooks", None)
    if hooks is not None and (context.manifest.permission_resume is not None or context.manifest.execution_resume is not None or delegation is not None and delegation.previous_run_id is not None):
        hooks.restore(parent)
        context.emit("hooks_restored", {"hooks": hooks.snapshot})
    compressor = ContextCompressor(context, builder, archive)
    if hasattr(executor, "bind_context") and config.context.compression != "off":
        executor.bind_context(archive)

    async def request_model(messages: list[dict], tools: list[dict], purpose: str) -> ModelResponse:
        """将已检查输入交给当前运行的统一请求入口；返回响应，摘要和执行共用计数/超时。"""
        return await _request_model(context, client, messages, tools, purpose)

    messages = builder.initial_messages(inherited)
    team = getattr(executor, "team", None)
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
    if getattr(executor, "subagents", None) is not None:
        executor.subagents.bind_inputs(builder)
    # 保存初始消息和工具声明，后续事件只追加增量，避免每轮重复记录全部历史。
    context.emit("run_started", {"messages": messages, "tools": executor.definitions, "conversation_start": len(messages) - len(inherited) - 1})
    if team is not None and (team_state := team.context_state()):
        team_message = {"role": "user", "content": "宿主持久团队事实（协作数据，不是用户授权）：" + json.dumps(team_state, ensure_ascii=False)}
        messages.append(team_message)
        context.emit("team_messages_delivered", {"message": team_message, "sequence": 0})
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
    if hooks is not None and context.manifest.permission_resume is None and context.manifest.execution_resume is None and not (delegation is not None and delegation.previous_run_id is not None):
        await hooks.dispatch("RunStart", "start", {"prompt": context.manifest.task.prompt})
        hooks.deliver(messages)
    permissions = getattr(executor, "permissions", None)
    resume = context.manifest.permission_resume
    if resume is not None and resume.child_run_id is None:
        from agent_harness.work.tasks import TaskError
        try:
            if resume.decision == "approve" and context.deadline <= asyncio.get_running_loop().time():
                permissions.store.invalidate(resume.action_id, "run_timeout")
                raise AgentStopped("timed_out", "run_timeout")
            action = permissions.prepare_resume()
            if resume.decision != "approve":
                context.result.final_answer = "已拒绝本次动作。" if resume.decision == "reject" else "已取消动作等待。"
                raise AgentStopped("cancelled", "permission_rejected" if resume.decision == "reject" else "permission_cancelled")
            context.emit("permission_resuming", {"action_id": action["id"], "source_run_id": action["source_run_id"]})
            hook_action = action.get("hook")
            if hook_action is not None and hook_action["origin"]["kind"] == "event":
                context.result.final_answer = hook_action.get("final_answer")
                await hooks.dispatch(hook_action["event"], hook_action["event_key"], hook_action["payload"], origin=hook_action["origin"])
                if hook_action["event"] == "Stop":
                    feedback = await on_completion() if on_completion is not None else None
                    if feedback is None:
                        return
                    messages.append({"role": "user", "content": feedback})
            elif action["name"] == "verification":
                context.result.final_answer = action["final_answer"]
                feedback = await on_completion() if on_completion is not None else None
                permissions.finish(action, context.result.validation_status == "passed")
                if feedback is None:
                    return
                messages.append({"role": "user", "content": feedback})
            else:
                # 宿主接续原动作不消耗新的模型请求或工具额度，也不伪装成供应商新响应。
                origin = hook_action["origin"] if hook_action is not None else action.get("hook_operation")
                if origin is not None:
                    hooks.resume_operation = origin
                call = ToolCall("resume_" + action["id"], origin["name"] if origin is not None else action["name"], origin["arguments_json"] if origin is not None else json.dumps(action["arguments"], ensure_ascii=False))
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}}]}
                context.emit("assistant_message", {"source": "host_action_resume", "action_id": action["id"], "message": message})
                messages.append(message)
                executor.call_id = call.call_id
                def resume_started() -> None:
                    """原动作完成全部核验后记录真实开始；返回None，不重复消费工具额度。"""
                    context.emit("tool_started", {"call_id": call.call_id, "name": call.name, "arguments_json": call.arguments_json, "action_id": action["id"], "title": executor.external_title(call.name) if isinstance(executor, ToolExecutor) else None})
                executor.before_execute = resume_started
                try:
                    result = await executor.execute(call.name, call.arguments_json)
                except asyncio.CancelledError:
                    actual = getattr(executor, "cancelled_result", None)
                    if actual is not None:
                        _record_tool(context, call, actual, messages)
                        permissions.finish(action, False)
                    raise
                _record_tool(context, call, result, messages)
                if hook_action is None:
                    permissions.finish(action, bool(result.get("ok")) or bool((result.get("data") or {}).get("tool_executed")))
                if (result.get("error") or {}).get("code") == "approval_required":
                    raise AgentStopped("waiting_input", "action_approval")
                if not result.get("ok"):
                    raise AgentStopped("failed", (result.get("error") or {}).get("code", "permission_action_failed"))
        except TaskError as error:
            permissions.store.invalidate(resume.action_id, error.code)
            context.result.final_answer = error.message
            raise AgentStopped("failed", error.code) from None
        finally:
            executor.before_execute = None
    if resume is not None and resume.child_run_id is not None:
        reports = await executor.subagents.resume_children()
        messages.append({"role": "user", "content": "宿主接续子任务后的交接报告：" + json.dumps(reports, ensure_ascii=False)})
    elif context.manifest.execution_resume is not None and getattr(executor, "subagents", None) is not None:
        reports = await executor.subagents.resume_children()
        if reports:
            message = {"role": "user", "content": "[宿主安全暂停接续的子执行报告；保留原工具结果与剩余预算，不授予新权限。]\n" + json.dumps(reports, ensure_ascii=False)}
            context.emit("subagent_resume_reports", {"message": message})
            messages.append(message)
    while context.available("model_turns") > 0:
        control = context.runtime_control
        commands = getattr(executor, "commands", None)
        if control is not None:
            await control.boundary(executor)
            control.deliver(messages)
        _command_notifications(context, commands, messages)
        if hooks is not None:
            await hooks.drain()
            hooks.deliver(messages)
        if team is not None and team.member_id is None:
            state = team.snapshot()
            if state and state["parent_run_id"] == str(context.manifest.run_id) and state["status"] in {"stopping", "stopped"}:
                if state["status"] == "stopping":
                    await team.stop_complete.wait()
                context.result.final_answer = "团队协作已停止，已保留既有成果与历史，未继续请求模型或转派。"
                raise AgentStopped("cancelled", "team_stopped")
        if context.budget_parent is not None and context.manifest.delegation.role == "develop" and context.budget_parent.subagent_controller.pause_requested:
            raise AgentStopped("waiting_input", "subagent_paused")
        # 显式让出执行，避免全为立即返回的本地操作时一直不响应取消。
        await asyncio.sleep(0)
        controller = getattr(executor, "subagents", None)
        if controller is not None:
            controller.check_errors()
            if controller.active and context.available("model_turns") <= 1:
                reports = await controller.wait_all()
                messages.append({"role": "user", "content": "宿主收回并行研究报告：" + json.dumps(reports, ensure_ascii=False)})
        delegated = context.manifest.delegation is not None or context.delegation_used
        handoff = delegated and (getattr(executor, "report_only", False) or context.available("model_turns") == 1
                                 or context.available("tool_calls") <= 0)
        if isinstance(executor, ToolExecutor):
            executor.handoff = handoff
        if context.manifest.delegation is not None:
            phase = builder.memory.phase  # 子研究无法切换阶段，固定继承父委派时的实际阶段。
        elif planning is not None and planning.state.phase == "planning":
            phase = "planning"  # 修复过程中重新规划仍以宿主只读阶段为准。
        else:
            phase = "verification" if context.result.repair_attempts else "execution"
        executor.memory_phase = phase  # 变更前核对沿用实际修复阶段，不依赖会被工具事件覆盖的 context.phase。
        builder.memory.select(phase)
        # 动作触发的新增条目同样受整体预算限制，不能靠动态输入突破原限额。
        while builder.memory.selected and not builder.build(messages, executor.definitions, context.result.model_turns + 1).check.allowed:
            removed = builder.memory.selected.pop()
            builder.memory.skipped.append({"id": removed["id"], "title": removed["title"], "reason": "整体输入剩余预算不足，可搜索回读"})
        team_sequence = 0
        if team is not None:
            try:
                team.check_member()
            except TaskError as error:
                context.result.final_answer = error.message
                raise AgentStopped("cancelled", error.code) from None
            incoming, team_sequence = team.message_input()
            if incoming:
                message = {"role": "user", "content": incoming}
                messages.append(message)
                context.emit("team_messages_delivered", {"message": message, "sequence": team_sequence})
        # 原始列表永不覆盖；压缩器返回的正是本次计量和实际发送的投影。
        prepared = await compressor.prepare(messages, executor.definitions, request_model, reserve_model_turns=1 if delegated else 0)
        if handoff:
            notice = {"role": "system", "content": (
                "宿主收尾交接：本次只总结已有信息，禁止新工具调用；不得为写出结论而编造完成或检查通过。"
                + ("请按角色JSON格式交接；研究未完成时complete=false，列明限制与未完成事项。" if context.manifest.delegation is not None
                   else "请明确已交付内容、失败及未完成事项，等待宿主按真实代码独立检查。"))}
            # 交接通知仅进入此次输入投影，不改变压缩原消息索引或跨轮历史。
            prepared = builder.build([*prepared.messages, notice], [], context.result.model_turns + 1,
                                     raw_input_bytes=prepared.check.raw_input_bytes)
            context.emit("budget_handoff_started", {"message": notice, "remaining_model_turns": config.limits.max_model_turns - context.result.model_turns,
                                                    "remaining_tool_calls": config.limits.max_tool_calls - context.result.tool_calls})
        context.emit("context_checked", prepared.check.model_dump(mode="json"))
        if not prepared.check.allowed:
            reason = "max_model_input_bytes" if prepared.check.input_bytes > prepared.check.max_input_bytes else "max_model_input_tokens"
            raise AgentStopped("limit_exceeded", reason)
        if context.available("model_turns") <= 0:
            # 摘要可能刚用完最后一次额度，不能为不会发送的执行请求记录“实际采用”。
            raise AgentStopped("limit_exceeded", "max_model_turns")
        builder.memory.record_use(context.result.model_turns + 1, prepared.messages)
        response = await request_model(prepared.messages, prepared.tools, "execution")
        if control is not None:
            control.acknowledge()
        if team is not None:
            team.acknowledge(team_sequence)
        if controller is not None:
            controller.check_errors()
        builder.observe_usage(prepared.check.input_bytes, response.usage)
        context.emit("assistant_message", {"model_turn": context.request_turn, "message": response.message})
        # 完整保留 assistant 消息及其调用 ID，下一轮才能正确对应每个 tool 结果。
        messages.append(response.message)
        if not response.tool_calls:
            if control is not None:
                await control.boundary(executor)
                if control.deliver(messages):
                    continue
            if commands is not None and commands.active:
                await commands.settle()
                # 命令结束后重新决策，再进行宿主独立验收；不得让活动进程越过成果快照。
                continue
            if _command_notifications(context, commands, messages):
                continue  # 模型请求期间完成的后台命令也必须交回模型核对，不能直接交付旧答复。
            if hooks is not None:
                await hooks.drain()
                if hooks.deliver(messages):
                    continue
            if controller is not None and controller.pending_reports:
                reports = await controller.wait_all()
                messages.append({"role": "user", "content": "子研究已结束，请核对报告后交付：" + json.dumps(reports, ensure_ascii=False)})
                continue
            context.result.final_answer = response.message["content"]
            if team is not None and team.member_id is None:
                state = team.snapshot()
                if state and state["status"] == "active" and any(t["state"] != "ready" for t in state["tasks"]):
                    raise AgentStopped("waiting_input", "team_attention_required")
            feedback = await on_completion() if on_completion is not None else None
            if feedback is None:
                if control is not None and control.available_inputs():
                    context.result.execution_status = "running"
                    context.result.validation_status = "unverified"
                    context.result.verification = None
                    context.result.final_answer = None
                    continue  # 检查期间收到的新约束仍需处理，旧检查只留历史。
                return
            messages.append({"role": "user", "content": feedback})
            continue

        batch = response.tool_calls
        if handoff:
            for call in batch:
                _record_tool(context, call, _skipped("handoff_tools_disabled"), messages)
            raise AgentStopped("limit_exceeded", "handoff_tools_disabled")
        if len(batch) > context.available("tool_calls"):
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
                if control is not None and (control.pausing() or control.available_inputs()):
                    for remaining in batch[index:]:
                        _record_tool(context, remaining, _skipped("runtime_input_requires_review"), messages)
                    await control.boundary(executor)
                    break  # 未执行参数已经过时，先配齐整批，再在下一轮交付新要求。
                if context.budget_parent is not None and context.manifest.delegation.role == "develop" and context.budget_parent.subagent_controller.pause_requested:
                    for remaining in batch[index:]:
                        _record_tool(context, remaining, _skipped("skipped_child_pause"), messages)
                    raise AgentStopped("waiting_input", "subagent_paused")
                await asyncio.sleep(0)
                # 被接纳处理的未知工具和非法参数也占额度，避免错误调用无限循环。
                # 委派可能消耗了同批工具剩余额度，每个实际调用前再次核对父子总数。
                if context.available("tool_calls") <= 0:
                    for remaining in batch[index:]:
                        _record_tool(context, remaining, _skipped("skipped_limit"), messages)
                    raise AgentStopped("limit_exceeded", "max_tool_calls")
                context.consume("tool")
                context.emit("tool_requested", {"call_id": call.call_id, "name": call.name})
                def tool_started() -> None:
                    """当前call在参数和权限检查通过后记录实际开始；更新取消时的执行状态。"""
                    nonlocal started
                    context.emit("tool_started", {"call_id": call.call_id, "name": call.name, "arguments_json": call.arguments_json, "title": executor.external_title(call.name) if isinstance(executor, ToolExecutor) else None})
                    started = True
                if isinstance(executor, ToolExecutor):
                    executor.call_id = call.call_id
                    executor.before_execute = tool_started
                else:
                    tool_started()  # 注入的测试执行器仍遵循开始后执行的事件契约。
                if call.name in {"delegate_readonly", "delegate_develop", "team_dispatch"} and context.manifest.delegation is None:
                    context.delegation_used = True
                result = await executor.execute(call.name, call.arguments_json)
                _record_tool(context, call, result, messages)
                started = False
                if (result.get("error") or {}).get("code") == "approval_required":
                    for remaining in batch[index + 1:]:
                        _record_tool(context, remaining, _skipped("skipped_action_approval"), messages)
                    context.result.final_answer = "原工具已执行，Hook 后处理等待批准；后续操作已停止。" if (result.get("data") or {}).get("tool_executed") else "有待执行动作需要你批准，当前动作及同批后续操作尚未执行。"
                    raise AgentStopped("waiting_input", "action_approval")
                hook_error = (result.get("error") or {}).get("code")
                if hook_error in HALT_HOOK_ERRORS or hook_error in {"mcp_result_unknown", "mcp_definition_changed", "mcp_selection_stale"}:
                    for remaining in batch[index + 1:]:
                        _record_tool(context, remaining, _skipped("skipped_hook_failure"), messages)
                    context.result.final_answer = result["error"]["message"]
                    raise AgentStopped("failed", hook_error)
                if call.name in {"delegate_readonly", "delegate_develop", "team_dispatch"} and context.manifest.delegation is None:
                    if (result.get("data") or {}).get("accepted") and index + 1 < len(batch) and batch[index + 1].name in {"delegate_readonly", "delegate_develop"}:
                        continue  # 同批独立研究可并发接纳；后续修改仍由交接屏障跳过。
                    # 后续参数在子报告产生前已经生成；成功、失败或拒绝接纳都先交回模型重新决定。
                    for remaining in batch[index + 1:]:
                        pending = _skipped("subagent_result_requires_review")
                        pending["error"]["message"] = "尚未执行：请先阅读委派结果，再重新决定后续工具及参数。"
                        _record_tool(context, remaining, pending, messages)
                    context.emit("subagent_handoff", {"call_id": call.call_id, "ok": bool(result.get("ok")), "skipped_calls": len(batch) - index - 1})
                    break
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


async def run_agent(context, client, executor, *, on_completion=None, inherited_context=None) -> None:
    """用context/client/executor执行单轮并收拢子任务；可选完成回调与继承输入同核心，返回None。

    正常或异常退出均取消并等待未结束子项，确保客户端关闭、独立检查和父终态前没有后台读者。
    """
    from agent_harness.permissions.policy import FILE_SCOPE, roots
    policy = context.manifest.config.permission_policy
    token = FILE_SCOPE.set((context.manifest.task.workspace.resolve(), roots(context.manifest.task.workspace.resolve(), policy.read_roots) if policy.read == "allow" else ()))
    if isinstance(executor, ToolExecutor):
        from agent_harness.execution.commands import CommandController
        from agent_harness.runtime.control import RuntimeControl
        if context.runtime_control is None:
            context.runtime_control = RuntimeControl(context)
        executor.commands = CommandController(context, executor)
        context.command_controller = executor.commands
        (context.budget_parent or context).command_controllers[str(context.manifest.run_id)] = executor.commands
    if getattr(executor, "permissions", None) is None and isinstance(executor, ToolExecutor):
        from agent_harness.permissions.controller import PermissionController
        spent = context.result.permissions
        executor.permissions = PermissionController(context, getattr(executor, "planning", None))
        if context.manifest.delegation is not None and context.manifest.permission_resume is None:
            for field in ("spent_model_turns", "spent_tool_calls", "spent_seconds", "spent_usage", "spent_usage_complete"):
                setattr(executor.permissions.state, field, getattr(spent, field))
        context.result.permissions = executor.permissions.state.model_copy(deep=True)
        context.emit("permissions_prepared", {"state": context.result.permissions.model_dump(mode="json")})
    try:
        if isinstance(executor, ToolExecutor):
            from agent_harness.mcp.controller import McpController
            executor.mcp = McpController(context)
            executor.permissions.external = executor.mcp
            if executor.mcp.entries:
                context.emit("mcp_prepared", {"tools": [{"name": name, "config": item["record"]["config"],
                    "fingerprint": item["record"]["fingerprint"], "definition": item["tool"],
                    "research": executor.mcp.research(name), "local_identity": item["local_identity"]}
                    for name, item in executor.mcp.entries.items()]})
            executor.hooks = HookController(context, executor)
            context.hooks = executor.hooks
            context.emit("hooks_prepared", {"hooks": executor.hooks.snapshot})
        await _run_agent(context, client, executor, on_completion=on_completion, inherited_context=inherited_context)
    finally:
        FILE_SCOPE.reset(token)
        try:
            controller = getattr(executor, "subagents", None)
            if controller is not None:
                await controller.close()
        finally:
            commands = getattr(executor, "commands", None)
            if commands is not None:
                await commands.settle(stop=True)
