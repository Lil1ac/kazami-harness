"""定义宿主生命周期组件的装配规则和阶段决定。"""

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Literal

from agent_harness.storage.runs import StorageError

if TYPE_CHECKING:
    from agent_harness.runtime.session import Session


class Stage(StrEnum):
    """定义内核触发组件的位置，值同时用于诊断事件。"""

    START = "start"  # 已固定会话和历史，尚未请求模型。
    INPUT = "input"  # 按顺序投递事实、选择记忆并投影模型输入。
    RESPONSE = "response"  # 模型已返回，确认实际收到的输入。
    BEFORE_TOOL = "before_tool"  # 尚未消耗工具额度或执行副作用。
    AFTER_TOOL = "after_tool"  # 工具结果已配对，可决定如何处理剩余调用。
    BATCH_END = "batch_end"  # 整批回执齐全，允许建立安全检查点。
    CANDIDATE = "candidate"  # 无工具答复，检查是否还有材料需要处理。
    BEFORE_FINISH = "before_finish"  # 候选交付已确定，可运行结束前策略。
    VERIFY = "verify"  # 必需的独立验收或子交接检查。
    AFTER_FINISH = "after_finish"  # 验收已结束，仅允许读取和更新运行状态。


Role = Literal["root", "explore", "review", "develop"]
ROLES: frozenset[Role] = frozenset({"root", "explore", "review", "develop"})


class Decision:
    """约束组件如何把控制权交还内核。"""

    kind: ClassVar[str]  # 诊断和前端共用的决定类型。


@dataclass(frozen=True)
class Proceed(Decision):
    """继续本阶段剩余组件，阶段耗尽后走下一步。"""

    kind = "proceed"


@dataclass(frozen=True)
class Replan(Decision):
    """停止本阶段，在配齐工具回执后交回模型重新决策。"""

    kind = "replan"
    feedback: str | None = None  # 已有宿主事实或待加入历史的提示。
    skip_code: str | None = None  # 剩余工具的未执行原因，非工具阶段可省略。
    skip_message: str | None = None  # None 使用内核统一未执行文案。


@dataclass(frozen=True)
class Wait(Decision):
    """结束本段并保留可接续的等待原因。"""

    kind = "wait"
    reason: str  # Runner 与前端共同使用的等待原因。
    skip_code: str | None = None  # 工具阶段可覆盖未执行原因。


@dataclass(frozen=True)
class Finish(Decision):
    """进入必需的完成流水线，例如直接接续已批准的验收。"""

    kind = "finish"


@dataclass(frozen=True)
class Abort(Decision):
    """立即终止调度并交给宿主清理、保存终态。"""

    kind = "abort"
    status: Literal["failed", "cancelled", "limit_exceeded", "timed_out", "completed"]  # Runner 保存的终态。
    reason: str  # 已定义的终态原因。
    skip_code: str | None = None  # 工具阶段可覆盖未执行原因。


@dataclass(frozen=True)
class Component:
    """声明宿主组件的身份、适用角色、输入输出及排序关系。"""

    name: str  # 诊断使用的稳定标识，同一流水线全局唯一。
    stage: Stage  # 组件可用的上下文由此阶段约束。
    run: Callable[["Session"], Awaitable[Decision]]  # 从本段状态读取事实并返回调度决定。
    roles: frozenset[Role] = ROLES  # 默认父子均执行，可限制到具体职责。
    after: tuple[str, ...] = ()  # 此角色中必须已装配且位置更早的组件。
    reads: frozenset[str] = frozenset()  # 此阶段要求已经建立的事实。
    writes: frozenset[str] = frozenset()  # 此阶段允许更新或提供的事实。


# 阶段入口自带的事实及允许的输出。工作区写入只允许在验收之前和独立验收中。
CONTRACTS = {
    Stage.START: ({"session", "messages"}, {"messages", "runtime", "workspace", "candidate"}),
    Stage.INPUT: ({"session", "messages", "turn"}, {"messages", "runtime", "handoff", "memory", "input", "workspace"}),
    Stage.RESPONSE: ({"session", "messages", "response", "input", "turn"}, {"runtime"}),
    Stage.BEFORE_TOOL: ({"session", "messages", "turn", "tool"}, {"runtime"}),
    Stage.AFTER_TOOL: ({"session", "messages", "turn", "tool", "receipt"}, {"runtime"}),
    Stage.BATCH_END: ({"session", "messages"}, {"runtime"}),
    Stage.CANDIDATE: ({"session", "messages", "response", "candidate"}, {"messages", "runtime", "workspace"}),
    Stage.BEFORE_FINISH: ({"session", "messages", "candidate"}, {"messages", "runtime", "workspace"}),
    Stage.VERIFY: ({"session", "candidate"}, {"messages", "runtime", "workspace", "verification"}),
    Stage.AFTER_FINISH: ({"session", "candidate", "verification"}, {"messages", "runtime"}),
}


class Pipeline:
    """校验装配与决定，再按登记顺序执行当前角色的阶段组件。"""

    def __init__(self, components: Iterable[Component], required: Iterable[Component], role: Role):
        """为 role 校验已排序 components；required 必须原样装配，声明无效时抛 ValueError。"""
        entries, required = tuple(components), tuple(required)
        names = {item.name for item in entries}
        if len(names) != len(entries):
            raise ValueError("生命周期组件标识重复")
        for item in required:
            if role in item.roles and not any(entry is item for entry in entries):
                raise ValueError(f"缺少必需的生命周期组件 {item.name}")
        self.stages: dict[Stage, list[Component]] = {stage: [] for stage in Stage}  # 本角色的有序阶段表。
        self._finish_sources = tuple(item for item in required if item.stage == Stage.START and role in item.roles)  # 可接续已有候选的宿主组件。
        seen: set[str] = set()
        for stage, (initial, outputs) in CONTRACTS.items():
            available = initial.copy()
            for item in entries:
                if not item.roles or not item.roles <= ROLES:
                    raise ValueError(f"生命周期组件角色无效 {item.name}")
                if item.stage != stage or role not in item.roles:
                    continue
                if stage == Stage.VERIFY and not any(item is entry for entry in required):
                    raise ValueError(f"独立验收阶段只允许宿主校验组件 {item.name}")
                if not set(item.after) <= seen or not item.reads <= available or not item.writes <= outputs:
                    raise ValueError(f"生命周期组件顺序或读写契约不符 {item.name}")
                if stage == Stage.INPUT and "input" in available and item.writes & {"messages", "memory", "handoff", "workspace"}:
                    raise ValueError(f"模型投影后只能修改请求副本或运行状态 {item.name}")
                self.stages[stage].append(item)
                available.update(item.writes)
                seen.add(item.name)
        if any(not isinstance(item.stage, Stage) for item in entries):
            raise ValueError("未知生命周期阶段")
        if len(self.stages[Stage.VERIFY]) != 1:
            raise ValueError("每个角色必须有且只有一个独立验收组件")
        for stage in (Stage.AFTER_TOOL, Stage.BEFORE_FINISH):
            protected = next((item for item in required if item.stage == stage and role in item.roles), None)
            if protected is not None and self.stages[stage][0] is not protected:
                raise ValueError(f"阶段必须先执行宿主门禁 {protected.name}")

    async def run(self, stage: Stage, session: "Session") -> Decision:
        """按 stage 调度已装配组件，返回首个非继续决定；仅持久化实际进入的组件及结果。"""
        trace: list[dict[str, str]] = []
        decision: Decision = Proceed()
        try:
            for item in self.stages[stage]:
                entry = {"name": item.name, "source": item.run.__module__, "outcome": "interrupted"}
                trace.append(entry)
                decision = await item.run(session)
                if not isinstance(decision, (Proceed, Replan, Wait, Finish, Abort)):
                    raise TypeError(f"生命周期组件未返回决定 {item.name}")
                if isinstance(decision, Finish) and not any(item is source for source in self._finish_sources):
                    raise ValueError(f"只有宿主接续组件可以直接进入已有交付 {item.name}")
                if stage in (Stage.INPUT, Stage.RESPONSE) and isinstance(decision, Replan):
                    raise ValueError(f"此阶段只允许继续或停止 {stage}")
                entry["outcome"] = decision.kind
                if not isinstance(decision, Proceed):
                    break
        except StorageError:
            raise  # 记录已不可用，禁止追加诊断掩盖原始错误。
        except BaseException as error:
            if trace:
                status = getattr(error, "status", None)
                reason = getattr(error, "reason", None)
                if isinstance(reason, str):
                    trace[-1]["reason"] = reason
                trace[-1]["outcome"] = "wait" if status == "waiting_input" else "interrupted"
                session.context.emit("pipeline_stage", {"stage": stage.value, "components": trace})
            raise
        if trace:
            if isinstance(decision, (Wait, Abort)):
                trace[-1]["reason"] = decision.reason
            session.context.emit("pipeline_stage", {"stage": stage.value, "components": trace})
        return decision


def apply_decision(session: "Session", decision: Decision) -> bool:
    """提交 decision 的反馈或停止信号；仅需重新请求模型时返回 True。"""
    from agent_harness.runtime.runner import AgentStopped

    if isinstance(decision, Wait):
        raise AgentStopped("waiting_input", decision.reason)
    if isinstance(decision, Abort):
        raise AgentStopped(decision.status, decision.reason)
    if isinstance(decision, Replan):
        if decision.feedback is not None:
            session.append({"role": "user", "content": decision.feedback}, "pipeline_feedback")
        return True
    return False
