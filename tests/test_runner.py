"""使用真实适配器与 MockTransport 验证模型—工具闭环，全程不联网。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_harness.config import ConfigurationError
from agent_harness.schemas import RunConfig, RunLimits, RunManifest, TaskSpec
from agent_harness.runner import exit_code, run_task
from agent_harness.store import RunStore, StorageError


SECRET = "dummy-key-never-persist"


def manifest(workspace: Path, **limits) -> RunManifest:
    """创建带固定文件分析任务的运行清单，并允许覆盖预算以触发控制边界。"""
    return RunManifest(
        run_id=uuid4(), task=TaskSpec(prompt="读取 app.log，统计 ERROR 类型。", workspace=workspace),
        config=RunConfig(limits=RunLimits(**limits)),
    )


def call(identifier: str, name: str = "read_file", arguments: str = '{"path":"app.log"}') -> dict:
    """构造供应商格式的工具调用，便于组合正常、非法或未知工具批次。"""
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def response(*calls: dict, content: str | None = None, usage: bool = True) -> dict:
    """构造工具调用或最终文本响应，并可选择省略用量字段。"""
    message = {"role": "assistant", "content": content, "reasoning_content": ""}
    if calls:
        message["tool_calls"] = list(calls)
    data = {
        "id": "response-test", "model": "deepseek-flash-test", "system_fingerprint": "fp-test",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
    }
    if usage:
        data["usage"] = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    return data


def run_script(value: RunManifest, root: Path, replies: list[dict | int]):
    """按预置响应序列运行真实模型适配器，返回运行结果、捕获请求和历史记录。"""
    requests = []

    def handler(request: httpx.Request):
        """依次消费预置响应，整数模拟 HTTP 错误码，字典模拟成功响应。"""
        requests.append(json.loads(request.content))
        item = replies[len(requests) - 1]
        return httpx.Response(item, json={"error": SECRET}) if isinstance(item, int) else httpx.Response(200, json=item)

    outcome = asyncio.run(run_task(value, root, SECRET, transport=httpx.MockTransport(handler)))
    return outcome, requests, RunStore.read(root, str(value.run_id))


def test_file_read_round_trip_persists_answer_and_can_show(tmp_path: Path):
    """验证读取文件后继续回答的完整闭环、消息配对、计数、落盘和凭据保护。"""
    (tmp_path / "app.log").write_text("ERROR timeout\nERROR timeout\nERROR database\n", encoding="utf-8")
    value = manifest(tmp_path)
    outcome, requests, history = run_script(value, tmp_path / "records", [response(call("call-1")), response(content="timeout 2 次，database 1 次。")])
    assert outcome.result.execution_status == "completed"
    assert outcome.result.validation_status == "unverified"
    assert outcome.result.final_answer == "timeout 2 次，database 1 次。"
    assert exit_code(outcome.result) == 3
    assert (outcome.result.model_turns, outcome.result.http_attempts, outcome.result.tool_calls) == (2, 2, 1)
    assert outcome.result.usage.total_tokens == 16
    assert outcome.result.usage_complete is True
    assert requests[1]["messages"][-2]["tool_calls"][0]["id"] == "call-1"
    result_message = requests[1]["messages"][-1]
    assert result_message["tool_call_id"] == "call-1"
    assert "ERROR database" in json.loads(result_message["content"])["data"]["content"]
    assert history.result == outcome.result
    assert history.interrupted is False
    assert outcome.event_count == len(history.events)
    assert [event.type for event in history.events].count("run_finished") == 1
    for file in outcome.run_dir.iterdir():
        assert SECRET not in file.read_text(encoding="utf-8")


def test_batch_pairs_unknown_and_invalid_calls_before_next_request(tmp_path: Path):
    """验证未知工具、非法 JSON 和文件错误都有配对结果，再发送下一轮请求。"""
    calls = [call("one", "unknown"), call("two", arguments="{"), call("three", arguments='{"path":"missing"}')]
    outcome, requests, history = run_script(manifest(tmp_path), tmp_path / "records", [response(*calls), response(content="未能读取文件。")])
    tools = requests[1]["messages"][-3:]
    assert [item["tool_call_id"] for item in tools] == ["one", "two", "three"]
    assert [json.loads(item["content"])["error"]["code"] for item in tools] == ["unknown_tool", "invalid_arguments", "file_not_found"]
    assert outcome.result.tool_calls == 3
    assert len([event for event in history.events if event.type == "tool_result"]) == 3


def test_entire_batch_is_skipped_if_tool_budget_insufficient(tmp_path: Path):
    """验证剩余额度不足时整批不执行，为每个调用记录跳过原因并终止。"""
    outcome, requests, history = run_script(manifest(tmp_path, max_tool_calls=1), tmp_path / "records", [response(call("one"), call("two"))])
    assert outcome.result.execution_status == "limit_exceeded"
    assert outcome.result.stop_reason == "max_tool_calls"
    assert outcome.result.tool_calls == 0
    assert len(requests) == 1
    assert not any(event.type == "tool_started" for event in history.events)
    skipped = [event.payload["result"]["error"]["code"] for event in history.events if event.type == "tool_result"]
    assert skipped == ["skipped_limit", "skipped_limit"]


def test_model_turn_limit_keeps_last_real_tool_result(tmp_path: Path):
    """验证模型轮次耗尽后保留已经执行的工具结果，且不额外发请求。"""
    (tmp_path / "app.log").write_text("ERROR one", encoding="utf-8")
    outcome, requests, history = run_script(manifest(tmp_path, max_model_turns=1), tmp_path / "records", [response(call("one"))])
    assert outcome.result.stop_reason == "max_model_turns"
    assert outcome.result.execution_status == "limit_exceeded"
    assert outcome.result.tool_calls == 1
    assert len(requests) == 1
    assert next(event for event in history.events if event.type == "tool_result").payload["result"]["ok"] is True


def test_http_retry_does_not_repeat_executed_tool(tmp_path: Path):
    """验证 HTTP 重试仅重发模型请求，不重复工具动作，并将用量标为不完整。"""
    (tmp_path / "app.log").write_text("ERROR one", encoding="utf-8")
    outcome, requests, history = run_script(manifest(tmp_path), tmp_path / "records", [response(call("one")), 503, response(content="one 1 次。")])
    assert (outcome.result.model_turns, outcome.result.http_attempts, outcome.result.tool_calls) == (2, 3, 1)
    assert requests[1] == requests[2]
    assert outcome.result.usage_complete is False
    assert len([event for event in history.events if event.type == "tool_started"]) == 1


def test_missing_usage_is_explicitly_incomplete(tmp_path: Path):
    """验证正常回答缺少用量时仍可完成，但必须明确用量不完整。"""
    outcome, _, _ = run_script(manifest(tmp_path), tmp_path / "records", [response(content="回答", usage=False)])
    assert outcome.result.execution_status == "completed"
    assert outcome.result.usage_complete is False
    assert outcome.result.usage.total_tokens == 0


@pytest.mark.parametrize("kind", ["run", "model"])
def test_request_timeout_persists_exactly_one_terminal_event(tmp_path: Path, kind: str):
    """验证总运行和单轮模型超时都保存唯一终态，并返回超时退出码。"""
    value = manifest(tmp_path, run_timeout_seconds=0.02 if kind == "run" else 3.0, model_turn_timeout_seconds=0.02 if kind == "model" else 3.0)

    async def handler(request):
        """延迟响应超过配置预算，供总超时与单轮超时用例复用。"""
        await asyncio.sleep(5)
        return httpx.Response(200, json=response(content="不应到达"))

    outcome = asyncio.run(run_task(value, tmp_path / "records", SECRET, transport=httpx.MockTransport(handler)))
    assert outcome.result.execution_status == "timed_out"
    assert outcome.result.stop_reason == ("run_timeout" if kind == "run" else "model_timeout")
    assert exit_code(outcome.result) == 124
    history = RunStore.read(tmp_path / "records", str(value.run_id))
    assert [event.type for event in history.events].count("run_finished") == 1
    assert history.interrupted is False


def test_cancellation_during_http_is_saved_and_not_retried(tmp_path: Path):
    """验证网络等待中取消会保存取消结果，且不会发起重试。"""
    value = manifest(tmp_path)
    attempts = []

    async def scenario():
        """等请求已进入处理器后取消运行任务，使取消时机确定且可重复。"""
        entered = asyncio.Event()

        async def handler(request):
            """记录请求并通知已进入等待，再阻塞到外层任务取消。"""
            attempts.append(request)
            entered.set()
            await asyncio.sleep(5)
            return httpx.Response(200, json=response(content="不应到达"))

        running = asyncio.create_task(run_task(value, tmp_path / "records", SECRET, transport=httpx.MockTransport(handler)))
        await entered.wait()
        running.cancel()
        return await running

    outcome = asyncio.run(scenario())
    assert outcome.result.execution_status == "cancelled"
    assert exit_code(outcome.result) == 130
    assert len(attempts) == 1
    assert RunStore.read(tmp_path / "records", str(value.run_id)).result == outcome.result


def test_storage_failure_stops_before_file_execution(tmp_path: Path, monkeypatch):
    """验证工具开始事件落盘失败后立即停止，不执行文件操作或伪造终态。"""
    value = manifest(tmp_path)
    original = RunStore.append

    def failing_append(self, event_type, payload=None):
        """仅在写 tool_started 事件时模拟磁盘故障，其他事件沿用真实存储实现。"""
        if event_type == "tool_started":
            raise StorageError("模拟磁盘故障")
        return original(self, event_type, payload)

    monkeypatch.setattr(RunStore, "append", failing_append)
    with pytest.raises(StorageError):
        run_script(value, tmp_path / "records", [response(call("one"))])
    history = RunStore.read(tmp_path / "records", str(value.run_id))
    assert history.interrupted is True
    assert history.result is None
    assert not any(event.type == "tool_result" for event in history.events)


def test_duplicate_provider_call_id_never_executes_file(tmp_path: Path):
    """验证供应商返回重复调用 ID 时协议失败，任何文件工具都不会启动。"""
    outcome, _, history = run_script(manifest(tmp_path), tmp_path / "records", [response(call("same"), call("same"))])
    assert outcome.result.execution_status == "failed"
    assert not any(event.type == "tool_started" for event in history.events)


@pytest.mark.parametrize("key", ["", "dummy\nsecret", "中文密钥"])
def test_bad_credentials_fail_before_creating_records(tmp_path: Path, key: str):
    """验证缺失或非法密钥在创建运行目录之前被拒绝。"""
    with pytest.raises(ConfigurationError):
        asyncio.run(run_task(manifest(tmp_path), tmp_path / "records", key))
    assert not (tmp_path / "records").exists()


def test_cancellation_inside_tool_pairs_remaining_batch_and_stops_http(tmp_path: Path):
    """验证工具执行中取消后补齐同批结果，不启动下一工具或下一轮请求。"""
    value = manifest(tmp_path)
    requests = []

    async def scenario():
        """在首个工具进入等待后取消运行，返回终态及实际执行次数。"""
        entered = asyncio.Event()

        class WaitingExecutor:
            """可观察调用次数并主动等待的替身工具，用于精确触发执行期间取消。"""
            definitions = []
            calls = 0

            async def execute(self, name, arguments_json):
                """登记调用并发出已进入信号，随后等待外层取消，不产生文件副作用。"""
                self.calls += 1
                entered.set()
                await asyncio.sleep(5)
                return {"ok": True}

        executor = WaitingExecutor()

        def handler(request):
            """返回包含两个调用的固定批次，并记录是否误发了后续模型请求。"""
            requests.append(request)
            return httpx.Response(200, json=response(call("one"), call("two")))

        task = asyncio.create_task(run_task(value, tmp_path / "records", SECRET, transport=httpx.MockTransport(handler), executor=executor))
        await entered.wait()
        task.cancel()
        return await task, executor.calls

    outcome, executed = asyncio.run(scenario())
    history = RunStore.read(tmp_path / "records", str(value.run_id))
    assert outcome.result.execution_status == "cancelled"
    assert executed == len(requests) == 1
    results = [event.payload for event in history.events if event.type == "tool_result"]
    assert [item["call_id"] for item in results] == ["one", "two"]
    assert [item["result"]["error"]["code"] for item in results] == ["tool_cancelled", "skipped_cancelled"]
    assert [event.type for event in history.events].count("run_finished") == 1


def test_internal_tool_fault_pairs_batch_without_echoing_exception(tmp_path: Path):
    """验证工具内部异常后补齐当前批次结果，保存失败终态且不泄露异常正文。"""
    value = manifest(tmp_path)

    class BrokenExecutor:
        """每次执行都抛出内部异常的替身工具，用于验证宿主错误收尾。"""
        definitions = []

        async def execute(self, name, arguments_json):
            """抛出包含敏感标记的异常，检查日志是否只保存受控错误码。"""
            raise RuntimeError(SECRET)

    def handler(request):
        """返回两个工具调用，使测试同时覆盖当前失败和剩余调用被跳过的结果。"""
        return httpx.Response(200, json=response(call("one"), call("two")))

    outcome = asyncio.run(run_task(value, tmp_path / "records", SECRET, transport=httpx.MockTransport(handler), executor=BrokenExecutor()))
    history = RunStore.read(tmp_path / "records", str(value.run_id))
    assert outcome.result.execution_status == "failed"
    assert outcome.result.stop_reason == "tool_internal_error"
    errors = [event.payload["result"]["error"]["code"] for event in history.events if event.type == "tool_result"]
    assert errors == ["tool_internal_error", "skipped_error"]
    assert SECRET not in (outcome.run_dir / "events.jsonl").read_text(encoding="utf-8")
