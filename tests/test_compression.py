"""验证分层压缩的真实工具链、原始证据、预算和失败边界。"""

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from agent_harness.compression import message_groups
from agent_harness.context_archive import ContextArchive, ReadContextArguments
from agent_harness.runner import CancellationState, run_task
from agent_harness.schemas import ContextPolicy, RunConfig, RunLimits, RunManifest, RunResult, TaskSpec
from agent_harness.sessions import history_messages
from agent_harness.store import RunStore
from agent_harness.workspace import WorkspaceError


def _manifest(tmp_path: Path, **context: object) -> RunManifest:
    """创建隔离目录及配置；context逐字段覆盖策略，返回清单，不调用模型。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "small.txt").write_text("实际文件内容\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("必须保留 RULE_COMPRESSION。\n", encoding="utf-8")
    return RunManifest(
        run_id=uuid4(), task=TaskSpec(prompt="保留兼容接口 USER_CONSTRAINT，完成小修改。", workspace=workspace),
        config=RunConfig(
            tools=["read_file"], limits=RunLimits(max_model_turns=15),
            context=ContextPolicy(compression="hybrid", **context),
        ),
    )


def _response(content: str, call: dict | None = None) -> dict:
    """构造真实适配器可解析的响应JSON；call非空时为工具请求，附固定可核对usage。"""
    message = {"role": "assistant", "content": content}
    if call is not None:
        message["tool_calls"] = [call]
    return {"model": "offline-compression", "choices": [{
        "finish_reason": "tool_calls" if call else "stop", "message": message,
    }], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


def _call(identifier: str, name: str, arguments: dict) -> dict:
    """创建单个工具调用；参数以JSON原文交给真实工具校验与执行。"""
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _summary(goal: str = "已读取并检查文件") -> str:
    """生成有界工作摘要JSON；goal只用于构造语义无效/过长等失败变体。"""
    return json.dumps({
        "goal": goal, "decisions": ["保留兼容接口"], "changes": [],
        "evidence": ["之前读取了small.txt，测试状态未知"],
        "open_issues": [], "next_steps": ["继续当前用户要求"],
    }, ensure_ascii=False)


class CompressionServer:
    """通过MockTransport驱动真实适配器，分别记录摘要和执行请求。"""

    def __init__(self, *, summary: str | None = None, observations: int = 5) -> None:
        """summary为None时返回标准摘要；observations为正常工具轮数，不启动网络。"""
        self.summary = _summary() if summary is None else summary  # 摘要响应原文。
        self.observations = observations  # 模型先执行的真实读取次数。
        self.execution: list[dict] = []  # 实际执行请求快照。
        self.summaries: list[dict] = []  # 实际无工具摘要请求快照。
        self.summary_started = asyncio.Event()  # 取消测试确认真实进入摘要等待。
        self.block_summary = False  # 为True时等待取消。

    async def handle(self, request: httpx.Request) -> httpx.Response:
        """记录实际HTTP体并返回脚本化响应；只模拟模型，Runner/文件工具/落盘均用真实实现。"""
        payload = json.loads(request.content)
        if not payload.get("tools"):
            self.summaries.append(deepcopy(payload))
            self.summary_started.set()
            if self.block_summary:
                await asyncio.Event().wait()
            return httpx.Response(200, json=_response(self.summary))
        self.execution.append(deepcopy(payload))
        index = len(self.execution)
        if index <= self.observations:
            return httpx.Response(200, json=_response(
                "已分析当前文件，保留兼容性。" + ("历史分析细节abcd；" * 95),
                _call("read-" + str(index), "read_file", {"path": "small.txt"}),
            ))
        return httpx.Response(200, json=_response("完成 USER_CONSTRAINT，保留 RULE_COMPRESSION。"))


def test_archive_utf8_pagination_integrity_and_foreign_scope(tmp_path: Path) -> None:
    """分页读取保留UTF-8边界，跨运行引用及篡改对象均失败，不开放任意记录路径。"""
    manifest = _manifest(tmp_path)
    store = RunStore.create(tmp_path / "runs", manifest)
    archive = ContextArchive(store.run_dir, manifest)
    text = "中文🙂边界\n" * 50
    reference = archive.save(text)
    assert reference
    chunks, offset = [], 0
    while True:
        page = archive.read(ReadContextArguments(reference=reference, offset=offset, max_bytes=64))
        chunks.append(page["content"])
        assert page["next_offset"] >= offset and not page["decoding_replaced"]
        offset = page["next_offset"]
        if page["eof"]:
            break
    assert "".join(chunks) == text
    assert offset == len(text.encode("utf-8"))
    foreign = reference.replace(str(manifest.run_id), str(uuid4()))
    with pytest.raises(WorkspaceError, match="可信前轮"):
        archive.read(ReadContextArguments(reference=foreign))
    (store.run_dir / "context" / (reference.rsplit(":", 1)[1] + ".txt")).write_text("篡改", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="损坏"):
        archive.read(ReadContextArguments(reference=reference))


def test_archive_ancestor_and_other_session(tmp_path: Path) -> None:
    """子轮可回读已完成祖先，不能因猜到编号访问另一个会话。"""
    parent = _manifest(tmp_path)
    root = tmp_path / "runs"
    parent_store = RunStore.create(root, parent)
    parent_archive = ContextArchive(parent_store.run_dir, parent)
    reference = parent_archive.save("前轮的真实历史证据")
    parent_store.finalize(RunResult(run_id=parent.run_id, execution_status="completed"))
    child = parent.model_copy(update={"run_id": uuid4(), "session_id": parent.run_id, "parent_run_id": parent.run_id, "turn_index": 2})
    child_store = RunStore.create(root, child)
    archive = ContextArchive(child_store.run_dir, child)
    assert archive.read(ReadContextArguments(reference=reference))["content"] == "前轮的真实历史证据"
    other = parent.model_copy(update={"run_id": uuid4()})
    other_store = RunStore.create(root, other)
    other_reference = ContextArchive(other_store.run_dir, other).save("其他会话")
    with pytest.raises(WorkspaceError):
        archive.read(ReadContextArguments(reference=other_reference))


def test_groups_protect_failures_and_reject_incomplete_pair() -> None:
    """完整批次保持配对，错误批次受保护，残缺或重复调用不能进入摘要。"""
    call = _call("one", "read_file", {"path": "a"})
    messages = [
        {"role": "user", "content": "原始要求"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "one", "content": '{"ok":false,"error":{"code":"missing"}}'},
    ]
    units = message_groups(messages)
    assert [(item.start, item.end, item.protected) for item in units] == [(0, 1, True), (1, 3, True)]
    with pytest.raises(ValueError):
        message_groups(messages[:-1])


def test_offload_readback_uses_real_tool_and_keeps_raw_history(tmp_path: Path) -> None:
    """大结果外置后模型通过真实read_context取回中间标记，事件仍保存原始结果。"""
    manifest = _manifest(tmp_path, inline_tool_bytes=1024)
    payload = "HEAD\n" + "abcd" * 1500 + "\nMIDDLE_EVIDENCE\n" + "xyz" * 500 + "\nTAIL"
    (manifest.task.workspace / "large.txt").write_bytes(payload.encode("utf-8"))
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        """按真实请求中的引用驱动回读，验证标记来自工具返回，非静态最终回答。"""
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, json=_response("", _call("large", "read_file", {"path": "large.txt", "max_bytes": 12000})))
        tool_result = json.loads(body["messages"][-1]["content"])
        if len(requests) == 2:
            assert "MIDDLE_EVIDENCE" not in tool_result["data"]["preview"]
            reference = tool_result["data"]["context_reference"]
            return httpx.Response(200, json=_response("", _call("recover", "read_context", {"reference": reference, "max_bytes": 16384})))
        recovered = json.loads(tool_result["data"]["content"])
        assert recovered["data"]["content"] == payload
        return httpx.Response(200, json=_response("从原始证据读到 MIDDLE_EVIDENCE。"))

    outcome = asyncio.run(run_task(manifest, tmp_path / "runs", "offline-key", transport=httpx.MockTransport(handle)))
    assert outcome.result.execution_status == "completed", outcome.result.model_dump()
    assert outcome.result.model_turns == outcome.result.http_attempts == 3
    assert outcome.result.tool_calls == 2
    history = RunStore.read(tmp_path / "runs", str(manifest.run_id))
    actions = [event for event in history.events if event.type == "context_action"]
    assert len(actions) == 1 and actions[0].payload["kind"] == "offload"
    raw = history_messages(history)
    assert json.loads(raw[2]["content"])["data"]["content"] == payload
    assert all(item.get("function", {}).get("name") != "read_context" for item in requests[0].get("tool_calls", []))


@pytest.mark.parametrize("invalid", [False, True])
def test_hybrid_summary_is_budgeted_and_invalid_output_is_not_committed(tmp_path: Path, invalid: bool) -> None:
    """真实适配器摘要计入轮次/HTTP/token；无效JSON停止且不提交摘要投影。"""
    manifest = _manifest(tmp_path, max_input_bytes=15000, inline_tool_bytes=20000, keep_recent_groups=1)
    server = CompressionServer(summary="无效的自然语言摘要" if invalid else None)
    outcome = asyncio.run(run_task(manifest, tmp_path / "runs", "offline-key", transport=httpx.MockTransport(server.handle)))
    history = RunStore.read(tmp_path / "runs", str(manifest.run_id))
    assert server.summaries, [(event.type, event.payload) for event in history.events if event.type == "context_checked"]
    assert outcome.result.model_turns == outcome.result.http_attempts == len(server.execution) + len(server.summaries)
    assert outcome.result.usage.total_tokens == outcome.result.model_turns * 120
    if invalid:
        assert outcome.result.stop_reason == "context_summary_invalid"
        assert not any(event.type == "context_action" and event.payload["kind"] == "summary" for event in history.events)
        assert any(event.type == "context_action" and event.payload["kind"] == "summary_failed" for event in history.events)
    else:
        assert outcome.result.execution_status == "completed", outcome.result.model_dump()
        assert any(event.type == "context_action" and event.payload["kind"] == "summary" for event in history.events)
        latest = server.execution[-1]["messages"]
        assert any("历史工作摘要" in str(message.get("content")) for message in latest)
        assert any(message.get("role") == "user" and message.get("content") == manifest.task.prompt for message in latest)
        assert "RULE_COMPRESSION" in json.dumps(latest, ensure_ascii=False)
        assert len(history_messages(history)) == 1 + server.observations * 2 + 1
        for request in server.summaries:
            assert not request.get("tools") and len(json.dumps({"messages": request["messages"], "tools": []}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= manifest.config.context.max_input_bytes


def test_summary_cancel_has_no_committed_replacement(tmp_path: Path) -> None:
    """摘要等待中网页取消能收尾，记录取消来源且不采用未完成摘要。"""
    async def scenario() -> None:
        """等待确切摘要请求再取消真实Runner，断言落盘终态和替换记录。"""
        manifest = _manifest(tmp_path, max_input_bytes=15000, inline_tool_bytes=20000, keep_recent_groups=1)
        server = CompressionServer()
        server.block_summary = True
        state = CancellationState()
        task = asyncio.create_task(run_task(manifest, tmp_path / "runs", "offline-key", transport=httpx.MockTransport(server.handle), cancellation_state=state))
        await asyncio.wait_for(server.summary_started.wait(), 10)
        state.api_requested = True
        task.cancel()
        outcome = await task
        assert outcome.result.execution_status == "cancelled"
        history = RunStore.read(tmp_path / "runs", str(manifest.run_id))
        assert any(event.type == "cancellation_received" and event.payload["source"] == "api_user" for event in history.events)
        assert not any(event.type == "context_action" and event.payload["kind"] == "summary" for event in history.events)
        assert history_messages(history)

    asyncio.run(scenario())


def test_token_budget_and_config_validation(tmp_path: Path) -> None:
    """声明窗口时扣除输出预留；长用户原文超限在任何HTTP请求前停止，不静默截断。"""
    manifest = _manifest(tmp_path, model_context_tokens=7000, token_safety_margin=1000)
    manifest.task.prompt = "必须保留原始要求" * 1500
    server = CompressionServer(observations=0)
    outcome = asyncio.run(run_task(manifest, tmp_path / "runs", "offline-key", transport=httpx.MockTransport(server.handle)))
    assert outcome.result.stop_reason == "max_model_input_tokens"
    assert outcome.result.model_turns == outcome.result.http_attempts == 0
    assert not server.execution and not server.summaries
    with pytest.raises(ValidationError):
        ContextPolicy(trigger_ratio=0.5, target_ratio=0.6)
    with pytest.raises(ValidationError):
        RunConfig(context=ContextPolicy(model_context_tokens=1000))


def test_masking_preserves_failed_result_and_recent_batch(tmp_path: Path) -> None:
    """仅规则策略可遮蔽旧成功结果；旧失败和近期证据保持原文，模型不用额外摘要调用。"""
    manifest = _manifest(tmp_path, max_input_bytes=12500, inline_tool_bytes=30000, keep_recent_groups=1)
    manifest.config.context.compression = "masking"
    text = "success observation " * 150
    (manifest.task.workspace / "small.txt").write_bytes(text.encode("utf-8"))
    captured = []

    def handle(request: httpx.Request) -> httpx.Response:
        """先产生一次真实缺失错误，再产生成功结果直到触发旧输出遮蔽。"""
        body = json.loads(request.content)
        captured.append(body)
        index = len(captured)
        if index <= 6:
            return httpx.Response(200, json=_response(
                "读取证据", _call("mask-" + str(index), "read_file", {"path": "missing.txt" if index == 1 else "small.txt"}),
            ))
        return httpx.Response(200, json=_response("本轮检查结束"))

    outcome = asyncio.run(run_task(manifest, tmp_path / "runs", "offline-key", transport=httpx.MockTransport(handle)))
    assert outcome.result.execution_status == "completed", outcome.result.model_dump()
    history = RunStore.read(tmp_path / "runs", str(manifest.run_id))
    assert any(event.type == "context_action" and event.payload["kind"] == "mask" for event in history.events)
    assert not any(event.type == "context_compaction_started" for event in history.events)
    raw = history_messages(history)
    failed = next(message for message in raw if message.get("role") == "tool")
    assert failed in captured[-1]["messages"]
    last_result = next(message for message in reversed(captured[-1]["messages"]) if message.get("role") == "tool")
    assert json.loads(last_result["content"])["data"]["content"] == text


def test_command_captured_stdout_is_recoverable_beyond_preview(tmp_path: Path) -> None:
    """外置命令输出可回读实际捕获日志，预览截断标记不等于完整进程输出丢失。"""
    manifest = _manifest(tmp_path, inline_tool_bytes=512)
    manifest.config.tools = ["run_command"]
    manifest.config.limits.tool_output_preview_chars = 800
    captured = []

    def handle(request: httpx.Request) -> httpx.Response:
        """只通过工具提供的捕获流引用找回预览之外的标记，不读取宿主日志路径。"""
        body = json.loads(request.content)
        captured.append(body)
        if len(captured) == 1:
            return httpx.Response(200, json=_response("", _call("command", "run_command", {
                "argv": ["python", "-c", "print('HEAD'+'x'*4000+'CAPTURE_MIDDLE'+'y'*4000+'TAIL')"],
            })))
        result = json.loads(body["messages"][-1]["content"])
        if len(captured) == 2:
            assert result["data"]["source_truncated"]
            reference = result["data"]["captured_streams"]["stdout"]
            return httpx.Response(200, json=_response("", _call("capture", "read_context", {"reference": reference, "max_bytes": 16384})))
        assert result["data"]["eof"] and "CAPTURE_MIDDLE" in result["data"]["content"]
        return httpx.Response(200, json=_response("已读取完整捕获流"))

    outcome = asyncio.run(run_task(manifest, tmp_path / "runs", "offline-key", transport=httpx.MockTransport(handle)))
    assert outcome.result.execution_status == "completed", outcome.result.model_dump()
    assert outcome.result.tool_calls == 2 and outcome.result.cleanup_status in {"complete", "not_needed"}


@pytest.mark.parametrize("mode", ["off", "masking", "hybrid"])
def test_summary_budget_zero_never_starts_summary(tmp_path: Path, mode: str) -> None:
    """摘要上限为零时不调用整理模型；各策略仍遵守总轮次与输入硬限制。"""
    manifest = _manifest(tmp_path, max_input_bytes=15000, max_summary_calls=0, keep_recent_groups=1)
    manifest.config.context.compression = mode
    manifest.config.limits.max_model_turns = 4
    server = CompressionServer()
    outcome = asyncio.run(run_task(manifest, tmp_path / "runs", "offline-key", transport=httpx.MockTransport(server.handle)))
    assert not server.summaries
    assert outcome.result.model_turns <= 4
    assert outcome.result.stop_reason in {"max_model_input_bytes", "max_model_turns"}


def test_nonshrinking_summary_is_rejected(tmp_path: Path) -> None:
    """格式正确但反而增加输入的摘要不能覆盖历史；保存明确失败原因。"""
    manifest = _manifest(tmp_path, max_input_bytes=15000, inline_tool_bytes=20000, keep_recent_groups=1, max_summary_bytes=14000)
    server = CompressionServer(summary=_summary("冗余" * 2000))
    outcome = asyncio.run(run_task(manifest, tmp_path / "runs", "offline-key", transport=httpx.MockTransport(server.handle)))
    assert outcome.result.stop_reason == "context_summary_invalid"
    history = RunStore.read(tmp_path / "runs", str(manifest.run_id))
    assert not any(event.type == "context_action" and event.payload["kind"] == "summary" for event in history.events)
