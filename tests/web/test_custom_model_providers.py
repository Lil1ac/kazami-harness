"""通过真实宿主和离线兼容响应验证自定义服务商的绑定与任务生命周期。"""

import asyncio
from collections import deque
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from agent_harness.desktop.host import DesktopHost
from agent_harness.desktop.storage import PreferenceStore
from agent_harness.model_selection import CustomProvider
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from tests.helpers.streaming import model_stream_response
from tests.permissions.test_permissions import decide
from tests.subagents.test_subagents import report
from tests.web.test_automations_api import action, definition as automation_definition, settled
from tests.web.job_helpers import submit_followup, submit_run
from tests.web.test_jobs_api import wait_job
from tests.web.test_planning_api import call, finished, reply
from tests.web.test_subagents_api import app_config


class NativeKeys:
    """用测试内存替代 Windows 密文文件，核对私有调用只接触指定分区。"""

    def __init__(self):
        """建立各凭据分区的空存储。"""
        self.slots: dict[str, str] = {}  # 分区标识到测试密钥的映射。

    async def request(self, method: str, parameters: dict) -> object:
        """模拟 method 对 parameters 指定分区的读取、写入或清除。"""
        slot = parameters["provider"]
        if method == "credential.read":
            return self.slots.get(slot, "")
        assert method == "credential.write"
        if parameters["secret"]:
            self.slots[slot] = parameters["secret"]
        else:
            self.slots.pop(slot, None)
        return None


def sse(response, model="custom-actual") -> httpx.Response:
    """把 response 转为离线 Chat Completions 响应，model 为服务返回的实际模型。"""
    return model_stream_response(200, json={
        "model": model, "choices": [{"message": response.message, "finish_reason": response.finish_reason}],
    })


@pytest.mark.parametrize("address", [
    "http://example.test/v1", "https://key@example.test/v1", "https://example.test/v1?key=private",
    "https://example.test/v1#fragment", "http://127.0.0.2/v1", "file:///tmp/v1",
    "https://example.test/v1/chat/completions", "https://example.test\\@127.0.0.1/v1",
])
def test_custom_address_validation(address):
    """拒绝非预期传输或有歧义的 address，配置阶段不发请求。"""
    with pytest.raises(ValidationError):
        CustomProvider(name="自定义", base_url=address, default_model="fixture")


def test_desktop_register_probe_run_role_and_history(tmp_path, monkeypatch):
    """登记免密端点后真实运行工具和审阅角色，探测不调用项目工具。"""
    web = app_config(tmp_path, monkeypatch)
    read = call("read_file", path="api.py")
    script = deque([
        reply(text="OK"), reply(call("harness_probe", value="ok")),
        reply(call("delegate_readonly", title="核对源码", objective="阅读 api.py", role="review")),
        reply(read), reply(text=report("value=42", ids=[read.call_id])),
        reply(call("write_file", path="result.txt", content="42")), reply(text="已写入"),
    ])
    requests: list[dict] = []  # 捕获离线发送体，检查模型及项目上下文边界。

    def send(request: httpx.Request) -> httpx.Response:
        """验证 request 到固定回环地址并返回脚本中下一帧。"""
        assert str(request.url) == "http://127.0.0.1:11434/v1/chat/completions"
        assert "Authorization" not in request.headers
        body = json.loads(request.content)
        assert "stream_options" not in body and body["stream"] is True
        requests.append(body)
        return sse(script.popleft())

    async def scenario():
        """通过受认证桌面 HTTP 完成登记、探测、审批接续及历史核对。"""
        store = PreferenceStore(tmp_path / "desktop")
        host = DesktopHost(store, NativeKeys(), "a" * 64, {})
        app = create_app(web, RunConfig(tools=["read_file", "write_file"]), tmp_path / "records", desktop=host, transport=httpx.MockTransport(send))
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.cookies.set(host.cookie_name, host.token)
            created = await client.post("/api/desktop/model-providers", json={
                "name": "本机网关", "base_url": "http://localhost:11434/v1/", "default_model": "sample", "auth": "none",
            })
            assert created.status_code == 201, created.text
            provider = created.json()["provider"]
            version = (await client.get("/api/options")).json()["model_providers"][provider]["version"]
            assert PreferenceStore(store.directory).value.custom_providers[provider].version == version
            response = await client.post(f"/api/model-providers/{provider}/probe", json={"version": version})
            assert response.json() == {"connection": "passed", "authentication": "not_required", "stream": "passed", "tools": "passed", "error_code": None}
            assert all(len(request["messages"]) == 1 for request in requests)
            assert not list((tmp_path / "records").glob("*/run.json"))
            selection = {"provider": provider, "requested_model": "sample"}
            first = await finished(client, await submit_run(client, json={
                "workspace_id": "sample", "prompt": "核对并写入结果", "model_versions": {provider: version},
                "config": {**selection, "permission_mode": "strict",
                           "role_models": {"review": {**selection, "requested_model": "review-model"}}},
            }))
            assert first["result"]["execution_status"] == "waiting_input", first["result"]["stop_reason"]
            assert [body["model"] for body in requests] == ["sample", "sample", "sample", "review-model", "review-model", "sample"]
            child = first["result"]["subagents"][0]
            assert child["provider_name"] == "本机网关" and child["response_model"] == "custom-actual"
            second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
            assert second["result"]["execution_status"] == "completed", second["result"]["stop_reason"]
            assert not second["result"]["usage_complete"]
            assert (web.workspaces[0].path / "result.txt").read_text(encoding="utf-8") == "42"
            history = (await client.get(f"/api/runs/{second['run_id']}")).json()
            assert history["config"]["custom_providers"][provider]["base_url"] == "http://127.0.0.1:11434/v1"
            assert not script

    asyncio.run(scenario())


def test_changed_address_rejects_queued_approval_and_old_credentials(tmp_path, monkeypatch):
    """地址变更后旧作业、旧审批与旧页面写密钥均被拦截，旧凭据不发给新地址。"""
    web = app_config(tmp_path, monkeypatch)
    calls: list[str] = []  # 已发送的真实逻辑请求地址，便于断言拦截前后没有额外调用。

    def send(request: httpx.Request) -> httpx.Response:
        """只有原绑定可接收测试密钥，并返回需要审批的写入。"""
        calls.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer offline-old-key"
        return sse(reply(call("write_file", path="no-write.txt", content="unsafe")))

    async def scenario():
        """在桌面认证范围内变更连接并处理旧任务，校验没有请求或项目写入。"""
        native = NativeKeys()
        store = PreferenceStore(tmp_path / "desktop")
        host = DesktopHost(store, native, "b" * 64, {})
        app = create_app(web, RunConfig(tools=["write_file"]), tmp_path / "records", desktop=host, transport=httpx.MockTransport(send))
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.cookies.set(host.cookie_name, host.token)
            definition = {"name": "自建网关", "base_url": "https://old.example.test/v1", "default_model": "agent"}
            provider = (await client.post("/api/desktop/model-providers", json=definition)).json()["provider"]
            old = store.value.custom_providers[provider]
            credential = f"/api/desktop/credentials/{provider}"
            saved = await client.put(credential, json={"version": old.version, "secret": "offline-old-key"})
            assert saved.json() == {"key_configured": True}
            # 同一连接在重启时只从当前的分区取回密钥。
            restored = DesktopHost(PreferenceStore(store.directory), native, "b" * 64, {})
            await restored.load_custom_keys(app.state.manager.run_config)
            assert restored.api_key(RunConfig(provider=provider, custom_providers={provider: old})) == "offline-old-key"
            request = {"workspace_id": "sample", "prompt": "写入", "model_versions": {provider: old.version},
                       "config": {"provider": provider, "requested_model": "agent", "permission_mode": "strict"}}
            first = await finished(client, await submit_run(client, json=request))
            assert first["result"]["execution_status"] == "waiting_input"
            app.state.manager.jobs.paused = True
            queued = (await client.post("/api/jobs", json={"request_id": str(uuid4()), "create": request})).json()
            assert queued["status"] == "queued"
            edited = await client.put(f"/api/desktop/model-providers/{provider}", json={
                "definition": {**definition, "base_url": "https://new.example.test/v1"}, "version": old.version,
            })
            assert edited.status_code == 200, edited.text
            current = (await client.get("/api/options")).json()["model_providers"][provider]
            assert current["version"] != old.version and not current["key_configured"] and not native.slots
            assert (await client.put(credential, json={"version": old.version, "secret": "offline-stale"})).status_code == 409
            rejected = await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first))
            assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "model_provider_changed"
            assert (await submit_run(client, json=request)).status_code == 409
            await client.post("/api/queue/resume")
            final = await wait_job(client, queued["id"])
            assert final["error_code"] == "model_provider_changed"
            assert calls == ["https://old.example.test/v1/chat/completions"]
            assert not (web.workspaces[0].path / "no-write.txt").exists()
            assert "offline-old-key" not in store.path.read_text(encoding="utf-8")

    asyncio.run(scenario())


def test_automation_requires_review_after_custom_endpoint_change(tmp_path, monkeypatch):
    """自定义端点可执行已登记的自动化，地址变化后原规则不会发出新请求。"""
    web = app_config(tmp_path, monkeypatch)
    provider = "custom-automation"
    connection = CustomProvider(name="自动化模型", base_url="http://127.0.0.1:11434/v1",
                                default_model="agent", auth="none")
    sent: list[str] = []  # 记录目标路径，区分旧地址和改址后的潜在请求。

    def send(request: httpx.Request) -> httpx.Response:
        """记录 request 的目的地并返回固定完成响应。"""
        sent.append(str(request.url))
        return sse(reply(text="巡检完成"))

    async def scenario():
        """试跑规则并改变宿主配置，核对规则历史保留而新执行被阻止。"""
        config = RunConfig(custom_providers={provider: connection})
        app = create_app(web, config, tmp_path / "records", transport=httpx.MockTransport(send))
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            body = automation_definition()
            body["task"].update(workspace_id="sample", config={"provider": provider, "requested_model": "agent"})
            created = await client.post("/api/automations", json=body)
            assert created.status_code == 201, created.text
            rule = created.json()
            assert (await action(client, rule, "run")).status_code == 200
            first = await settled(client, app, rule)
            assert first["status"] == "completed", first
            moved = CustomProvider(**{**connection.model_dump(), "base_url": "http://127.0.0.1:19999/v1"})
            app.state.manager.run_config = RunConfig(custom_providers={provider: moved})
            response = await action(client, rule, "enable")
            assert response.status_code == 409 and response.json()["error"]["code"] == "automation_review_changed"
            assert sent == ["http://127.0.0.1:11434/v1/chat/completions"]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure,code", [(401, "model_authentication"), (302, "model_redirect"), (200, "model_stream_interrupted")])
def test_probe_rejects_wrong_key_redirect_and_partial_stream(tmp_path, monkeypatch, failure, code):
    """注入 failure 对应的受控响应，断言错误码、凭据边界和不重放。"""
    web = app_config(tmp_path, monkeypatch)
    monkeypatch.setenv("FIXTURE_GATEWAY_KEY", "offline-probe-key")
    definition = CustomProvider(name="兼容服务", base_url="https://probe.example.test/v1",
                                default_model="probe", api_key_env="FIXTURE_GATEWAY_KEY")
    seen: list[str] = []  # 探测实际请求 URL，仅用于校验无重定向。

    def send(request: httpx.Request) -> httpx.Response:
        """记录 request 并返回指定 HTTP 故障或没有 DONE 的流。"""
        assert request.headers["Authorization"] == "Bearer offline-probe-key"
        seen.append(str(request.url))
        if failure == 200:
            return httpx.Response(200, content=sse(reply(text="partial")).content.split(b"data: [DONE]", 1)[0])
        return httpx.Response(failure, text="untrusted-body", headers={"Location": "https://wrong.example.test/"})

    async def scenario():
        """只触发连接探测并核对报告，项目和网络响应正文都不回传。"""
        app = create_app(web, RunConfig(custom_providers={"custom-fixture": definition}), tmp_path / "records", transport=httpx.MockTransport(send))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            result = await client.post("/api/model-providers/custom-fixture/probe", json={"version": definition.version})
            assert result.status_code == 200 and result.json()["error_code"] == code
            assert "untrusted-body" not in result.text
            assert seen == ["https://probe.example.test/v1/chat/completions"]

    asyncio.run(scenario())
