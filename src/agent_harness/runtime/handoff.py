"""提取本段可复核的末轮交付事实，并按原运行预算提交收尾答复。"""

import asyncio
from hashlib import sha256
import json
from typing import TYPE_CHECKING, Literal

from agent_harness.execution.workspace import WorkspaceError
from agent_harness.tools.files import read_file_snapshot

if TYPE_CHECKING:
    from agent_harness.runtime.session import Session


def root_handoff_facts(session: "Session") -> dict | None:
    """从 session 本段回执提取仍匹配工作区的写入事实；没有可核对写入时返回 None。

    文件只读校验按原回执字节数执行。命令仅提供近期参数预览与退出事实，保留回执编号供追溯。
    """
    context = session.context
    writes: dict[str, dict] = {}  # 每个路径只保留最后一次提交的文件回执。
    commands: list[dict] = []  # 本段通过命令工具返回的退出事实。
    calls: dict[str, dict] = {}  # 同批配对；后续模型响应可重新使用编号。
    for message in session.messages[session.initial_message_count:]:
        if message["role"] == "assistant":
            calls = {item["id"]: item["function"] for item in message.get("tool_calls") or []}
            continue
        if message["role"] != "tool":
            continue
        call_id = message["tool_call_id"]
        call = calls.get(call_id)
        if call is None or call["name"] not in {"write_file", "edit_file", "run_command"}:
            continue
        receipt = json.loads(message["content"])
        data = receipt.get("data") or {}
        if call["name"] == "run_command":
            if type(data.get("exit_code")) is int:
                commands.append({
                    "call_id": call_id,
                    "arguments_preview": call["arguments"][:400],
                    "exit_code": data["exit_code"],
                    "timed_out": bool(data.get("timed_out")),
                    "error": (receipt.get("error") or {}).get("code"),
                })
            continue
        if receipt.get("ok") is not True or not isinstance(data.get("path"), str):
            continue
        size = data.get("bytes_written")
        if type(size) is not int or not 0 <= size <= context.manifest.config.limits.max_file_write_bytes:
            continue
        args = json.loads(call["arguments"])
        digest = data.get("sha256")
        if call["name"] == "write_file":
            content = args.get("content")
            if not isinstance(content, str):
                continue
            digest = sha256(content.encode("utf-8")).hexdigest()
        elif digest == args.get("expected_sha256"):
            continue  # 相同指纹的替换不能作为实际修改证据。
        if isinstance(digest, str):
            writes[data["path"]] = {"path": data["path"], "sha256": digest, "bytes_written": size, "call_id": call_id}
    confirmed = []
    for fact in writes.values():
        try:
            content = read_file_snapshot(context.manifest.task.workspace, context.runs_root,
                                         fact["path"], fact["bytes_written"])
        except (WorkspaceError, OSError):
            continue
        if sha256(content).hexdigest() == fact["sha256"]:
            confirmed.append(fact)
    return {"writes": confirmed, "recent_commands": commands[-8:]} if confirmed else None



def _root_handoff_blocked(session: "Session") -> str | None:
    """核对 session 根段能否继续触发副作用；返回阻止原因，无待处理风险时返回 None。"""
    context, executor = session.context, session.executor
    if context.result.permissions.pending is not None:
        return "approval_pending"
    control = context.runtime_control
    if control is not None and (control.pausing() or control.available_inputs()):
        return "runtime_interrupted"
    if (context.result.cleanup_status not in {"complete", "not_needed"}
            or getattr(executor, "cleanup_status", "not_needed") not in {"complete", "not_needed"}
            or getattr(context.command_controller, "active", False)):
        return "cleanup_unconfirmed"
    if context.available("model_turns") <= 0:
        return "model_budget_exhausted"
    if asyncio.get_running_loop().time() >= context.deadline:
        return "time_budget_exhausted"
    return None


def _published_self_test(fact: dict | None) -> dict | None:
    """从 fact 提取绑定候选且实际通过的公开自测事实；无可信通过证据时返回 None。"""
    if (fact is None or fact.get("status") != "passed" or type(fact.get("tests_run")) is not int
            or fact["tests_run"] <= 0 or type(fact.get("exit_code")) is not int or fact["exit_code"] != 0):
        return None
    digest, selector = fact.get("candidate_sha256"), fact.get("selector")
    if (not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest)
            or not isinstance(selector, str) or not selector):
        return None
    # 仅把核验事实带入模型；原始日志和宿主产物位置仍经 Run 回执查看。
    return {"status": "passed", "candidate_sha256": digest, "selector": selector,
            "tests_run": fact["tests_run"], "exit_code": fact["exit_code"]}


async def start_verified_root_handoff(
    session: "Session", trigger: Literal["last_model_turn", "input_rejected"], limit_reason: str,
) -> bool:
    """为 session 尝试唯一一次经公开自测核验的紧凑收尾，成功时固定事实并返回 True。

    trigger 记录末轮或普通输入被拒；limit_reason 用于部分交付时保留原预算原因。
    已有通过回执须匹配当前候选，否则最多补一次真实自测，消耗本 Run 一个工具额度。
    返回 False 会保存阻止原因且不发送任何模型请求。共享环境异常停止 Run，超时与取消照常传播。
    """
    from agent_harness.runtime.runner import AgentStopped

    context, executor = session.context, session.executor

    def skip(reason: str, fact: dict | None = None) -> bool:
        """持久化阻止原因 reason 与可选自测 fact 的状态；固定返回 False 供调用方停止。"""
        payload = {"mode": "root", "trigger": trigger, "reason": reason}
        if fact is not None:
            payload["public_self_test_status"] = fact.get("status")
        context.emit("budget_handoff_skipped", payload)
        return False

    if session.verified_handoff_attempted:
        return skip("handoff_already_attempted")
    blocked = _root_handoff_blocked(session)
    if blocked:
        return skip(blocked)
    facts = root_handoff_facts(session)
    if not facts:
        return skip("no_verified_write")
    session.verified_handoff_attempted = True

    fact = executor.public_self_test_facts()
    proof = _published_self_test(fact)
    if proof is None:
        permissions = getattr(executor, "permissions", None)
        if permissions is None or permissions.decision("run_command") != "allow":
            return skip("self_test_permission_denied")
        if context.available("tool_calls") <= 0:
            return skip("tool_budget_exhausted")
        # 留出按 Run 既有设置执行单次模型收尾的时间；测试本身最多三十秒。
        limits = context.manifest.config.limits
        timeout = min(30.0, limits.command_timeout_seconds,
                      context.deadline - asyncio.get_running_loop().time() - limits.model_turn_timeout_seconds)
        if timeout <= 0:
            return skip("time_budget_exhausted")
        await asyncio.sleep(0)
        blocked = _root_handoff_blocked(session)
        if blocked:
            return skip(blocked)
        if root_handoff_facts(session) != facts:
            return skip("no_verified_write")
        # 直接执行同一宿主测试，不造一个供应商的 tool_call 或虚构已完成回执。
        context.consume("tool")
        outcome = await executor.run_public_self_test(source="handoff", timeout_seconds=timeout)
        if outcome.get("reason") == "self_test_environment":
            skip("self_test_environment", outcome)
            raise AgentStopped("failed", "self_test_environment")
        blocked = _root_handoff_blocked(session)
        if blocked:
            return skip(blocked, outcome)
        if _published_self_test(outcome) is None:
            return skip("self_test_not_passed", outcome)
        proof = _published_self_test(executor.public_self_test_facts())
        if proof != _published_self_test(outcome):
            return skip("self_test_not_current", outcome)

    blocked = _root_handoff_blocked(session)
    if blocked:
        return skip(blocked, fact)
    if root_handoff_facts(session) != facts:
        return skip("no_verified_write")
    session.turn.root_handoff_facts = {**facts, "public_self_test": proof}
    session.turn.root_handoff_trigger = trigger
    session.turn.root_handoff_limit_reason = limit_reason
    session.turn.handoff = True
    return True


def _verified_handoff_current(session: "Session") -> bool:
    """返回 session 已封存的公开核验仍与当前候选一致，旧式末轮收尾不要求公开自测。"""
    facts = session.turn.root_handoff_facts or {}
    expected = facts.get("public_self_test")
    return expected is None or (
        _published_self_test(session.executor.public_self_test_facts()) == expected
        and (root_handoff_facts(session) or {}).get("writes") == facts["writes"]
    )


def recheck_verified_handoff_request(session: "Session") -> None:
    """在 session 获得模型槽位后、预留账本前重新核对自测和运行状态。

    没有新异常时返回 None；证据过期或暂停等情况先保存阻止回执，再按相应状态停止，
    因此排队期间变化不能让旧候选获得费用预留或发单。
    """
    from agent_harness.runtime.runner import AgentStopped

    context, turn = session.context, session.turn
    blocked = _root_handoff_blocked(session)
    if blocked is None and _verified_handoff_current(session):
        return
    context.emit("budget_handoff_skipped", {"mode": "root", "trigger": turn.root_handoff_trigger,
                                            "reason": blocked or "self_test_not_current"})
    if blocked == "time_budget_exhausted":
        raise AgentStopped("timed_out", "run_timeout")
    raise AgentStopped("limit_exceeded", turn.root_handoff_limit_reason)


def record_root_handoff_finished(session: "Session", status: str, *, reason: str | None = None) -> None:
    """保存 session 真实收尾状态 status 及原触发条件；reason 可选，为不能交付的稳定原因。"""
    context, turn = session.context, session.turn
    payload = {"mode": "root", "status": status, "model_turn": context.request_turn}
    if turn.root_handoff_trigger is not None:
        payload["trigger"] = turn.root_handoff_trigger
    if reason is not None:
        payload["reason"] = reason
    context.emit("budget_handoff_finished", payload)


def finish_root_handoff(session: "Session", text: str) -> str:
    """解析 text 中的交付声明并返回答复；无效或部分完成时保存事实并抛出限额终态。"""
    from agent_harness.runtime.runner import AgentStopped

    context = session.context
    raw = text.strip()
    if raw.startswith("```json") and raw.endswith("```"):
        raw = raw[7:-3].strip()
    try:
        response = json.loads(raw)
    except ValueError:
        response = None
    status = response.get("status") if isinstance(response, dict) else None
    answer = response.get("answer") if isinstance(response, dict) else None
    valid = status in ("ready", "partial") and isinstance(answer, str) and bool(answer.strip())
    if not valid:
        record_root_handoff_finished(session, "invalid", reason="handoff_invalid_response")
        raise AgentStopped("limit_exceeded", "handoff_invalid_response")
    limit_reason = session.turn.root_handoff_limit_reason
    if status == "partial":
        record_root_handoff_finished(session, "partial", reason=limit_reason)
        context.result.final_answer = answer.strip()
        raise AgentStopped("limit_exceeded", limit_reason)
    if not _verified_handoff_current(session):
        record_root_handoff_finished(session, "partial", reason="self_test_not_current")
        raise AgentStopped("limit_exceeded", limit_reason)
    record_root_handoff_finished(session, "ready")
    return answer.strip()
