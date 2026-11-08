"""通过统一作业、权限和真实流式适配器验收多供应商配置。"""

import asyncio
from collections import deque
import json
from uuid import uuid4

import httpx
import pytest

from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from tests.helpers.streaming import model_stream_response
from tests.permissions.test_permissions import decide
from tests.subagents.test_subagents import report
from tests.web.job_helpers import submit_followup, submit_run
from tests.web.test_planning_api import call, finished, reply
from tests.web.test_subagents_api import app_config


@pytest.mark.parametrize("provider,role_provider", [("deepseek", "qwen"), ("qwen", "deepseek")])
def test_provider_selection_role_approval_and_history(tmp_path, monkeypatch, provider, role_provider):
    """分别以 provider 为主模型、role_provider 为审阅模型完成同一项审批和文件交付。"""
    web = app_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-deepseek")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-qwen")
    read = call("read_file", path="api.py")
    steps = deque([
        (provider, reply(call("delegate_readonly", title="核对源码", objective="检查 api.py", role="review"))),
        (role_provider, reply(read)),
        (role_provider, reply(text=report("value=42", ids=[read.call_id]))),
        (provider, reply(call("write_file", path="result.txt", content="42"))),
        (provider, reply(text="已核对并写入结果。")),
    ])
    hosts = {"deepseek": "api.deepseek.com", "qwen": "dashscope.aliyuncs.com"}  # 固定官方域名，用离线传输断言。

    def send(request: httpx.Request) -> httpx.Response:
        """核对 request 的模型、域名、凭据和回传消息，返回下一帧离线 SSE。"""
        expected, response = steps.popleft()
        body = json.loads(request.content)
        assert request.url.host == hosts[expected]
        assert request.headers["Authorization"] == f"Bearer offline-{expected}"
        assert body["model"] == f"{expected}-requested"
        assert body["stream_options"] == {"include_usage": True}
        assert ("enable_thinking" in body) == (expected == "qwen")
        assert ("thinking" in body) == (expected == "deepseek")
        for message in body["messages"]:
            if message["role"] == "assistant" and message.get("tool_calls"):
                assert message["content"] == ("" if expected == "qwen" else None)
        if not steps:
            assert any(message["role"] == "tool" and "result.txt" in message["content"] for message in body["messages"])
        return model_stream_response(200, json={
            "model": f"{expected}-actual",
            "choices": [{"message": response.message, "finish_reason": response.finish_reason}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })

    async def scenario():
        """通过正式接纳、审批和事件接口完成任务，返回 None，副作用只写测试目录。"""
        app = create_app(web, RunConfig(tools=["read_file", "write_file"]), tmp_path / "records", transport=httpx.MockTransport(send))
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            assert all(item["key_configured"] for item in (await client.get("/api/options")).json()["model_providers"].values())
            first = await finished(client, await submit_run(client, json={
                "workspace_id": "sample", "prompt": "核对并写入结果", "config": {
                    "provider": provider, "requested_model": f"{provider}-requested", "permission_mode": "strict",
                    "role_models": {"review": {"provider": role_provider, "requested_model": f"{role_provider}-requested"}},
                },
            }))
            assert first["result"]["execution_status"] == "waiting_input", first["result"]["stop_reason"]
            assert first["config"]["provider"] == provider
            assert first["result"]["usage"]["total_tokens"] == 20
            children = (await client.get(f"/api/runs/{first['run_id']}/subagents")).json()["items"]
            assert children[0]["report"]["summary"] == "value=42"
            assert children[0]["model"]["provider"] == role_provider
            assert children[0]["response_model"] == f"{role_provider}-actual"
            path = web.workspaces[0].path / "result.txt"
            assert not path.exists()

            # 改宿主默认后，审批恢复仍只使用原会话已记录的模型。
            app.state.manager.run_config = RunConfig(provider=role_provider)
            second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
            assert second["result"]["execution_status"] == "completed", second["result"]["stop_reason"]
            assert second["result"]["response_model"] == f"{provider}-actual"
            assert second["config"]["provider"] == provider
            assert path.read_text(encoding="utf-8") == "42"
            events = (await client.get(f"/api/runs/{second['run_id']}/events")).json()["items"]
            assert any(event["type"] == "model_delta" for event in events)
            switched = await submit_followup(client, f"/api/runs/{second['run_id']}/followups", json={
                "request_id": str(uuid4()), "prompt": "切换",
                "config": {"provider": role_provider, "requested_model": f"{role_provider}-requested"},
            })
            assert switched.status_code == 409 and switched.json()["error"]["code"] == "session_model_locked"
            assert not steps

    asyncio.run(scenario())


def test_qwen_requires_its_own_key(tmp_path, monkeypatch):
    """只有 DeepSeek 凭据时，接纳拒绝 Qwen 运行且不访问任一模型。"""
    web = app_config(tmp_path, monkeypatch)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    async def scenario():
        """读取两家配置状态并提交 Qwen 请求，返回 None，不建立运行记录。"""
        app = create_app(web, RunConfig(), tmp_path / "records")
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            providers = (await client.get("/api/options")).json()["model_providers"]
            assert providers["deepseek"]["key_configured"] and not providers["qwen"]["key_configured"]
            denied = await submit_run(client, json={
                "workspace_id": "sample", "prompt": "检查项目",
                "config": {"provider": "qwen", "requested_model": "qwen-plus"},
            })
            assert denied.status_code == 400 and "DASHSCOPE_API_KEY" in denied.json()["error"]["message"]

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,reason,attempts", [
    ("rate_limited", "model_rate_limited", 2),
    ("interrupted", "model_stream_interrupted", 1),
])
def test_qwen_failure_does_not_apply_incomplete_action(tmp_path, monkeypatch, mode, reason, attempts):
    """分别注入限流和未完成的工具响应；核对停止原因、尝试次数及工作区无写入。"""
    web = app_config(tmp_path, monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-qwen")

    def send(request: httpx.Request) -> httpx.Response:
        """按 mode 返回供应商限流或缺少 DONE 的工具响应，忽略测试请求正文。"""
        if mode == "rate_limited":
            return httpx.Response(429, json={"message": "离线限流"})
        tool = reply(call("write_file", path="bad.txt", content="不可执行"))
        response = model_stream_response(200, json={
            "model": "qwen-plus",
            "choices": [{"message": tool.message, "finish_reason": "tool_calls"}],
        })
        return httpx.Response(200, content=response.content.split(b"data: [DONE]", 1)[0])

    async def scenario():
        """通过真实运行器触发故障并核对终态，无未验证副作用。"""
        config = RunConfig(provider="qwen", tools=["write_file"], limits={"max_http_attempts_per_turn": 2})
        app = create_app(web, config, tmp_path / "records", transport=httpx.MockTransport(send))
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            view = await finished(client, await submit_run(client, json={"workspace_id": "sample", "prompt": "测试异常"}))
            result = view["result"]
            assert result["stop_reason"] == reason
            assert result["http_attempts"] == attempts and result["tool_calls"] == 0
            assert not result["usage_complete"]
            assert not (web.workspaces[0].path / "bad.txt").exists()

    asyncio.run(scenario())
