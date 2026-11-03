"""真实Runner与文件工具验证角色隔离、预算及结构化委派交接，全程离线。"""

from tests.helpers.streaming import model_stream_response

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_harness.runtime.runner import run_task
from tests.helpers.permissions import trusted_config
from agent_harness.schemas import RunConfig, RunLimits, RunManifest, TaskSpec
from agent_harness.storage.runs import StorageError
from agent_harness.subagents.service import SubagentError, read_child, report_page
from tests.runtime.test_runner import call, response, run_script
from tests.web.test_planning_api import ScriptModel, call as tool_call, reply


def setup_run(tmp_path: Path, **limits) -> RunManifest:
    """创建tmp_path独立代码夹具并返回清单；limits覆盖父总预算，不访问用户项目。"""
    (tmp_path / "api.py").write_text("def value():\n    return 42\n", encoding="utf-8")
    return RunManifest(run_id=uuid4(), task=TaskSpec(prompt="研究接口后生成result.txt。", workspace=tmp_path),
                       config=trusted_config(tools=["read_file", "search_files", "write_file", "edit_file", "run_command"],
                                        limits=RunLimits(max_model_turns=limits.pop("max_model_turns", 12), **limits)))


def delegate(**overrides) -> dict:
    """返回供应商格式委派调用；overrides覆盖角色、技能与预算，默认只研究api.py。"""
    return call("delegate", "delegate_readonly", json.dumps({"title": "研究接口", "objective": "读取api.py，说明返回值。", **overrides}, ensure_ascii=False))


def report(summary="api.py返回42。", *, ids=(), complete=True, **overrides) -> str:
    """构造报告JSON；ids为实际子读取编号，complete标记完成，overrides覆盖固定字段。"""
    findings = [{"category": "observation", "title": "返回值", "detail": summary, "evidence_call_ids": list(ids)}] if ids else []
    return json.dumps({"summary": summary, "findings": findings, "limitations": [],
                       "pending": [] if complete else ["需要继续核对调用方。"], "complete": complete, **overrides}, ensure_ascii=False)


def test_independent_report_drives_parent_delivery_and_counts_once(tmp_path: Path):
    """父工具正文不进入子上下文，真实子报告驱动父文件交付，父子用量不重复累计。"""
    value = setup_run(tmp_path)
    (tmp_path / "private.txt").write_text("仅父历史有的工具正文", encoding="utf-8")
    outcome, requests, history = run_script(value, tmp_path / "records", [
        response(call("parent-read", arguments='{"path":"private.txt"}')), response(delegate()),
        response(call("child-read", arguments='{"path":"api.py"}')), response(content=report(ids=["child-read"])),
        response(call("parent-write", "write_file", '{"path":"result.txt","content":"42"}')), response(content="已使用研究结果。")])
    record = outcome.result.subagents[0]
    assert outcome.result.execution_status == "completed" and (tmp_path / "result.txt").read_text() == "42"
    assert record.report.summary == "api.py返回42。" and not record.report.partial
    assert record.report.evidence[0].path == "api.py" and record.report.evidence[0].sha256
    assert (record.model_turns, record.tool_calls, record.http_attempts) == (2, 1, 2)
    assert (outcome.result.model_turns, outcome.result.tool_calls, outcome.result.http_attempts) == (6, 4, 6)
    assert outcome.result.usage.total_tokens == 48 and record.usage["total_tokens"] == 16
    assert "仅父历史有的工具正文" not in json.dumps(requests[2], ensure_ascii=False)
    assert "api.py返回42" in json.dumps(requests[4], ensure_ascii=False)
    assert not any(message.get("tool_call_id") == "child-read" for message in requests[4]["messages"])
    child = read_child(tmp_path / "records", value.run_id, record.run_id)
    assert child.manifest.parent_run_id is None and child.manifest.delegation.parent_run_id == value.run_id
    assert child.result.subagent_report == record.report and history.result.subagents == outcome.result.subagents


@pytest.mark.parametrize("child_failure", [False, True])
def test_delegation_always_rejects_precomputed_same_batch_write(tmp_path: Path, child_failure: bool):
    """委派成功和失败均不执行报告产生前已生成的写入，父读取结果后才能重新提交新参数。"""
    script = [response(delegate(), call("stale-write", "write_file", '{"path":"stale.txt","content":"wrong"}')),
              400 if child_failure else response(content=report()),
              response(call("fresh-write", "write_file", '{"path":"result.txt","content":"reviewed"}')),
              response(content="重新决定后交付。")]
    outcome, requests, history = run_script(setup_run(tmp_path), tmp_path / "records", script)
    assert not (tmp_path / "stale.txt").exists() and (tmp_path / "result.txt").read_text() == "reviewed"
    skipped = next(event.payload["result"] for event in history.events if event.type == "tool_result" and event.payload["call_id"] == "stale-write")
    assert skipped["error"]["code"] == "subagent_result_requires_review"
    assert json.loads(requests[2]["messages"][-2]["content"])["ok"] is not child_failure
    assert outcome.result.tool_calls == 2


def test_host_whitelist_rejects_write_command_mutation_and_recursion(tmp_path: Path):
    """绕过声明的子写入、命令、任务/记忆修改及递归仍被宿主拒绝，不留下文件。"""
    names = ["write_file", "edit_file", "run_command", "task_update", "memory_propose", "delegate_readonly", "ask_user_question"]
    outcome, requests, _ = run_script(setup_run(tmp_path), tmp_path / "records", [response(delegate(max_tool_calls=10)),
        response(*(call(str(index), name, '{"path":"wrong.txt","content":"wrong","argv":["bad"]}') for index, name in enumerate(names))),
        response(content=report("无法执行超出只读范围的操作。", complete=False)), response(content="研究未完成。")])
    assert {tool["function"]["name"] for tool in requests[1]["tools"]} <= {"read_file", "search_files", "list_files", "memory_search", "memory_get", "read_context"}
    denied = [json.loads(message["content"])["error"]["code"] for message in requests[2]["messages"] if message["role"] == "tool"]
    assert denied == ["subagent_read_only"] * len(names)
    assert not (tmp_path / "wrong.txt").exists() and outcome.result.subagents[0].report.partial


def test_child_cannot_read_parent_records_or_parent_disabled_tools(tmp_path: Path):
    """嵌套日志仍受真实数据根保护，父未开放的搜索不能被子直接调用获得。"""
    value = setup_run(tmp_path)
    value.config.tools = ["read_file"]
    requested = f"records/{value.run_id}/run.json"
    _, requests, _ = run_script(value, tmp_path / "records", [response(delegate()),
        response(call("read-protected", arguments=json.dumps({"path": requested})), call("disabled", "search_files", '{"query":"value"}')),
        response(content=report("无法取得额外权限。", complete=False)), response(content="结束。")])
    errors = [json.loads(message["content"])["error"]["code"] for message in requests[2]["messages"] if message["role"] == "tool"]
    assert errors == ["run_records_forbidden", "subagent_read_only"]


def test_reserve_allows_parent_write_and_final_text_within_total(tmp_path: Path):
    """父5轮裁剪子到2轮；父子末轮均关闭工具，父实际交付，且没有额外调用。"""
    value = setup_run(tmp_path, max_model_turns=5)
    outcome, requests, _ = run_script(value, tmp_path / "records", [response(delegate(max_model_turns=12)),
        response(call("child-read", arguments='{"path":"api.py"}')), response(content=report(ids=["child-read"])),
        response(call("write", "write_file", '{"path":"result.txt","content":"42"}')), response(content="已实际交付42。")])
    record = outcome.result.subagents[0]
    assert record.max_model_turns == 2 and record.parent_reserve["model_turns"] == 2
    assert not requests[2].get("tools") and not requests[4].get("tools")
    assert outcome.result.model_turns == len(requests) == 5 and outcome.result.execution_status == "completed"
    assert (tmp_path / "result.txt").read_text() == "42"


def test_insufficient_budget_rejects_delegation_and_same_batch_write(tmp_path: Path):
    """父保留额度后无法研究时拒绝接纳，也不继续执行同批写入。"""
    outcome, requests, history = run_script(setup_run(tmp_path, max_model_turns=3), tmp_path / "records", [
        response(delegate(), call("stale", "write_file", '{"path":"wrong.txt","content":"bad"}')), response(content="额度不足。")])
    assert not outcome.result.subagents and len(requests) == 2 and not (tmp_path / "wrong.txt").exists()
    error = next(event.payload["result"]["error"]["code"] for event in history.events if event.type == "tool_result")
    assert error == "subagent_budget_exhausted"


def test_last_child_turn_cannot_execute_tools_and_keeps_evidence(tmp_path: Path):
    """子末轮仍请求工具时宿主跳过，保留先前读取证据，不伪造完整报告。"""
    value = setup_run(tmp_path)
    outcome, requests, _ = run_script(value, tmp_path / "records", [response(delegate(max_model_turns=2)),
        response(call("read", arguments='{"path":"api.py"}')),
        response(call("forbidden-final", "write_file", '{"path":"wrong.txt","content":"bad"}')), response(content="研究未交接。")])
    record = outcome.result.subagents[0]
    assert not requests[2].get("tools") and not (tmp_path / "wrong.txt").exists()
    assert record.status == "limit_exceeded" and record.stop_reason == "handoff_tools_disabled"
    assert record.report.partial and record.report.evidence[0].tool_call_id == "read"


def test_parent_last_turn_cannot_bypass_handoff_with_write(tmp_path: Path):
    """父末轮即使供应商忽略空工具声明，也不得写入或增加额外模型额度。"""
    outcome, requests, history = run_script(setup_run(tmp_path, max_model_turns=5), tmp_path / "records", [
        response(delegate(max_model_turns=2)), response(call("read", arguments='{"path":"api.py"}')),
        response(content=report(ids=["read"])), response(call("fresh-read", arguments='{"path":"api.py"}')),
        response(call("late-write", "write_file", '{"path":"wrong.txt","content":"bad"}'))])
    assert outcome.result.execution_status == "limit_exceeded" and outcome.result.stop_reason == "handoff_tools_disabled"
    assert outcome.result.model_turns == len(requests) == 5 and not requests[-1].get("tools")
    assert not (tmp_path / "wrong.txt").exists() and outcome.result.tool_calls == 3
    skipped = next(event.payload["result"] for event in history.events if event.type == "tool_result" and event.payload["call_id"] == "late-write")
    assert skipped["error"]["code"] == "handoff_tools_disabled"


@pytest.mark.parametrize("bad_report", ["not json", report(ids=["invented-call"]), report(summary="x" * 1201), report(complete=False, pending=[])])
def test_invalid_or_forged_reports_are_failed_handoffs(tmp_path: Path, bad_report: str):
    """普通文本、伪造引用和超长正文不当作成功交接，原输出保留且部分证据可读。"""
    value = setup_run(tmp_path)
    outcome, requests, _ = run_script(value, tmp_path / "records", [response(delegate()),
        response(call("read", arguments='{"path":"api.py"}')), response(content=bad_report), response(content="交接失败。")])
    record = outcome.result.subagents[0]
    assert record.status == "failed" and record.report.partial and record.report.evidence[0].path == "api.py"
    assert json.loads(requests[-1]["messages"][-1]["content"])["ok"] is False
    assert read_child(tmp_path / "records", value.run_id, record.run_id).result.final_answer == bad_report


@pytest.mark.parametrize("parent_timeout", [False, True])
def test_parent_stop_or_timeout_seals_partial_child(tmp_path: Path, parent_timeout: bool):
    """父停止与总超时都退出真实子执行并封存部分报告，不留下后台工作。"""
    async def scenario():
        """用门闩确认子模型进入等待后取消或等总超时，返回父结果和重读子日志。"""
        value = setup_run(tmp_path, run_timeout_seconds=1.5 if parent_timeout else 10.0)
        model = ScriptModel([reply(tool_call("delegate_readonly", title="等待", objective="读取接口"))], gated=True)
        running = asyncio.create_task(run_task(value, tmp_path / "records", "offline", model_client=model))
        await asyncio.wait_for(model.entered.wait(), 5)
        if not parent_timeout:
            running.cancel()
        outcome = await asyncio.wait_for(running, 5)
        return outcome, read_child(tmp_path / "records", value.run_id, outcome.result.subagents[0].run_id)
    outcome, child = asyncio.run(scenario())
    assert outcome.result.execution_status == ("timed_out" if parent_timeout else "cancelled")
    assert child.result.execution_status == ("timed_out" if parent_timeout else "cancelled") and child.result.subagent_report.partial


def test_report_utf8_pagination_and_foreign_child_rejection(tmp_path: Path):
    """结构报告UTF-8分页不丢字段，跨父归属及字符内部偏移明确拒绝。"""
    large = report("中文结论" * 200, findings=[{"category": "optional", "title": "建议", "detail": "中文说明" * 120, "evidence_call_ids": []}] * 8)
    value = setup_run(tmp_path)
    outcome, _, _ = run_script(value, tmp_path / "records", [response(delegate()), response(content=large), response(content="结束。")])
    child = read_child(tmp_path / "records", value.run_id, outcome.result.subagents[0].run_id)
    parts, offset = [], 0
    while True:
        page = report_page(child, offset, 8192)
        parts.append(page["content"])
        if page["eof"]:
            break
        offset = page["next_offset"]
    assert len(parts) > 1 and json.loads("".join(parts)) == child.result.subagent_report.model_dump(mode="json")
    raw = "".join(parts).encode("utf-8")
    middle = next(index for index, byte in enumerate(raw) if byte & 0xC0 == 0x80)
    with pytest.raises(SubagentError):
        report_page(child, middle, 8192)
    with pytest.raises(StorageError):
        read_child(tmp_path / "records", uuid4(), child.manifest.run_id)


@pytest.mark.parametrize("selected", [[], ["alpha"]])
def test_profiles_only_use_explicit_frozen_skills(tmp_path: Path, selected: list[str]):
    """子不继承父角色和未选技能，明确选择的技能复制冻结正文，不重新加载。"""
    value = setup_run(tmp_path)
    value.config.system_prompt = "PARENT_ROLE_MUST_WRITE 父级写入职责"
    skills = tmp_path / "skills"
    for name in ("alpha", "beta"):
        directory = skills / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: 测试技能\n---\n{name.upper()}_FROZEN", encoding="utf-8")
    value.config.skill_roots, value.config.skills = [skills], ["alpha", "beta"]
    captured = []
    replies = [response(delegate(role="review", skill_names=selected)), response(content=report()), response(content="结束。")]

    def handler(request: httpx.Request):
        """记录request，父首请求后改技能源文件，返回固定HTTP响应，不访问网络。"""
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            (skills / "alpha" / "SKILL.md").write_text("CHANGED_SOURCE", encoding="utf-8")
        return model_stream_response(200, json=replies[len(captured) - 1])

    outcome = asyncio.run(run_task(value, tmp_path / "records", "offline", transport=httpx.MockTransport(handler)))
    child_input = json.dumps(captured[1]["messages"], ensure_ascii=False)
    assert "审阅职责" in child_input and "PARENT_ROLE_MUST_WRITE" not in child_input
    assert "BETA_FROZEN" not in child_input and "CHANGED_SOURCE" not in child_input
    assert ("ALPHA_FROZEN" in child_input) is bool(selected)
    assert outcome.result.subagents[0].skill_names == selected


def test_unselected_skill_cannot_be_loaded(tmp_path: Path):
    """未在父运行冻结的技能不能通过委派名称加载，不创建子执行。"""
    outcome, requests, _ = run_script(setup_run(tmp_path), tmp_path / "records", [response(delegate(skill_names=["unknown"])), response(content="技能不可用。")])
    assert not outcome.result.subagents
    assert json.loads(requests[-1]["messages"][-1]["content"])["error"]["code"] == "subagent_skill_unavailable"


def test_fixed_rules_memory_and_actual_phase_are_inherited(tmp_path: Path):
    """规划子研究继承父固定规则、记忆修订与实际阶段，不重新加载并发修改。"""
    from agent_harness.memory.service import MemoryChange, MemoryContent, MemoryService, MemoryStore
    from tests.helpers.memory import fact_content
    value = setup_run(tmp_path)
    value.planning.phase = "planning"
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("FIRST_INSTRUCTIONS", encoding="utf-8")
    root = tmp_path / "records"
    service = MemoryService(MemoryStore(root), tmp_path)
    item = service.create(fact_content("api.py接口", "FIRST_MEMORY"), str(uuid4()), provenance={"kind": "manual_confirmation"})
    for phase, marker in (("planning", "PLANNING_ONLY_RULE"), ("execution", "EXECUTION_ONLY_RULE")):
        service.create(MemoryContent(title=marker, type="rule", details={"topic": marker, "behavior": marker}, applies_to={"phases": [phase]}), str(uuid4()), provenance={"kind": "manual_confirmation"})
    captured = []
    replies = [response(delegate()), response(call("memory-read", "memory_get", json.dumps({"memory_id": item["id"]}))), response(content=report()), response(content="结束规划。")]

    def handler(request: httpx.Request):
        """记录request，父请求后修改约定和记忆，返回预置响应，不访问网络。"""
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            instructions.write_text("SECOND_INSTRUCTIONS", encoding="utf-8")
            service.update(item["id"], MemoryChange(request_id=uuid4(), expected_revision=item["revision"], action="edit", content=fact_content("api.py接口", "SECOND_MEMORY")))
        return model_stream_response(200, json=replies[len(captured) - 1])

    outcome = asyncio.run(run_task(value, root, "offline", transport=httpx.MockTransport(handler)))
    child_input = json.dumps(captured[1]["messages"], ensure_ascii=False)
    assert "FIRST_INSTRUCTIONS" in child_input and "SECOND_INSTRUCTIONS" not in child_input
    assert "PLANNING_ONLY_RULE" in child_input and "EXECUTION_ONLY_RULE" not in child_input
    returned = json.loads(captured[2]["messages"][-1]["content"])
    assert "FIRST_MEMORY" in json.dumps(returned) and "SECOND_MEMORY" not in json.dumps(returned)
    assert not outcome.result.subagents[0].report.partial


def test_initialization_failure_is_sealed(tmp_path: Path, monkeypatch):
    """子执行器创建失败也封存失败报告和终态，不保留假活动子项。"""
    from agent_harness.tools.registry import ToolExecutor
    original = ToolExecutor.__init__

    def initialize(executor, *arguments, **options):
        """arguments/options正常初始化父，只对readonly子初始化注入异常，返回None。"""
        if options.get("readonly") is not None:
            raise ValueError("offline initialization failure")
        original(executor, *arguments, **options)

    monkeypatch.setattr(ToolExecutor, "__init__", initialize)
    outcome, _, _ = run_script(setup_run(tmp_path), tmp_path / "records", [response(delegate()), response(content="研究失败。")])
    record = outcome.result.subagents[0]
    assert record.status == "failed" and record.stop_reason == "subagent_internal_error" and record.report.partial
