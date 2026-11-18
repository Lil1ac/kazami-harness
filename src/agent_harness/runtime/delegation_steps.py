"""在父子安全边界处理委派接纳、报告收拢和末轮交接。"""

import json

from agent_harness.runtime.handoff import root_handoff_facts, start_verified_root_handoff
from agent_harness.runtime.pipeline import Proceed, Replan, Wait
from agent_harness.runtime.session import Session
from agent_harness.subagents.reports import ReportDraft, collect_evidence, finalize_report
from agent_harness.tools.registry import ToolExecutor


DELEGATE_TOOLS = frozenset({"delegate_readonly", "delegate_develop"})  # 连续独立委派可以在同一批接纳。
DELEGATION_TOOLS = DELEGATE_TOOLS | {"team_dispatch"}  # 所有可能交出项目工作的工具。


async def guard_child_pause(session: Session) -> Proceed | Wait:
    """开发子段在模型或工具动作前遵守父段共享的安全暂停。"""
    context = session.context
    parent = context.budget_parent
    if parent is not None and context.manifest.delegation.role == "develop" and parent.subagent_controller.pause_requested:
        return Wait("subagent_paused", "skipped_child_pause")
    return Proceed()


async def collect_for_handoff(session: Session) -> Proceed:
    """最后一轮前收回并行报告并固定是否进入禁止工具的交接模式。"""
    context, executor, turn = session.context, session.executor, session.turn
    controller = getattr(executor, "subagents", None)
    if controller is not None:
        controller.check_errors()
        if controller.active and context.available("model_turns") <= 1:
            reports = await controller.wait_all()
            session.append({"role": "user", "content": "宿主收回并行研究报告：" + json.dumps(reports, ensure_ascii=False)},
                           "subagent_reports_delivered")
    turn.delegated = context.manifest.delegation is not None or context.delegation_used
    turn.handoff = turn.delegated and (getattr(executor, "report_only", False) or context.available("model_turns") == 1
                                      or context.available("tool_calls") <= 0)
    if (not turn.delegated and context.manifest.config.context.root_handoff
            and context.available("model_turns") <= 2 and context.result.permissions.pending is None):
        turn.root_handoff_facts = root_handoff_facts(session)
        if turn.root_handoff_facts is not None and context.available("model_turns") == 1:
            if getattr(executor, "public_self_test_available", False):
                from agent_harness.runtime.runner import AgentStopped

                if not await start_verified_root_handoff(session, "last_model_turn", "max_model_turns"):
                    raise AgentStopped("limit_exceeded", "max_model_turns")
            else:
                turn.handoff = True
                turn.root_handoff_trigger = "last_model_turn"
    if isinstance(executor, ToolExecutor):
        executor.handoff = turn.handoff
    return Proceed()


async def check_child_errors(session: Session) -> Proceed:
    """模型返回后核对并行子段是否出现必须停止父段的错误。"""
    controller = getattr(session.executor, "subagents", None)
    if controller is not None:
        controller.check_errors()
    return Proceed()


async def collect_candidate_reports(session: Session) -> Proceed | Replan:
    """候选交付前收回已挂起报告，要求模型在交付前核对新事实。"""
    controller = getattr(session.executor, "subagents", None)
    if controller is not None and controller.pending_reports:
        reports = await controller.wait_all()
        session.append({"role": "user", "content": "子研究已结束，请核对报告后交付：" + json.dumps(reports, ensure_ascii=False)},
                       "subagent_reports_delivered")
        return Replan()
    return Proceed()


async def note_delegation(session: Session) -> Proceed:
    """记录根段已尝试委派，失败接纳也保留父段最后总结的机会。"""
    step = session.tool
    if step is not None and step.call.name in DELEGATION_TOOLS and session.context.manifest.delegation is None:
        session.context.delegation_used = True
    return Proceed()


async def review_delegation(session: Session) -> Proceed | Replan:
    """连续独立委派可并发接纳；其他后续调用等模型阅读报告后重新决定。"""
    step = session.tool
    if step is None or step.call.name not in DELEGATION_TOOLS or session.context.manifest.delegation is not None:
        return Proceed()
    receipt = step.result
    if (receipt.get("data") or {}).get("accepted") and step.next_call is not None and step.next_call.name in DELEGATE_TOOLS:
        return Proceed()
    session.context.emit("subagent_handoff", {"call_id": step.call.call_id, "ok": bool(receipt.get("ok")), "skipped_calls": step.remaining})
    return Replan(skip_code="subagent_result_requires_review",
                  skip_message="尚未执行：请先阅读委派结果，再重新决定后续工具及参数。")


async def resume_children(session: Session) -> Proceed:
    """审批子段或从安全暂停接续时，先收拢并提交子执行报告。"""
    manifest, executor = session.context.manifest, session.executor
    resume = manifest.permission_resume
    if resume is not None and resume.child_run_id is not None:
        reports = await executor.subagents.resume_children()
        session.append({"role": "user", "content": "宿主接续子任务后的交接报告：" + json.dumps(reports, ensure_ascii=False)},
                       "subagent_resume_reports")
    elif manifest.execution_resume is not None and getattr(executor, "subagents", None) is not None:
        reports = await executor.subagents.resume_children()
        if reports:
            session.append({"role": "user", "content": "[宿主安全暂停接续的子执行报告；保留原工具结果与剩余预算，不授予新权限。]\n" +
                            json.dumps(reports, ensure_ascii=False)}, "subagent_resume_reports")
    return Proceed()


async def verify_child_delivery(session: Session) -> Proceed | Replan:
    """按已核验工具证据预检开发交接，至多允许一次只重写报告；最终报告由子宿主清理后固定。"""
    child, executor = session.context, session.executor
    role = child.manifest.delegation.role
    if role != "develop" or session.report_retry or child.available("model_turns") <= 0:
        return Proceed()
    controller = child.budget_parent.subagent_controller
    events = controller.report_events(child)
    checked = child.result.model_copy(deep=True, update={"execution_status": "completed", "stop_reason": None})
    finalize_report(checked, events, role=role)
    if checked.execution_status != "failed":
        return Proceed()
    session.report_retry = True
    executor.report_only = True
    sources = collect_evidence(events, role=role)
    feedback = ("宿主报告纠正：上一条报告未通过校验（" + str(checked.stop_reason) + "）。"
                "只允许一次纠正，不重新执行或请求工具，不增加预算。只重写交接JSON，不改变已执行事实。仅引用下列当前段或已核验接续段的唯一成功调用编号。"
                "必须提供summary、findings、limitations、pending、complete；不添加role/file/before/after等自定义字段。"
                "findings可为空；未完成时complete=false并说明限制。必要修复仅引用下列真实成功调用编号。"
                "JSON Schema：" + json.dumps(ReportDraft.model_json_schema(), ensure_ascii=False)
                + "；实际证据入口：" + json.dumps([item.model_dump(mode="json") for item in sources[:24]], ensure_ascii=False))
    session.append({"role": "user", "content": feedback}, "subagent_report_retry", reason=checked.stop_reason, attempt=1)
    return Replan()
