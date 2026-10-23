"""从真实HTTP任务到压缩、继续、SSE及重启读取的联动回归。"""

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import ContextPolicy, RunConfig
from agent_harness.store import RunStore
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


class CompressionEditingModel:
    """脚本化模型只提出读写调用；真实Runner执行工具、压缩、快照和验收。"""

    def __init__(self, delay: float = 0) -> None:
        """delay为每次调用的非阻塞等待秒数，默认零；初始化请求证据，不访问网络。"""
        self.delay = delay  # 浏览器观察阶段用的等待秒数。
        self.requests: list[dict] = []  # 实际模型输入副本，包含额外摘要请求。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """根据实际输入阶段选择工具或JSON摘要；返回统一响应，不直接修改工作区。"""
        self.requests.append(deepcopy({"messages": messages, "tools": tools}))
        await asyncio.sleep(self.delay)
        serialized = json.dumps(messages, ensure_ascii=False)
        calls = []
        if not tools:
            if "取消摘要" in serialized:
                await asyncio.sleep(30)
            content = "无效摘要" if "错误摘要" in serialized else json.dumps({
                "goal": "修复加法并保留兼容接口",
                "decisions": ["保留用户要求 USER_CONSTRAINT"],
                "changes": [], "evidence": ["已读取项目文件，当前测试状态未知"],
                "open_issues": [], "next_steps": ["继续修改app.py，交由宿主重新测试"],
            }, ensure_ascii=False)
        else:
            users = [(index, message) for index, message in enumerate(messages)
                     if message.get("role") == "user"
                     and not message.get("content", "").startswith(("[历史工作摘要", "以下是本次运行固定"))]
            start, current = users[-1]
            prompt = current["content"]
            tail = messages[start + 1:]
            identifiers = [call["id"] for message in tail for call in message.get("tool_calls") or [] if call["id"].startswith("ce-")]
            step = max([int(identifier[3:]) for identifier in identifiers] or [0]) + 1
            if "回读" in prompt:
                if step == 1:
                    calls = [ToolCall("ce-1", "read_file", '{"path":"large.txt","max_bytes":12000}')]
                elif step == 2:
                    result = json.loads(next(message["content"] for message in reversed(tail) if message.get("role") == "tool"))
                    reference = result["data"]["context_reference"]
                    calls = [ToolCall("ce-2", "read_context", json.dumps({"reference": reference, "max_bytes": 16384}))]
                elif step == 3:
                    recovered = json.loads(next(message["content"] for message in reversed(tail) if message.get("role") == "tool"))
                    if "MIDDLE_EVIDENCE" not in recovered["data"]["content"]:
                        raise AssertionError("回读没有取回原始中间证据")
                    calls = [ToolCall("ce-3", "write_file", json.dumps({
                        "path": "app.py", "content": 'def add(a, b):\n    """返回两数之和；保留 RULE_COMPRESSION。"""\n    return a + b\n',
                        "overwrite": True,
                    }, ensure_ascii=False))]
                content = "" if calls else "已通过真实工具回读 MIDDLE_EVIDENCE，并修改 app.py。测试以宿主结果为准。"
            else:
                if step <= 5:
                    calls = [ToolCall("ce-" + str(step), "read_file", '{"path":"small.txt"}')]
                elif step == 6:
                    calls = [ToolCall("ce-6", "write_file", json.dumps({
                        "path": "app.py", "content": 'def add(a, b):\n    """返回两数之和；保留 RULE_COMPRESSION。"""\n    return a + b\n',
                        "overwrite": True,
                    }, ensure_ascii=False))]
                content = "继续核对已有证据，保留接口兼容。" + ("历史分析细节abcd；" * 95) if calls else "已完成本轮修改，保留 USER_CONSTRAINT 和 RULE_COMPRESSION。宿主测试独立展示。"
        message = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {
                "name": call.name, "arguments": call.arguments_json,
            }} for call in calls]
        return ModelResponse(
            message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop",
            response_model="offline-compression", usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        )


async def _finished(client: httpx.AsyncClient, run_id: str) -> dict:
    """等待run_id后台运行和快照收尾，十秒内返回实际视图；不发起新执行。"""
    async with asyncio.timeout(10):
        while True:
            response = await client.get("/api/runs/" + run_id)
            assert response.status_code == 200
            if not response.json()["active"]:
                return response.json()
            await asyncio.sleep(0.01)


def test_compression_followup_and_restart_keep_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """页面式提交和追加使用真实文件与持久记录，重启后只读恢复压缩证据且不重入模型。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-compression-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_bytes("保留 RULE_COMPRESSION。\n".encode("utf-8"))
    (workspace / "small.txt").write_bytes(b"source evidence\n")
    (workspace / "app.py").write_bytes(b"def add(a,b):\n    return a-b\n")
    config = WebConfig(project_dir=tmp_path, runtime_root=tmp_path / "copies", frontend_dist=tmp_path / "dist",
                       workspaces=[WebWorkspace(id="code", label="压缩任务", path=workspace)])
    host = RunConfig(tools=["read_file", "write_file"], context=ContextPolicy(
        compression="hybrid", max_input_bytes=15000, keep_recent_groups=1, inline_tool_bytes=1024, max_summary_calls=4,
    ))
    model = CompressionEditingModel()

    async def scenario() -> None:
        """在同一单槽服务完成两轮，核对来源/事件/SSE，再启动新实例只读查询。"""
        app = create_app(config, host, tmp_path / "runs", model_client=model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", trust_env=False) as client:
                created = await client.post("/api/runs", json={"workspace_id": "code", "prompt": "保留 USER_CONSTRAINT，连续修改验证压缩。"})
                assert created.status_code == 202, created.text
                first = await _finished(client, created.json()["run_id"])
                assert first["result"]["execution_status"] == "completed", first
                assert first["context"]["summary_calls"] >= 1
                assert any(action["kind"] == "summary" for action in first["context"]["actions"])
                directory = tmp_path / "runs" / first["run_id"]
                fingerprints = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in ("run.json", "events.jsonl", "summary.json")}
                stream = await client.get("/api/runs/" + first["run_id"] + "/stream")
                assert "context_action" in stream.text and "context_compaction_started" in stream.text
                appended = await client.post("/api/runs/" + first["run_id"] + "/followups", json={
                    "request_id": str(uuid4()), "prompt": "第二轮继续保留 USER_CONSTRAINT，重新检查代码。",
                })
                assert appended.status_code == 202, appended.text
                second = await _finished(client, appended.json()["run_id"])
                assert second["result"]["execution_status"] == "completed", second
                assert second["parent_run_id"] == first["run_id"]
                assert any(action["reason"] == "inherited" for action in second["context"]["actions"])
                assert second["context"]["summary_calls"] <= 4
                inherited = [action for action in second["context"]["actions"] if action["reason"] == "inherited"]
                for previous, current in zip(inherited, inherited[1:]):
                    assert previous["source_end"] <= current["source_start"]
                assert "return a + b" in (Path(second["workspace"]) / "app.py").read_text(encoding="utf-8")
                for name, digest in fingerprints.items():
                    assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
                assert not RunStore.read(tmp_path / "runs", second["run_id"]).warnings
                count = len(model.requests)
                stored_context = second["context"]
        restarted = create_app(config, host, tmp_path / "runs", model_client=model)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://localhost", trust_env=False) as client:
                restored = (await client.get("/api/runs/" + second["run_id"])).json()
                assert restored["context"] == stored_context
                assert len(model.requests) == count
                assert "offline-compression-key" not in json.dumps(restored)

    asyncio.run(scenario())
