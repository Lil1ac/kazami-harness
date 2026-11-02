"""验证流式观察器到持久化事件及真实工具分发的完整链路。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx

from agent_harness.runtime.runner import run_task
from agent_harness.schemas import RunConfig, RunManifest, TaskSpec
from agent_harness.storage.runs import RunStore
from tests.helpers.streaming import model_stream_response


def test_streaming_text_precedes_tool_execution_and_final_answer(tmp_path: Path) -> None:
    """在 tmp_path 执行真实读文件；文字先落盘，完整调用再执行，最终正文单独固定。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "demo.txt").write_text("真实内容", encoding="utf-8")
    manifest = RunManifest(run_id=uuid4(), task=TaskSpec(prompt="读取文件", workspace=workspace), config=RunConfig(tools=["read_file"]))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """首轮给出进度和读取调用，次轮基于真实回执生成最终回答。"""
        requests.append(request)
        if len(requests) == 1:
            message = {"role": "assistant", "content": "先读取目标文件。", "tool_calls": [{"id": "read-a", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"demo.txt"}'}}]}
            reason = "tool_calls"
        else:
            assert any(item["role"] == "tool" for item in json.loads(request.content)["messages"])
            message = {"role": "assistant", "content": "文件内容为真实内容。"}
            reason = "stop"
        return model_stream_response(200, json={"model": "offline", "choices": [{"message": message, "finish_reason": reason}]})

    outcome = asyncio.run(run_task(manifest, tmp_path / "runs", "offline-key", transport=httpx.MockTransport(handler)))
    history = RunStore.read(tmp_path / "runs", str(manifest.run_id))
    types = [event.type for event in history.events]
    assert outcome.result.final_answer == "文件内容为真实内容。"
    assert types.index("model_delta") < types.index("assistant_message") < types.index("tool_started") < types.index("tool_result")
    assert [event.payload["model_turn"] for event in history.events if event.type == "model_delta" and event.payload["channel"] == "text"] == [1, 2]
