"""验证计划的持久事实、按需回读与模型输入投影，不依赖模型记住历史方案。"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agent_harness.compression import message_groups
from agent_harness.context import ContextBuilder
from agent_harness.planning import PlanningController, PlanningError, ReadPlanArguments, UpdatePlanArguments, project_plan_history, read_plan
from agent_harness.providers.base import ModelResponse
from agent_harness.runner import run_task
from agent_harness.schemas import DecisionRecord, PlanRevision, PlanningState, PlanStep, RunConfig, TaskSpec
from agent_harness.store import RunStore
from agent_harness.workspace import Workspace
from tests.test_compression import _manifest, _summary
from tests.web.test_planning_api import call, reply


def plan_state(workspace: Path, count: int = 1) -> PlanningState:
    """为 workspace 创建 count 个版本；最后一版已批准，其余为历史，返回独立可变状态。"""
    identity = workspace.stat()
    plans = [PlanRevision(version=index, title=f"功能 {index}", summary=f"历史版本 {index} 正文。" * 80,
                          scope=["BOUNDARY_KEEP：不能修改输入文件"], steps=[PlanStep(text="实现并检查")],
                          verification=["核对前后端及文件结果"], workspace=str(workspace),
                          workspace_identity=(identity.st_dev, identity.st_ino), status="approved" if index == count else "superseded")
             for index in range(1, count + 1)]
    return PlanningState(plans=plans, decisions=[DecisionRecord(interaction_id=uuid4(), action="approve", plan_version=index, text=f"历史决定 {index}") for index in range(1, count + 1)])


def tool_group(name: str, arguments: dict, data: dict, identifier: str = "call", *, ok: bool = True) -> list[dict]:
    """返回 name/arguments 及 data 的完整批次；identifier 可跨批次复用，ok=False 表示失败。"""
    return [{"role": "assistant", "content": None, "tool_calls": [{"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]},
            {"role": "tool", "tool_call_id": identifier, "content": json.dumps({"ok": ok, "data": data, "error": None if ok else {"code": "plan_version_conflict"}}, ensure_ascii=False)}]


def test_current_plan_projection_shrinks_history_and_refreshes_without_mutating_records(tmp_path: Path) -> None:
    """多版计划仅按当前版本装载；进度更新即时进入系统输入，历史原文和状态事件仍完整。"""
    state = plan_state(tmp_path, 12)
    events = []  # 宿主完整事件，与模型阅读回执分别验证。

    def emit(kind: str, payload: dict) -> None:
        """记录 kind/payload 的独立副本；返回 None，不写项目或执行命令。"""
        events.append((kind, deepcopy(payload)))

    controller = PlanningController(state, tmp_path, emit)
    builder = ContextBuilder(Workspace(tmp_path, tmp_path / "runs"), TaskSpec(prompt="继续执行", workspace=tmp_path), RunConfig())
    messages = builder.initial_messages()
    builder.bind_planning(controller, messages[0]["content"])
    messages[0]["content"] += controller.instructions()
    for plan in state.plans:
        messages.extend(tool_group("submit_plan", {"title": plan.title, "summary": plan.summary, "scope": plan.scope, "steps": [step.text for step in plan.steps], "verification": plan.verification}, {"plan_version": plan.version}))
    original = deepcopy(messages)
    raw_size = builder.build(messages, [], 1, apply_planning=False).check.input_bytes
    prepared = builder.build(messages, [], 1)
    text = json.dumps(prepared.messages, ensure_ascii=False)
    assert "历史版本 1 正文" not in text and "历史决定 1" not in text
    assert "历史版本 12 正文" in text and "BOUNDARY_KEEP" in text
    assert prepared.check.input_bytes < raw_size / 4
    assert messages == original and len(controller.state.plans) == 12
    acknowledgement = controller.execute("update_plan", UpdatePlanArguments(version=12, step=1, status="in_progress"))
    current = json.loads(builder.build(messages, [], 2).messages[0]["content"].rsplit("\n", 1)[-1])
    assert current["current_plan"]["steps"][0]["status"] == "in_progress"
    assert "plans" not in acknowledgement and len(events[0][1]["state"]["plans"]) == 12
    assert messages == original


def test_projection_preserves_mixed_batches_failures_explicit_reads_and_reused_ids() -> None:
    """只移除已成功的规划状态操作；混合工具、错误和主动回读保持有效配对及原内容。"""
    first = tool_group("submit_plan", {"summary": "旧方案正文"}, {"plan_version": 1})
    other = tool_group("read_file", {"path": "source.txt"}, {"content": "真实输入"}, "file")
    first[0]["tool_calls"].extend(other[0]["tool_calls"])
    first[0]["content"] = "保留这段观察"
    messages = first + other[1:] + tool_group("update_plan", {"version": 2}, {}, ok=False) + tool_group("read_plan", {"version": 1}, {"content": "显式回读旧方案"})
    original = deepcopy(messages)
    projected = project_plan_history(messages)
    message_groups(projected)
    names = [item["function"]["name"] for message in projected for item in message.get("tool_calls", [])]
    assert names == ["read_file", "update_plan", "read_plan"]
    assert projected[0]["content"] == "保留这段观察"
    assert "显式回读旧方案" in json.dumps(projected, ensure_ascii=False)
    assert "plan_version_conflict" in json.dumps(projected)
    assert messages == original
    with pytest.raises(ValueError, match="incomplete"):
        project_plan_history(first[:1])


def test_read_plan_pages_utf8_versions_and_decisions_without_publishing(tmp_path: Path) -> None:
    """完整计划和决定按需分页；指纹变化、错误版本及非 UTF-8 边界受控拒绝，读取不改变事实。"""
    state = plan_state(tmp_path, 2)
    original = state.model_dump_json()
    parts = []
    offset = 0
    fingerprint = None
    while True:
        page = read_plan(state, ReadPlanArguments(version=1, offset=offset, max_bytes=128, expected_sha256=fingerprint))
        parts.append(page["content"])
        fingerprint = page["sha256"]
        if page["eof"]:
            break
        offset = page["next_offset"]
    assert json.loads("".join(parts))["summary"] == state.plans[0].summary
    assert "workspace_identity" not in json.loads("".join(parts))
    assert len(json.loads(read_plan(state, ReadPlanArguments(section="history"))["content"])) == 2
    assert json.loads(read_plan(state, ReadPlanArguments(section="decisions"))["content"])[0]["text"] == "历史决定 1"
    assert state.model_dump_json() == original
    with pytest.raises(PlanningError, match="不存在"):
        read_plan(state, ReadPlanArguments(version=3))
    with pytest.raises(PlanningError, match="第一页"):
        read_plan(state, ReadPlanArguments(version=1, expected_sha256="0" * 64))
    with pytest.raises(PlanningError, match="版本参数"):
        read_plan(state, ReadPlanArguments(section="history", version=1))
    raw = json.dumps(state.plans[0].model_dump(mode="json", exclude={"workspace_identity"}), ensure_ascii=False, separators=(",", ":")).encode()
    middle = next(index for index, value in enumerate(raw) if value & 0xC0 == 0x80)
    with pytest.raises(PlanningError, match="偏移"):
        read_plan(state, ReadPlanArguments(version=1, offset=middle))


def test_question_and_answer_remain_paired_in_context() -> None:
    """没有成文计划时仍保留问题语义，避免用户只回答“JSON”后模型无法确定回答对象。"""
    messages = tool_group("ask_user_question", {"question": "输出使用什么格式？", "options": ["JSON", "纯文本"]}, {"waiting_for": "question"})
    messages.append({"role": "user", "content": "JSON"})
    projected = project_plan_history(messages)
    assert projected == messages and "输出使用什么格式" in json.dumps(projected, ensure_ascii=False)
    message_groups(projected)


class CompressingPlanModel:
    """用固定响应触发真实混合压缩，观察每次执行输入是否仍有最新计划。"""

    def __init__(self) -> None:
        """初始化执行和摘要请求记录；构造不访问供应商或文件。"""
        self.execution: list[list[dict]] = []  # 实际执行请求，保留压缩前后输入以验证计划不丢失。
        self.summaries: list[list[dict]] = []  # 实际摘要请求，不模拟压缩算法。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """按 tools 是否为空返回摘要或下一个真实工具请求；保存 messages 独立副本，不执行工具。"""
        if not tools:
            self.summaries.append(deepcopy(messages))
            return reply(text=_summary())
        self.execution.append(deepcopy(messages))
        index = len(self.execution)
        if index > 6:
            return reply(text="完成并核对 BOUNDARY_KEEP。")
        operations = [call("read_file", path="small.txt")]
        if index in {1, 6}:
            operations.insert(0, call("update_plan", version=1, step=1, status="in_progress" if index == 1 else "completed"))
        response = reply(*operations)
        response.message["content"] = "已分析当前文件，记录观察。" + "历史分析细节abcd；" * 95
        return response


def test_hybrid_compaction_reinjects_current_approved_plan_on_every_request(tmp_path: Path) -> None:
    """真实 Runner 压缩旧历史后仍装载当前批准版和最新进度，原始事件保留完整规划操作。"""
    manifest = _manifest(tmp_path, max_input_bytes=15000, keep_recent_groups=1)
    manifest.planning = plan_state(manifest.task.workspace)
    # 本例施压对象是增长的工作历史；当前计划保持精简，避免固定前缀本身占满预算。
    manifest.planning.plans[0].summary = "核对输入并实现功能，保持 BOUNDARY_KEEP。"
    model = CompressingPlanModel()
    outcome = asyncio.run(run_task(manifest, tmp_path / "runs", "offline", model_client=model))
    history = RunStore.read(tmp_path / "runs", str(manifest.run_id))
    assert outcome.result.execution_status == "completed", outcome.result.stop_reason
    assert model.summaries and any(event.type == "context_action" and event.payload["kind"] == "summary" for event in history.events)
    for index, messages in enumerate(model.execution):
        current = json.loads(messages[0]["content"].rsplit("\n", 1)[-1])
        assert current["current_plan"]["status"] == "approved"
        assert "BOUNDARY_KEEP" in current["current_plan"]["scope"][0]
        assert current["current_plan"]["steps"][0]["status"] == ("pending" if index == 0 else "completed" if index == 6 else "in_progress")
        message_groups(messages)
    assert len([event for event in history.events if event.type == "tool_started" and event.payload["name"] == "update_plan"]) == 2
