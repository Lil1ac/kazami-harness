"""通过真实 Runner 和离线 HTTP 适配器验证上下文检查与执行计数的边界。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_harness.context import ContextBuilder
from agent_harness.runner import run_task
from agent_harness.schemas import ContextPolicy, RunConfig, RunManifest, TaskSpec
from agent_harness.store import RunStore
from agent_harness.tools.registry import ToolExecutor
from agent_harness.workspace import Workspace


def make_manifest(workspace: Path, *, budget: int = 256 * 1024, load: bool = True, instruction_limit: int = 32 * 1024) -> RunManifest:
    """创建固定任务清单；budget 和 instruction_limit 均按字节，load 控制读取根项目约定。"""
    return RunManifest(run_id=uuid4(), task=TaskSpec(prompt="按项目约定写出结果", workspace=workspace), config=RunConfig(tools=["read_file", "write_file"], context=ContextPolicy(max_input_bytes=budget, max_project_instruction_bytes=instruction_limit, load_project_instructions=load)))


def model_reply(*calls: dict, content: str | None = None, reasoning: str = "") -> dict:
    """构造工具批次或最终回答的供应商响应；reasoning 原样传回下一轮。"""
    message = {"role": "assistant", "content": content, "reasoning_content": reasoning}
    if calls:
        message["tool_calls"] = list(calls)
    return {"model": "offline-model", "choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}


def tool_call(identifier: str, name: str, arguments: dict) -> dict:
    """为工具 identifier 和 name 编码 arguments，返回供应商原始调用结构。"""
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def initial_size(value: RunManifest, root: Path) -> int:
    """用同一完整工具定义计算清单 value 的首轮字节数；root 为受保护记录目录。"""
    builder = ContextBuilder(Workspace(value.task.workspace, root), value.task, value.config)
    executor = ToolExecutor(value.task.workspace, root, value.config)
    return builder.build(builder.initial_messages(), executor.definitions, 1).check.input_bytes


def scripted_run(value: RunManifest, root: Path, replies: list[dict], *, rewrite_rule: str | None = None):
    """离线执行 value 并返回 outcome、实际请求及历史；rewrite_rule 非空时首次请求后修改磁盘规则。"""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        """捕获真实 HTTP 请求并逐个响应；不允许超限后多发请求。"""
        requests.append(json.loads(request.content))
        if rewrite_rule is not None and len(requests) == 1:
            (value.task.workspace / "AGENTS.md").write_text(rewrite_rule, encoding="utf-8")
        assert len(requests) <= len(replies), "发生了预算之外的请求"
        return httpx.Response(200, json=replies[len(requests) - 1])

    outcome = asyncio.run(run_task(value, root, "dummy-key", transport=httpx.MockTransport(handler)))
    return outcome, requests, RunStore.read(root, str(value.run_id))


def test_first_input_over_budget_records_check_without_model_or_tool(tmp_path: Path) -> None:
    """首次输入超限时保存检查事实，但实际模型轮次、HTTP 和工具调用全部保持零。"""
    outcome, requests, history = scripted_run(make_manifest(tmp_path, budget=1), tmp_path / ".runs", [])
    assert outcome.result.execution_status == "limit_exceeded"
    assert outcome.result.stop_reason == "max_model_input_bytes"
    assert (outcome.result.model_turns, outcome.result.http_attempts, outcome.result.tool_calls) == (0, 0, 0)
    assert requests == []
    checks = [event.payload for event in history.events if event.type == "context_checked"]
    assert len(checks) == 1 and checks[0]["model_turn"] == 1 and not checks[0]["allowed"]
    assert checks[0]["input_bytes"] > checks[0]["max_input_bytes"] == 1
    assert not any(event.type in {"model_request_started", "model_http_attempt", "tool_started"} for event in history.events)
    assert [event.type for event in history.events].count("run_finished") == 1


def test_initial_input_equal_budget_makes_one_real_request(tmp_path: Path) -> None:
    """完整初始输入等于预算时允许实际请求，统计包括完整消息和工具定义。"""
    root = tmp_path / ".runs"
    value = make_manifest(tmp_path)
    size = initial_size(value, root)
    value.config.context.max_input_bytes = size
    outcome, requests, history = scripted_run(value, root, [model_reply(content="完成")])
    check = next(event.payload for event in history.events if event.type == "context_checked")
    assert check["allowed"] and check["input_bytes"] == check["max_input_bytes"] == size
    assert outcome.result.execution_status == "completed"
    assert (outcome.result.model_turns, outcome.result.http_attempts) == (1, 1)
    assert len(requests) == 1


def test_later_over_budget_keeps_full_tool_batch_and_artifacts(tmp_path: Path) -> None:
    """后续输入超限保留已执行工具的真实结果和产物，配齐整批后停止且不增加请求计数。"""
    root = tmp_path / ".runs"
    value = make_manifest(tmp_path)
    value.config.context.max_input_bytes = initial_size(value, root) + 32
    calls = [tool_call("write", "write_file", {"path": "result.txt", "content": "真实交付产物"}), tool_call("read", "read_file", {"path": "result.txt"})]
    outcome, requests, history = scripted_run(value, root, [model_reply(*calls, reasoning="必须保留的供应商推理内容" * 20)])
    assert outcome.result.execution_status == "limit_exceeded" and outcome.result.stop_reason == "max_model_input_bytes"
    assert (outcome.result.model_turns, outcome.result.http_attempts, outcome.result.tool_calls) == (1, 1, 2)
    assert len(requests) == 1
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "真实交付产物"
    assert any(item.path == "result.txt" for item in outcome.result.artifacts)
    results = [event for event in history.events if event.type == "tool_result"]
    assert [event.payload["call_id"] for event in results] == ["write", "read"]
    assert all(event.payload["result"]["ok"] for event in results)
    checks = [event for event in history.events if event.type == "context_checked"]
    assert [event.payload["allowed"] for event in checks] == [True, False]
    assert checks[-1].payload["model_turn"] == 2
    assert checks[-1].seq > results[-1].seq
    assert [event.type for event in history.events].count("context_prepared") == 1
    assert [event.type for event in history.events].count("run_started") == 1


def test_rules_frozen_across_rounds_and_reasoning_pairs_preserved(tmp_path: Path) -> None:
    """磁盘规则变化不影响本次后续请求；原始推理字段和工具调用/结果完整回传。"""
    original = "不要修改测试；保留中文注释。"
    (tmp_path / "AGENTS.md").write_text(original, encoding="utf-8")
    (tmp_path / "data.txt").write_text("中文数据", encoding="utf-8")
    reasoning = "供应商要求回传的推理字段"
    replies = [model_reply(tool_call("one", "read_file", {"path": "data.txt"}), reasoning=reasoning), model_reply(content="已完成")]
    outcome, requests, history = scripted_run(make_manifest(tmp_path), tmp_path / ".runs", replies, rewrite_rule="运行中被修改的规则")
    assert outcome.result.execution_status == "completed" and len(requests) == 2
    assert all(original in request["messages"][1]["content"] for request in requests)
    assert all("运行中被修改的规则" not in json.dumps(request, ensure_ascii=False) for request in requests)
    assert requests[1]["messages"][-2]["reasoning_content"] == reasoning
    assert requests[1]["messages"][-2]["tool_calls"][0]["id"] == requests[1]["messages"][-1]["tool_call_id"] == "one"
    prepared = next(event.payload for event in history.events if event.type == "context_prepared")
    assert prepared["serialization"] == "json_utf8_v1" and prepared["instruction"]["content"] == original
    for event, request in zip([event for event in history.events if event.type == "context_checked"], requests, strict=True):
        encoded = json.dumps({"messages": request["messages"], "tools": request["tools"]}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        assert event.payload["input_bytes"] == len(encoded)


@pytest.mark.parametrize("kind,reason", [("encoding", "project_instructions_invalid_utf8"), ("too_large", "project_instructions_too_large"), ("directory", "project_instructions_not_regular_file")])
def test_bad_project_rules_fail_before_any_model_request(tmp_path: Path, kind: str, reason: str) -> None:
    """非法规则只产生错误快照与终态，不发模型、不执行工具或静默忽略规则。"""
    path = tmp_path / "AGENTS.md"
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"\xff" if kind == "encoding" else b"x" * 9)
    outcome, requests, history = scripted_run(make_manifest(tmp_path, instruction_limit=8), tmp_path / ".runs", [])
    assert outcome.result.execution_status == "failed" and outcome.result.stop_reason == reason
    assert (outcome.result.model_turns, outcome.result.http_attempts, outcome.result.tool_calls) == (0, 0, 0)
    assert not requests
    prepared = next(event.payload for event in history.events if event.type == "context_prepared")
    assert prepared["instruction"]["status"] == "error" and prepared["instruction"]["error_code"] == reason
    assert not any(event.type == "context_checked" for event in history.events)


def test_disabled_project_rules_allow_invalid_file_without_loading(tmp_path: Path) -> None:
    """显式关闭规则加载后，非法编码不阻断任务且事件记录 disabled。"""
    (tmp_path / "AGENTS.md").write_bytes(b"\xff")
    outcome, requests, history = scripted_run(make_manifest(tmp_path, load=False), tmp_path / ".runs", [model_reply(content="完成")])
    assert outcome.result.execution_status == "completed" and len(requests) == 1
    prepared = next(event.payload for event in history.events if event.type == "context_prepared")
    assert prepared["instruction"]["status"] == "disabled"
    assert len(requests[0]["messages"]) == 2
