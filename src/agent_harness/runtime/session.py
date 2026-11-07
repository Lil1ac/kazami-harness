"""冻结运行输入和可信历史，为阶段组件提供当前段状态。"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_harness.context.builder import ContextBuilder, ModelInput, SERIALIZATION
from agent_harness.context.compression import ContextCompressor
from agent_harness.context.archive import ContextArchive
from agent_harness.providers.base import ModelClient, ModelResponse
from agent_harness.schemas import SkillFailure, VerificationResult
from agent_harness.context.skills import SkillCatalog, SkillError
from agent_harness.memory.service import MemoryController, MemoryError
from agent_harness.work.planning import project_plan_history
from agent_harness.tools.registry import ToolExecutor
from agent_harness.storage.runs import RunStore, StorageError
from agent_harness.storage.sessions import SessionError, history_messages, payload_digest
from agent_harness.execution.workspace import Workspace

if TYPE_CHECKING:
    from agent_harness.runtime.runner import RunContext
    from agent_harness.runtime.tool_batch import ToolStep
    from agent_harness.runtime.verifier import Verifier
    from agent_harness.storage.runs import RunHistory


@dataclass
class Turn:
    """只保存本次请求的临时投影；进入下一轮时整体替换。"""

    delegated: bool = False  # 当前段需要为父子交接预留模型次数。
    handoff: bool = False  # 当前投影只允许交接总结，无工具。
    team_sequence: int = 0  # 当前输入的最新团队消息序号，收到答复后确认。
    input: ModelInput | None = None  # 经过记忆、压缩及交接修订的实际发送副本。
    response: ModelResponse | None = None  # 当前模型响应，未请求时为空。


@dataclass
class Session:
    """承载一次执行段及其历史；供应商输入始终从完整历史投影。"""

    context: "RunContext"  # 预算、截止时间、结果和事件归属。
    client: ModelClient  # 父子可共享的模型适配器。
    executor: ToolExecutor  # 本段的工具边界及宿主控制器。
    builder: ContextBuilder  # 绑定本段固定约定、记忆修订及规划投影。
    compressor: ContextCompressor  # 本段压缩器；辅助请求走统一网关。
    messages: list[dict]  # 供存储与工具配对使用的完整会话。
    turn: Turn = field(default_factory=Turn)  # 当前模型轮的临时状态。
    tool: "ToolStep | None" = None  # 只在当前批执行时有意义。
    completion_action: dict | None = None  # 已批准验收的接续动作，完成检查后提交结果。
    verifier: "Verifier | None" = None  # 根段独立验收；子段按交接协议核验。
    verification_detail: VerificationResult | None = None  # 当前候选答复对应的验收结论。
    skill_error: SkillFailure | None = None  # 初始化失败但仍需保留完整历史供接续。
    compression_parent: "RunHistory | None" = None  # 可复用摘要且未被记忆投影改变的已核验历史。
    report_retry: bool = False  # 本开发子段是否已用完唯一一次交接纠正机会。

    def append(self, message: dict, event: str | None = None, **facts) -> None:
        """追加 message；event 非空时先保存来源事实和消息，facts 是额外的事件字段。"""
        if event is not None:
            self.context.emit(event, {"message": message, **facts})
        self.messages.append(message)


def prepare_session(context: "RunContext", client: ModelClient, executor: ToolExecutor,
                    inherited_context: ContextBuilder | None, verifier: "Verifier | None") -> Session:
    """冻结 context 的约定、历史、技能和记忆，绑定 client/executor 后返回可调度会话。

    inherited_context 为 None 时读取根段输入；委派时仅继承固定输入，历史来源独立核验。
    verifier 为 None 表示无宿主验收或使用子交接校验。
    来源、项目约定或记忆无法核验时抛 AgentStopped，且不会请求模型。
    """
    from agent_harness.runtime.runner import AgentStopped

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
    if getattr(executor, "subagents", None) is not None:
        executor.subagents.bind_inputs(builder)
    # 保存初始消息和工具声明，后续事件只追加增量，避免每轮重复记录全部历史。
    context.emit("run_started", {"messages": messages, "tools": executor.definitions, "conversation_start": len(messages) - len(inherited) - 1})
    return Session(context, client, executor, builder, compressor, messages, verifier=verifier,
                   skill_error=skill_error, compression_parent=None if memory_history_changed else parent)
