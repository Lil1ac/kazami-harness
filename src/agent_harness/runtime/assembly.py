"""声明父子角色共用的生命周期阶段、依赖和必需组件。"""

from agent_harness.runtime import background, completion, delegation_steps, input_steps, interaction, resume, team_steps, tool_steps
from agent_harness.runtime.pipeline import ROLES, Component, Pipeline, Role, Stage

ROOT = frozenset({"root"})  # 根运行有独立项目验收。
CHILDREN = ROLES - ROOT  # 子运行使用角色交接协议。
DEVELOP = frozenset({"develop"})  # 仅开发子段会在父请求时安全暂停。


def component(name, stage, run, *, roles=ROLES, after=(), reads=(), writes=()) -> Component:
    """由稳定 name、stage 和异步 run 创建组件；角色、依赖与事实列表默认按阶段约束。"""
    return Component(name, stage, run, roles=roles, after=tuple(after), reads=frozenset(reads), writes=frozenset(writes))


BOUNDARY = component("completion.boundary", Stage.BEFORE_FINISH, interaction.completion_boundary, writes=("runtime",))
ROOT_CHECK = component("completion.verification", Stage.VERIFY, completion.verify_delivery, roles=ROOT, after=(BOUNDARY.name,),
                       reads=("candidate",), writes=("runtime", "workspace", "verification"))
CHILD_CHECK = component("completion.child_report", Stage.VERIFY, delegation_steps.verify_child_delivery, roles=CHILDREN,
                        after=(BOUNDARY.name,), reads=("candidate",), writes=("messages", "runtime"))
RESULT_GUARD = component("tool.safety", Stage.AFTER_TOOL, tool_steps.guard_tool_result,
                         reads=("receipt",), writes=("runtime",))
MEMORY = component("input.memory", Stage.INPUT, input_steps.select_memory, reads=("messages", "turn"), writes=("memory",))
COMPRESS = component("input.compress", Stage.INPUT, input_steps.compress_input, after=(MEMORY.name, "team.input"),
                     reads=("memory", "handoff"), writes=("input", "workspace"))
HANDOFF = component("input.handoff", Stage.INPUT, input_steps.handoff_input, after=(COMPRESS.name,),
                     reads=("input", "handoff"), writes=("input",))
RESUME = component("permission.resume", Stage.START, resume.resume_action, after=("hooks.start",),
                   writes=("messages", "runtime", "workspace", "candidate"))

# 验收、安全门禁、审批接续和有序输入投影属于宿主契约。
REQUIRED = (BOUNDARY, ROOT_CHECK, CHILD_CHECK, RESULT_GUARD, MEMORY, COMPRESS, HANDOFF, RESUME)

# 同阶段按声明位置执行；after 和读写契约保证移动、删减时能在启动前发现错误。
COMPONENTS = (
    component("team.restore", Stage.START, team_steps.restore_team_input, writes=("messages",)),
    component("plan.decision", Stage.START, resume.apply_human_decision, after=("team.restore",), writes=("runtime",)),
    component("history.restore", Stage.START, resume.restore_projection, after=("plan.decision",), reads=("messages",)),
    component("hooks.start", Stage.START, background.run_start_hook, after=("history.restore",), writes=("messages", "workspace")),
    RESUME,
    component("delegation.resume", Stage.START, delegation_steps.resume_children, roles=ROOT, after=(RESUME.name,), writes=("messages", "workspace")),
    component("interaction.receive", Stage.INPUT, interaction.receive_input, writes=("messages", "runtime")),
    component("command.notifications", Stage.INPUT, background.command_input, after=("interaction.receive",), writes=("messages",)),
    component("hooks.input", Stage.INPUT, background.hook_input, after=("command.notifications",), writes=("messages", "workspace")),
    component("team.guard", Stage.INPUT, team_steps.guard_team, roles=ROOT, after=("hooks.input",), writes=("runtime",)),
    component("delegation.pause_input", Stage.INPUT, delegation_steps.guard_child_pause, roles=DEVELOP, after=("hooks.input",), writes=("runtime",)),
    component("delegation.handoff", Stage.INPUT, delegation_steps.collect_for_handoff, after=("hooks.input",), writes=("messages", "runtime", "handoff")),
    MEMORY,
    component("team.input", Stage.INPUT, team_steps.team_input, after=(MEMORY.name,), writes=("messages", "runtime")),
    COMPRESS,
    HANDOFF,
    component("interaction.acknowledge", Stage.RESPONSE, interaction.acknowledge_input, writes=("runtime",)),
    component("team.acknowledge", Stage.RESPONSE, team_steps.acknowledge_team, after=("interaction.acknowledge",), writes=("runtime",)),
    component("delegation.check", Stage.RESPONSE, delegation_steps.check_child_errors, roles=ROOT, after=("team.acknowledge",), writes=("runtime",)),
    component("interaction.guard_tool", Stage.BEFORE_TOOL, interaction.guard_tool_input, writes=("runtime",)),
    component("delegation.pause_tool", Stage.BEFORE_TOOL, delegation_steps.guard_child_pause, roles=DEVELOP, after=("interaction.guard_tool",), writes=("runtime",)),
    component("delegation.accept", Stage.BEFORE_TOOL, delegation_steps.note_delegation, roles=ROOT, after=("interaction.guard_tool",), reads=("tool",), writes=("runtime",)),
    RESULT_GUARD,
    component("delegation.review", Stage.AFTER_TOOL, delegation_steps.review_delegation, roles=ROOT, after=(RESULT_GUARD.name,), reads=("tool", "receipt"), writes=("runtime",)),
    component("memory.review", Stage.AFTER_TOOL, tool_steps.review_memory_change, after=(RESULT_GUARD.name,), reads=("receipt",)),
    component("plan.capture", Stage.AFTER_TOOL, tool_steps.capture_planning, after=("memory.review",), writes=("runtime",)),
    component("interaction.settled_batch", Stage.BATCH_END, interaction.settled_batch, after=("interaction.guard_tool",), writes=("runtime",)),
    component("interaction.candidate", Stage.CANDIDATE, interaction.candidate_input, writes=("messages", "runtime")),
    component("command.candidate", Stage.CANDIDATE, background.settle_candidate_commands, after=("interaction.candidate",), writes=("messages",)),
    component("hooks.candidate", Stage.CANDIDATE, background.candidate_hooks, after=("command.candidate",), writes=("messages", "workspace")),
    component("delegation.candidate", Stage.CANDIDATE, delegation_steps.collect_candidate_reports, roles=ROOT, after=("hooks.candidate",), writes=("messages",)),
    component("team.candidate", Stage.CANDIDATE, team_steps.candidate_team, roles=ROOT, after=("delegation.candidate",), reads=("candidate",), writes=("runtime",)),
    BOUNDARY,
    component("hooks.stop", Stage.BEFORE_FINISH, background.stop_hook, after=(BOUNDARY.name,), reads=("candidate",), writes=("messages", "runtime", "workspace")),
    ROOT_CHECK,
    CHILD_CHECK,
    component("completion.repair", Stage.AFTER_FINISH, completion.repair_delivery, roles=ROOT, after=(ROOT_CHECK.name,),
              reads=("verification",), writes=("messages", "runtime")),
    component("interaction.final_check", Stage.AFTER_FINISH, interaction.invalidate_candidate, writes=("runtime",)),
)


def assemble(role: Role) -> Pipeline:
    """校验所有适用角色的注册，再返回当前 role 的执行表；配置错误在副作用前暴露。"""
    pipelines = {item: Pipeline(COMPONENTS, REQUIRED, item) for item in ROLES}
    return pipelines[role]
