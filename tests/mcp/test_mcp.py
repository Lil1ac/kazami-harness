"""真实 SDK、工具执行及 HTTP 页面接口的 MCP 回归。"""

import asyncio
import json
import sys
import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_harness.mcp.client import McpError, local_identity, perform, validate_schema
from agent_harness.mcp.models import McpServerConfig
from agent_harness.mcp.store import McpStore, tool_name
from agent_harness.permissions.models import PermissionPolicy
from agent_harness.schemas import RunConfig
from agent_harness.work.tasks import TaskError
from mcp.client.stdio import get_default_environment
from tests.permissions.test_permissions import setup_app, decide
from tests.web.job_helpers import submit_run, submit_followup
from tests.web.test_planning_api import call, reply, finished


def config_for(tmp_path, monkeypatch, **overrides):
    """在 tmp_path 创建资料并登记环境；返回固定本机服务配置，overrides 调整时限或变量。"""
    document = tmp_path / "docs.txt"
    document.write_text("这是工具真实读取的参考内容", encoding="utf-8")
    monkeypatch.setenv("HARNESS_MCP_FIXTURE", str(document))
    return McpServerConfig.model_validate({"id": "docs", "label": "验收文档", "transport": "stdio",
        "command": sys.executable, "args": [str(Path("tests/helpers/mcp_server.py").resolve())], "cwd": str(tmp_path),
        "env_vars": ["HARNESS_MCP_FIXTURE"], **overrides})


def register(store, config, catalog, *, research=("lookup",)):
    """把 config/catalog 按实际审核状态保存到 store；返回启用记录和本轮 lookup 选择。"""
    item = store.save(config, None)
    item = store.update(config.id, item["fingerprint"], {"trusted": True, "trusted_identity": local_identity(config), "catalog": catalog, "research_tools": list(research), "enabled": True, "status": "ready"})
    return item, {"name": tool_name(config.id, "lookup"), "fingerprint": item["fingerprint"]}


def test_real_stdio_discovery_call_and_secret_redaction(tmp_path, monkeypatch):
    """启动真实子进程，发现工具、校验 Schema、读取资料并隐藏外部返回的测试秘密。"""
    async def scenario():
        """驱动两次有界 SDK 调用；返回 None，所有临时资料仅在 tmp_path。"""
        monkeypatch.setenv("HARNESS_MCP_SECRET", "private-mcp-test-value")
        config = config_for(tmp_path, monkeypatch, env_vars=["HARNESS_MCP_FIXTURE", "HARNESS_MCP_SECRET"])
        discovered = await perform(config)
        assert len(discovered["catalog"]) == 4
        lookup = next(item for item in discovered["catalog"] if item["name"] == "lookup")
        output = await perform(config, tool=lookup, arguments={"query": "文档"})
        assert "这是工具真实读取" in json.dumps(output, ensure_ascii=False)
        echo = next(item for item in discovered["catalog"] if item["name"] == "echo_credential")
        output = await perform(config, tool=echo, arguments={})
        assert "private-mcp-test-value" not in json.dumps(output)
        assert "环境值已隐藏" in json.dumps(output, ensure_ascii=False)
    asyncio.run(scenario())


def test_revocation_never_restores_old_fingerprint(tmp_path):
    """停用再启用同一服务也递增代次，防止旧选择或动作许可复活。"""
    store = McpStore(tmp_path)
    config = McpServerConfig(id="docs", label="文档", transport="http", url="https://example.test/mcp")
    item, _ = register(store, config, [])
    first = item["fingerprint"]
    item = store.update("docs", first, {"enabled": False})
    item = store.update("docs", item["fingerprint"], {"enabled": True})
    assert item["fingerprint"] != first
    with pytest.raises(TaskError, match="变化"):
        store.update("docs", first, {"enabled": False})


def test_external_schema_references_are_rejected():
    """目录和参数校验不能根据外部 $ref 发起额外网络请求。"""
    with pytest.raises(ValueError):
        validate_schema({"$ref": "https://example.test/secret-schema"})


def test_page_approval_executes_exact_call_once_and_preserves_history(tmp_path, monkeypatch):
    """页面选择→真实外部审批→SDK 读取→来源及旧历史，拒绝前不连接业务工具。"""
    async def scenario():
        """驱动真实队列和两段运行；返回 None，验证原工具参数不能被批准载荷替换。"""
        config = config_for(tmp_path, monkeypatch)
        catalog = (await perform(config))["catalog"]
        app, model, _ = setup_app(tmp_path, monkeypatch, [reply(call(tool_name("docs", "lookup"), query="初始参数")), reply(text="参见 https://example.test/docs/reference")])
        item, selection = register(app.state.manager.mcp, config, catalog)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"prompt": "查文档", "workspace_id": "files", "config": {"mcp_tools": [selection]}}))
                assert first["result"]["stop_reason"] == "action_approval", first
                action = first["result"]["permissions"]["pending"]
                assert action["arguments"] == {"query": "初始参数"}
                before = (await client.get(f"/api/runs/{first['run_id']}/mcp")).json()
                assert before["items"] == []
                second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
                assert second["result"]["execution_status"] == "completed", second
                evidence = (await client.get(f"/api/runs/{second['run_id']}/mcp")).json()
                result = next(event for event in evidence["items"] if event["type"] == "mcp_call_result")
                assert result["sources"] == ["https://example.test/docs/reference"]
                assert "这是工具真实读取" in json.dumps(result, ensure_ascii=False)
                assert len(model.requests) == 2
                events = (await client.get(f"/api/runs/{second['run_id']}/events?limit=100")).json()["items"]
                assert next(event["payload"]["title"] for event in events if event["type"] == "tool_started") == "验收文档 / lookup"
                app.state.manager.mcp.update("docs", item["fingerprint"], {"enabled": False})
                assert (await client.get(f"/api/runs/{second['run_id']}/mcp")).json() == evidence
    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["reject", "cancel", "revoke"])
def test_rejection_cancel_and_revocation_do_not_call_external_tool(tmp_path, monkeypatch, decision):
    """decision 拒绝、取消或撤权后批准均不产生外部计数文件，也不调用额外模型。"""
    async def scenario():
        """运行待审批 change 工具；返回 None，检查真实进程副作用不存在。"""
        config = config_for(tmp_path, monkeypatch)
        catalog = (await perform(config))["catalog"]
        app, model, _ = setup_app(tmp_path, monkeypatch, [reply(call(tool_name("docs", "change"), value="1"))])
        item, _ = register(app.state.manager.mcp, config, catalog)
        selection = {"name": tool_name("docs", "change"), "fingerprint": item["fingerprint"]}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"prompt": "写入计数", "workspace_id": "files", "config": {"mcp_tools": [selection]}}))
                assert first["result"]["stop_reason"] == "action_approval", first
                if decision == "revoke":
                    app.state.manager.mcp.update("docs", item["fingerprint"], {"enabled": False})
                second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first, "approve" if decision == "revoke" else decision)))
                assert second["result"]["execution_status"] in {"failed", "cancelled"}
                assert not (tmp_path / "docs.count").exists()
                assert len(model.requests) == 1
    asyncio.run(scenario())


def test_management_requires_trust_and_fresh_version(tmp_path, monkeypatch):
    """管理页先保存审核再发现；目录、研究授权及启停变化都会使旧选择失效。"""
    async def scenario():
        """通过真实 API 管理 stdio 服务，返回 None，不调用业务工具。"""
        config = config_for(tmp_path, monkeypatch)
        app, _, _ = setup_app(tmp_path, monkeypatch, [])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                saved = (await client.put("/api/mcp", json={"config": config.model_dump()})).json()
                assert not saved["trusted"] and not saved["enabled"]
                refused = await client.post(f"/api/mcp/docs/discover?fingerprint={saved['fingerprint']}")
                assert refused.status_code == 409
                trusted = (await client.post("/api/mcp/docs/review", json={"fingerprint": saved["fingerprint"], "action": "trust"})).json()
                discovered = (await client.post(f"/api/mcp/docs/discover?fingerprint={trusted['fingerprint']}")).json()
                assert discovered["status"] == "discovered", discovered
                assert not discovered["research_tools"]
                researched = (await client.post("/api/mcp/docs/review", json={"fingerprint": discovered["fingerprint"], "action": "research", "research_tools": ["lookup"]})).json()
                enabled = (await client.post("/api/mcp/docs/review", json={"fingerprint": researched["fingerprint"], "action": "enable"})).json()
                assert enabled["enabled"]
                options = (await client.get("/api/options")).json()["mcp_tools"]
                assert len(options) == 4 and next(item for item in options if item["tool_name"] == "lookup")["research"]
                stale = await client.post("/api/mcp/docs/review", json={"fingerprint": discovered["fingerprint"], "action": "enable"})
                assert stale.status_code == 409
                assert not (tmp_path / "docs.count").exists()
    asyncio.run(scenario())


def test_real_http_transport_on_loopback(tmp_path, monkeypatch):
    """真实 HTTP 服务完成发现及查询；环回连接不经过宿主代理，测试结束清理服务进程。"""
    async def scenario():
        """启动固定可控服务并验证网络传输；返回 None，监听地址仅为127.0.0.1。"""
        config_for(tmp_path, monkeypatch)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment = {**get_default_environment(), "HARNESS_MCP_FIXTURE": str(tmp_path / "docs.txt"), "PYTHONUTF8": "1"}
        process = subprocess.Popen([sys.executable, str(Path("tests/helpers/mcp_server.py").resolve()), "--http", str(port)], env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            async with asyncio.timeout(15):
                async with httpx.AsyncClient(trust_env=False) as probe:
                    while True:
                        try:
                            await probe.get(f"http://127.0.0.1:{port}/mcp")
                            break
                        except httpx.TransportError:
                            await asyncio.sleep(.05)
            config = McpServerConfig(id="http-docs", label="HTTP文档", transport="http", url=f"http://127.0.0.1:{port}/mcp")
            discovered = await perform(config)
            tool = next(item for item in discovered["catalog"] if item["name"] == "lookup")
            result = await perform(config, tool=tool, arguments={"query": "HTTP实际查询"})
            assert "HTTP实际查询" in json.dumps(result, ensure_ascii=False)
        finally:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_or_cancel_disables_connection_without_replaying(tmp_path, monkeypatch, cancel):
    """外部工具先写入后超时或取消；只产生一次计数，连接停用，未知结果不能自动重放。"""
    async def scenario():
        """cancel=True 显式停止活动 Run；否则使用1秒调用时限，返回 None 并检查持久证据。"""
        config = config_for(tmp_path, monkeypatch, tool_timeout_seconds=1.0 if not cancel else 60.0)
        catalog = (await perform(config))["catalog"]
        app, _, _ = setup_app(tmp_path, monkeypatch, [reply(call(tool_name("docs", "slow"), value="1"))], config=RunConfig(permission_policy=PermissionPolicy(external="allow")))
        item, _ = register(app.state.manager.mcp, config, catalog)
        selection = {"name": tool_name("docs", "slow"), "fingerprint": item["fingerprint"]}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                response = await submit_run(client, json={"prompt": "执行慢任务", "workspace_id": "files", "config": {"mcp_tools": [selection]}})
                if cancel:
                    async with asyncio.timeout(15):
                        while not (tmp_path / "docs.count").exists():
                            await asyncio.sleep(.02)
                    stopped = await client.post(f"/api/runs/{response.json()['run_id']}/cancel")
                    assert stopped.status_code == 200
                view = await finished(client, response)
                assert view["result"]["execution_status"] == ("cancelled" if cancel else "failed"), view
                assert (tmp_path / "docs.count").read_text() == "1"
                current = app.state.manager.mcp.get("docs")
                assert not current["enabled"] and current["status"] == "needs_review"
                evidence = (await client.get(f"/api/runs/{view['run_id']}/mcp")).json()
                assert any(event.get("unknown") for event in evidence["items"])
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,research,host,expected", [
    ("research", False, "allow", "permission_denied"),
    ("plan", False, "allow", "permission_denied"),
    ("plan", True, "ask", "approval_required"),
    ("research", True, "allow", None),
    ("workspace_edit", True, "deny", "permission_denied"),
    ("strict", True, "allow", "approval_required"),
])
def test_research_and_host_permissions_apply_to_dynamic_tools(tmp_path, monkeypatch, mode, research, host, expected):
    """mode/research/host 组合必须经统一权限判定；expected 非空时外部业务请求数保持零。"""
    async def scenario():
        """用可控 HTTP 替身隔离权限判断，真实队列和审批照常运行，返回 None。"""
        called = []
        async def perform_tool(config, **kwargs):
            """只记录真正越过权限的 config/kwargs 调用，返回固定研究结果，不访问网络。"""
            called.append(kwargs)
            return {"protocol_version": "2026-07-28", "result": {"content": [{"type": "text", "text": "https://example.test/docs/reference"}]}}
        monkeypatch.setattr("agent_harness.mcp.controller.perform", perform_tool)
        server = McpServerConfig(id="docs", label="文档", transport="http", url="https://example.test/mcp")
        catalog = [{"name": "lookup", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]
        app, _, _ = setup_app(tmp_path, monkeypatch, [reply(call(tool_name("docs", "lookup"), query="research")), reply(text="结束")],
            config=RunConfig(permission_policy=PermissionPolicy(external=host)))
        _, selection = register(app.state.manager.mcp, server, catalog, research=("lookup",) if research else ())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                view = await finished(client, await submit_run(client, json={"prompt": "研究资料", "workspace_id": "files", "mode": "plan" if mode == "plan" else "auto", "config": {"permission_mode": "workspace_edit" if mode == "plan" else mode, "mcp_tools": [selection]}}))
                page = (await client.get(f"/api/runs/{view['run_id']}/events?limit=100")).json()
                result = next(event["payload"]["result"] for event in page["items"] if event["type"] == "tool_result")
                assert (result.get("error") or {}).get("code") == expected
                assert len(called) == (0 if expected else 1)
    asyncio.run(scenario())


def test_readonly_child_inherits_only_reviewed_parent_tools_and_real_evidence(tmp_path, monkeypatch):
    """真实父子循环只继承父已选的研究工具，结构报告能引用实际外部回执与来源。"""
    async def scenario():
        """驱动父委派、子查询和证据报告；返回 None，外部替身只供确定性权限验收。"""
        async def perform_tool(config, **kwargs):
            """返回 config 的固定文档结果，kwargs 是已通过宿主校验的原调用。"""
            return {"protocol_version": "2026-07-28", "result": {"content": [{"type": "text", "text": "https://example.test/docs/child"}]}}
        monkeypatch.setattr("agent_harness.mcp.controller.perform", perform_tool)
        child_call = call(tool_name("docs", "lookup"), query="child")
        report = json.dumps({"summary": "查询完成", "findings": [{"category": "observation", "title": "文档来源", "detail": "实际工具返回来源", "evidence_call_ids": [child_call.call_id]}], "limitations": [], "pending": [], "complete": True}, ensure_ascii=False)
        app, model, _ = setup_app(tmp_path, monkeypatch, [reply(call("delegate_readonly", title="文档研究", objective="查询指定参考资料", max_model_turns=3)), reply(child_call), reply(text=report), reply(text="父总结")], config=RunConfig(tools=["read_file"], permission_policy=PermissionPolicy(external="allow")))
        server = McpServerConfig(id="docs", label="文档", transport="http", url="https://example.test/mcp")
        catalog = [{"name": "lookup", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}, {"name": "change", "inputSchema": {"type": "object"}}]
        item, selection = register(app.state.manager.mcp, server, catalog)
        unreviewed = {"name": tool_name("docs", "change"), "fingerprint": item["fingerprint"]}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                view = await finished(client, await submit_run(client, json={"prompt": "委派查询", "workspace_id": "files", "config": {"mcp_tools": [selection, unreviewed]}}))
                assert view["result"]["execution_status"] == "completed", view
                child = view["result"]["subagents"][0]
                assert not child["report"]["partial"], child
                assert any(item["tool"] == selection["name"] and item["path"] == "https://example.test/docs/child" for item in child["report"]["evidence"])
                child_names = {tool["function"]["name"] for tool in model.requests[1]["tools"]}
                assert selection["name"] in child_names and unreviewed["name"] not in child_names
    asyncio.run(scenario())


def test_http_auth_error_is_classified_without_leaking_secret(capsys):
    """真实 HTTP 401 返回即使回显秘密也不会进入异常或 SDK 日志，客户端正确报告需要认证。"""
    received = []
    class AuthServer(BaseHTTPRequestHandler):
        """只提供确定性的401，不运行任何 MCP 业务工具。"""

        def do_POST(self):
            """记录认证头并返回401；返回 None，回显正文用于测试脱敏边界。"""
            received.append(self.headers.get("Authorization"))
            body = b"private-auth-test-value"
            self.send_response(401)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            """忽略验收服务 format/args 日志，避免请求头进入测试终端；返回 None。"""
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), AuthServer)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        config = McpServerConfig(id="auth", label="认证案例", transport="http", url=f"http://127.0.0.1:{server.server_port}/mcp")
        with pytest.raises(McpError) as caught:
            asyncio.run(perform(config, secret="private-auth-test-value"))
        assert caught.value.code == "mcp_auth_required"
        assert received and set(received) == {"Bearer private-auth-test-value"}
        output = capsys.readouterr()
        assert "private-auth-test-value" not in str(caught.value) + output.out + output.err
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("failed", [False, True])
def test_desktop_credential_update_revokes_old_authority_and_blocks_retrust(tmp_path, failed):
    """秘密跨 await 写入期间禁止重新信任；成功和失败均撤销旧选择，公开记录不含秘密。"""
    async def scenario():
        """模拟原生保存的等待窗口，通过真实桌面 HTTP 验证并发授权边界，返回 None。"""
        from agent_harness.config import ConfigurationError
        from agent_harness.desktop.host import DesktopHost
        from agent_harness.desktop.storage import PreferenceStore
        from agent_harness.web.api import create_app
        from agent_harness.web.config import WebConfig

        class Native:
            """可控原生边界，不写入真实凭据库。"""

            def __init__(self):
                """建立等待事件及秘密内存，不产生文件副作用，返回 None。"""
                self.entered = asyncio.Event()  # 表示已进入原生保存。
                self.release = asyncio.Event()  # 明确放行后才结束保存。

            async def request(self, method, parameters):
                """仅模拟 method 对应保存；parameters 不返回页面，failed 时抛安全异常。"""
                if method == "mcp.credential.write":
                    self.entered.set()
                    await self.release.wait()
                    if failed:
                        raise ConfigurationError("原生加密不可用")
                return None

        native = Native()
        host = DesktopHost(PreferenceStore(tmp_path / "data"), native, "a" * 64, "test-key")
        app = create_app(WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=tmp_path / "dist"), RunConfig(), tmp_path / "runs", desktop=host)
        server = McpServerConfig(id="docs", label="文档", transport="http", url="https://example.test/mcp")
        item, _ = register(app.state.manager.mcp, server, [])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                client.cookies.set(host.cookie_name, host.token)
                pending = asyncio.create_task(client.put("/api/mcp/docs/credential", json={"fingerprint": item["fingerprint"], "secret": "private-native-test-value"}))
                await native.entered.wait()
                current = app.state.manager.mcp.get("docs")
                assert not current["trusted"] and not current["enabled"] and current["fingerprint"] != item["fingerprint"]
                blocked = await client.post("/api/mcp/docs/review", json={"fingerprint": current["fingerprint"], "action": "trust"})
                assert blocked.status_code == 409
                native.release.set()
                result = await pending
                assert result.status_code == (400 if failed else 200), result.text
                public = (await client.get("/api/mcp")).text
                assert "private-native-test-value" not in public + result.text
                assert b"private-native-test-value" not in app.state.manager.mcp.path.read_bytes()
    asyncio.run(scenario())
