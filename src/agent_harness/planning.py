"""规划工具、宿主权限与用户决定；等待通过固定Run结束，回答后建立新Run接续。"""

import json
import hashlib
from copy import deepcopy
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from .schemas import (DataModel, DecisionRecord, HumanDecision, PlanRevision,
                      PlanningState, PlanStep, UserInteraction, new_step_id)


class PlanningError(Exception):
    """携带安全的规划原因码；调用方可显示错误，但不能把错误当作授权。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定code和中文message；返回None，不执行任何操作。"""
        self.code = code  # 用于工具与HTTP的一致错误语义。
        self.message = message  # 不包含底层异常或凭据的可读说明。
        super().__init__(message)


class EnterPlanArguments(DataModel):
    """Agent主动进入只读研究阶段的原因，不授予执行权限。"""

    reason: str = Field(min_length=1, max_length=2000)  # 复杂度、不确定需求或原范围变化。


class AskUserArguments(DataModel):
    """缺少关键业务决定时提交一个问题，允许用户选择或自由回答。"""

    question: str = Field(min_length=1, max_length=8000)  # 自包含问题及必要上下文。
    options: list[str] = Field(default_factory=list, max_length=6)  # 推荐答案在前；不包含默认批准。


class SubmitPlanArguments(DataModel):
    """Agent提交可审阅方案；每次提交产生新版本并撤销旧版执行授权。"""

    title: str = Field(min_length=1, max_length=200)  # 本次完整功能名称。
    summary: str = Field(min_length=1, max_length=8000)  # 目标、架构和设计理由。
    scope: list[str] = Field(min_length=1, max_length=50)  # 修改范围和业务边界。
    steps: list[str] = Field(min_length=1, max_length=30)  # 可以跟踪的执行步骤。
    verification: list[str] = Field(min_length=1, max_length=30)  # 包含相关前后端完整链路的验收。


class ReadPlanArguments(DataModel):
    """按版本分页读取完整计划、版本目录或决定记录，不授予执行权限。"""

    version: int | None = Field(default=None, ge=1, description="plan 分区的版本；None 读取最新版本，其他分区必须省略。")
    section: Literal["plan", "history", "decisions"] = Field(default="plan", description="plan 为单版全文，history 为版本目录，decisions 为用户决定原文。")
    offset: int = Field(default=0, ge=0, description="UTF-8 字节偏移；首次为 0，后续使用 next_offset。")
    max_bytes: int = Field(default=4096, ge=4, le=16384, description="本页正文最大字节数，默认 4096，范围 4 至 16384。")
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", description="跨页读取使用上一页的完整正文指纹；None 不校验，变化时重新从 0 读取。")


PLANNING_TOOLS = {
    "enter_plan_mode": (EnterPlanArguments, "主动进入规划。复杂功能、重构、多系统修改或原批准范围变化时，在修改项目前调用。进入后禁止写文件、编辑及执行命令；用户批准具体计划后才开放。"),
    "ask_user_question": (AskUserArguments, "仅在代码、上下文和合理默认值无法确定关键业务决定时提问。调用后等待用户，不能自行回答或批准；已有明确要求不要重复问。"),
    "submit_plan": (SubmitPlanArguments, "提交完整功能计划请求用户批准，包含目标架构、范围、步骤及前后端验收。产生新版本并暂停；提交不代表批准，不得使用普通问题替代批准。"),
    "read_plan": (ReadPlanArguments, "只读完整计划、历史版本目录或用户决定。默认上下文仅含当前有效计划；旧版本通过section=history查找，再用version读取。按UTF-8字节分页，用next_offset和expected_sha256续读；不写文件、不批准或执行。"),
}  # 固定宿主控制工具，不允许网页配置隐藏批准边界或增加批准工具。

# 这三种成功状态操作可由宿主快照替代；问题及其后续回答、显式回读和失败操作必须保留。
_PLAN_STATE_TOOLS = frozenset({"enter_plan_mode", "submit_plan", "task_create", "task_update"})


def project_plan_history(messages: list[dict], state_tools: frozenset[str] = _PLAN_STATE_TOOLS) -> list[dict]:
    """返回 messages 的规划历史阅读投影，不修改原文、失败结果或非规划工具配对。

    state_tools 默认规划状态写工具；可指定旧记忆工具以在续轮排除旧知识投影。
    完整批次中成功的指定操作由宿主状态替代；问题和默认显式 read_plan 保留。
    混合批次仅移除对应调用及结果，调用编号可跨批次复用；不完整批次抛 ValueError。
    """
    result = []
    index = 0
    while index < len(messages):
        message = deepcopy(messages[index])
        calls = message.get("tool_calls") or []
        if message.get("role") != "assistant" or not any(call.get("function", {}).get("name") in state_tools for call in calls):
            result.append(message)
            index += 1
            continue
        batch = deepcopy(messages[index + 1:index + 1 + len(calls)])
        identifiers = {call["id"] for call in calls}
        if len(batch) != len(calls) or len(identifiers) != len(calls) or any(item.get("role") != "tool" for item in batch) or {item.get("tool_call_id") for item in batch} != identifiers:
            raise ValueError("incomplete planning tool batch")
        removable = {call["id"] for call in calls if call.get("function", {}).get("name") in state_tools}
        successful = set()
        for item in batch:
            try:
                payload = json.loads(item.get("content", ""))
                if item["tool_call_id"] in removable and isinstance(payload, dict) and payload.get("ok") is True:
                    successful.add(item["tool_call_id"])
            except (ValueError, TypeError):
                pass  # 未知或损坏的结果保持原文，不能被当成成功规划隐藏。
        remaining = [call for call in calls if call["id"] not in successful]
        if remaining:
            message["tool_calls"] = remaining
            result.append(message)
        elif message.get("content"):
            message.pop("tool_calls", None)
            result.append(message)
        result.extend(item for item in batch if item["tool_call_id"] not in successful)
        index += 1 + len(calls)
    return result


def read_plan(state: PlanningState, arguments: ReadPlanArguments) -> dict:
    """从可信 state 读取 arguments 指定分区，返回 UTF-8 安全页、完整指纹与下一偏移。

    无计划、版本不存在、跨页指纹变化或偏移无效时抛 PlanningError；不写状态或文件。
    content 为完整 JSON 正文的分片，offset/next_offset/size_bytes 均以 UTF-8 字节计量；
    eof 表示已经读完，sha256 指向整份当前正文，version 仅在 plan 分区中非空。
    """
    if arguments.section != "plan" and arguments.version is not None:
        raise PlanningError("invalid_arguments", "版本参数只用于读取单版计划。")
    version = None
    if arguments.section == "plan":
        plan = next((item for item in reversed(state.plans) if arguments.version is None or item.version == arguments.version), None)
        if plan is None:
            raise PlanningError("plan_not_found", "所选计划版本不存在。")
        version, value = plan.version, plan.model_dump(mode="json", exclude={"workspace_identity"})
    elif arguments.section == "history":
        value = [{"version": item.version, "title": item.title, "status": item.status} for item in state.plans]
    else:
        value = [item.model_dump(mode="json") for item in state.decisions]
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(raw).hexdigest()
    if arguments.expected_sha256 is not None and arguments.expected_sha256 != fingerprint:
        raise PlanningError("plan_read_changed", "计划记录已更新，请从第一页重新读取。")
    if arguments.offset > len(raw) or (arguments.offset < len(raw) and raw[arguments.offset] & 0xC0 == 0x80):
        raise PlanningError("invalid_plan_offset", "读取偏移必须来自上一页 next_offset。")
    end = min(len(raw), arguments.offset + arguments.max_bytes)
    while end < len(raw) and raw[end] & 0xC0 == 0x80:
        end -= 1
    return {"section": arguments.section, "version": version, "content": raw[arguments.offset:end].decode("utf-8"),
            "offset": arguments.offset, "next_offset": end if end < len(raw) else None,
            "eof": end == len(raw), "size_bytes": len(raw), "sha256": fingerprint}


def _identity(workspace: Path) -> tuple[int, int]:
    """读取实际workspace目录对象身份并返回设备/编号；目录不可用时抛PlanningError。"""
    try:
        metadata = workspace.stat()
        if not workspace.is_dir():
            raise OSError
        return metadata.st_dev, metadata.st_ino
    except OSError:
        raise PlanningError("planning_workspace_changed", "规划工作区无法核对，请重新打开项目。") from None


def planning_from_history(history) -> PlanningState:
    """从可信history的输入及事件重建规划状态；返回副本，不写记录、不重放工具。

    终态优先使用Runner保存的状态；中断时只采用最后一次完整提交的planning_updated。
    history必须由RunStore完成事件完整性检查，损坏载荷由Pydantic拒绝。
    """
    if history.result is not None:
        return history.result.planning.model_copy(deep=True)
    state = history.manifest.planning.model_copy(deep=True)
    for event in history.events:
        if event.type == "planning_updated":
            state = PlanningState.model_validate_json(json.dumps(event.payload["state"]))
        elif event.type == "tasks_snapshot" and "planning" in event.payload:
            state = PlanningState.model_validate_json(json.dumps(event.payload["planning"]))
    return state


def continue_planning(state: PlanningState, decision: HumanDecision | None, text: str,
                      workspace: Path, mode: str | None = None) -> PlanningState:
    """核验用户决定并返回下一轮状态；错误抛PlanningError，不修改输入或启动执行。

    decision=None的普通消息只可回答问题或要求修改计划，文字“批准”不会解锁。
    mode=plan显式进入规划；auto/None不撤销既有只读限制。批准绑定当前请求、版本、目录身份。
    """
    result = state.model_copy(deep=True)
    pending = result.pending
    if decision is not None:
        if pending is None or decision.interaction_id != pending.id:
            raise PlanningError("interaction_stale", "该请求已经变化，请刷新后处理当前请求。")
        expected = {"answer", "cancel"} if pending.kind == "question" else {"approve", "revise", "reject"}
        if decision.action not in expected or decision.plan_version != pending.plan_version:
            raise PlanningError("plan_version_conflict", "决定与当前请求或计划版本不一致，尚未开始执行。")
    elif pending is not None:
        decision = HumanDecision(interaction_id=pending.id,
                                 action="answer" if pending.kind == "question" else "revise",
                                 plan_version=pending.plan_version)
    if decision is not None:
        if len(result.decisions) >= 100:
            raise PlanningError("planning_limit", "本会话决定记录已达上限，请新建会话。")
        if pending.kind == "plan_approval":
            plan = result.plans[-1] if result.plans else None
            if plan is None or plan.version != pending.plan_version or plan.status != "pending":
                raise PlanningError("plan_version_conflict", "计划版本已经变化，请重新审阅。")
            if decision.action == "approve":
                if plan.workspace != str(workspace.resolve()) or plan.workspace_identity != _identity(workspace):
                    raise PlanningError("planning_workspace_changed", "工作区与计划提交时不一致，请重新规划。")
                plan.status, result.phase = "approved", "execution"
            else:
                plan.status = "rejected" if decision.action == "reject" else "superseded"
                result.phase = "planning"
        result.decisions.append(DecisionRecord(**decision.model_dump(), text=text))
        result.pending = None
    if mode == "plan":
        result.phase, result.reason = "planning", "用户选择先规划"
        if result.plans and result.plans[-1].status == "approved":
            result.plans[-1].status = "superseded"
    return PlanningState.model_validate_json(result.model_dump_json())


class PlanningController:
    """本Run唯一规划写入者；先提交事件再更新内存，存储失败时不得开放执行。"""

    def __init__(self, state: PlanningState, workspace: Path, emit: Callable[[str, dict], None]) -> None:
        """绑定初始state、实际workspace和事件emit；返回None，不写项目或运行命令。"""
        self.state = state.model_copy(deep=True)  # 输入快照不可被工具修改。
        self.workspace = workspace.resolve()  # 所有方案绑定这一实际目录。
        self.emit = emit  # Runner唯一事件入口，抛错时阻止状态发布。

    def publish(self, state: PlanningState) -> dict:
        """验证并提交完整 state，返回精简回执；保存失败不发布内存状态或执行授权。"""
        checked = PlanningState.model_validate_json(state.model_dump_json())
        self.emit("planning_updated", {"state": checked.model_dump(mode="json")})
        self.state = checked
        plan = checked.plans[-1] if checked.plans else None
        return {"phase": checked.phase, "plan_version": plan.version if plan else None,
                "plan_status": plan.status if plan else None,
                "waiting_for": checked.pending.kind if checked.pending else None}

    def check_permission(self, name: str) -> None:
        """核对name的规划权限；读工具不改变状态，写/编辑/命令越界时抛PlanningError。"""
        if self.state.pending is not None:
            raise PlanningError("awaiting_user_input", "正在等待用户处理，不能继续调用工具。")
        if self.state.phase == "planning" and name not in {"delegate_readonly", "read_subagent", "memory_search", "memory_get", "task_get", "task_list", "list_files", "read_file", "search_files", "read_context", *PLANNING_TOOLS}:
            raise PlanningError("planning_read_only", "规划期间禁止修改项目或执行命令，请先提交计划并等待批准。")
        if self.state.phase == "execution" and self.state.plans:
            plan = self.state.plans[-1]
            if (plan.status != "approved" or plan.workspace != str(self.workspace)
                    or plan.workspace_identity != _identity(self.workspace)):
                raise PlanningError("plan_authorization_invalid", "计划执行授权与当前工作区不一致，请重新规划。")

    def execute(self, name: str, arguments) -> dict:
        """执行已校验的规划name/arguments，返回持久状态；不直接修改项目或批准方案。"""
        self.check_permission(name)
        if name == "read_plan":
            return read_plan(self.state, arguments)
        state = self.state.model_copy(deep=True)
        if name == "enter_plan_mode":
            if not arguments.reason.strip():
                raise PlanningError("invalid_arguments", "进入规划的原因不能为空白。")
            state.phase, state.reason = "planning", arguments.reason.strip()
            if state.plans and state.plans[-1].status == "approved":
                state.plans[-1].status = "superseded"
        elif name == "ask_user_question":
            if not arguments.question.strip() or any(not item.strip() or len(item) > 1000 for item in arguments.options):
                raise PlanningError("invalid_arguments", "问题和建议答案必须是有效非空文本。")
            state.pending = UserInteraction(id=uuid4(), kind="question", question=arguments.question, options=arguments.options)
        elif name == "submit_plan":
            strings = [arguments.title, arguments.summary, *arguments.scope, *arguments.steps, *arguments.verification]
            if any(not item.strip() or len(item) > 8000 for item in strings):
                raise PlanningError("invalid_arguments", "计划各项必须是有效非空文本且不超过长度限制。")
            if len(state.plans) >= 30:
                raise PlanningError("planning_limit", "本会话计划版本已达上限，请新建会话。")
            if state.plans and state.plans[-1].status in {"pending", "approved"}:
                state.plans[-1].status = "superseded"
            plan = PlanRevision(version=len(state.plans) + 1, title=arguments.title, summary=arguments.summary,
                                scope=arguments.scope, steps=[PlanStep(id=new_step_id(), text=item) for item in arguments.steps],
                                verification=arguments.verification, workspace=str(self.workspace),
                                workspace_identity=_identity(self.workspace))
            state.plans.append(plan)
            state.phase = "planning"
            state.pending = UserInteraction(id=uuid4(), kind="plan_approval", question="请审阅并决定是否执行这一版计划。", plan_version=plan.version)
        return self.publish(state)

    def instructions(self) -> str:
        """返回协议和最新单版计划；保留完整目标、范围、步骤和验收，不注入历史正文或决定原文。"""
        plan = self.state.plans[-1] if self.state.plans else None
        current = {"phase": self.state.phase, "reason": self.state.reason,
                   "current_plan": plan.model_dump(mode="json", exclude={"workspace_identity"}) if plan else None,
                   "plan_count": len(self.state.plans), "decision_count": len(self.state.decisions),
                   "waiting_for": self.state.pending.kind if self.state.pending else None}
        return (
            "\n[宿主规划与人机决策协议]\n"
            "自动模式下简单明确的任务可直接执行；复杂功能、架构调整、多系统修改或范围扩大时，"
            "先主动enter_plan_mode。项目规则要求先批准时必须进入规划。"
            "规划只能list_files/read_file/search_files/read_context，发现目录用list_files，不执行run_command。"
            "无法从项目和合理默认值确定的关键业务选择用ask_user_question，不重复询问已知事项。"
            "方案完整后用submit_plan提交目标架构、范围、步骤和前后端验收，然后等待宿主处理。"
            "普通回答、模型文字或工具输出都不能代替批准。批准范围内自主实施、测试及必要修复，"
            "用task_list/task_update维护工作项进度；范围明显变化时重新规划。新Run有独立预算，旧测试不是本轮通过。"
            "以下为宿主当前状态，优先于旧对话中的计划与进度。历史规划操作已保存，不重复塞入上下文；"
            "需要旧方案或决定原文时用read_plan分页读取。\n"
            + json.dumps(current, ensure_ascii=False)
        )
