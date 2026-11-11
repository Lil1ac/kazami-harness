"""通过真实 HTTP、队列和重启审批验证 Hook 阶段，不重复执行原工具。"""

import asyncio
import json
import sys

import httpx
import pytest

from agent_harness.hooks.models import HookPolicy, HookRule
from agent_harness.permissions.models import PermissionPolicy
from tests.helpers.models import configured_run
from tests.permissions.test_permissions import setup_app, decide
from tests.web.job_helpers import submit_run, submit_followup
from tests.web.test_planning_api import call, reply, finished


def hook_app(tmp_path, monkeypatch, event="PostToolUse", responses=None):
    """在 tmp_path 建立需逐次命令审批的 event 脚本和离线服务；返回应用、模型及实际工作区。"""
    script = tmp_path / "hook.py"
    script.write_text('import json,pathlib,sys\nx=json.load(sys.stdin)\np=pathlib.Path("hook-count")\np.write_text(p.read_text()+"1" if p.exists() else "1")\nprint("{}")', encoding="utf-8")
    policy = HookPolicy(rules=[HookRule(id="format", event=event, tools=["write_file"] if "Tool" in event else [], argv=[sys.executable, str(script)])])
    config = configured_run(tools=["read_file", "write_file", "run_command"], permission_policy=PermissionPolicy(command="ask", verification="allow"), hooks=policy)
    return setup_app(tmp_path, monkeypatch, responses if responses is not None else [reply(call("write_file", path="result.txt", content="first")), reply(text="完成")], config=config)


async def trust(client):
    """通过 client 读取并审核页面将显示的精确定义；返回审核后的规则，不执行脚本。"""
    catalog = await client.get("/api/hooks", params={"project_id": "files"})
    assert catalog.status_code == 200, catalog.text
    for item in catalog.json()["items"]:
        result = await client.post("/api/hooks/review?project_id=files", json={"key": item["key"], "fingerprint": item["fingerprint"], "action": "trust"})
        assert result.status_code == 200, result.text
        assert next(value for value in result.json()["items"] if value["key"] == item["key"])["trusted"]


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse", "RunStart", "Stop"])
def test_hook_approval_resumes_original_stage(tmp_path, monkeypatch, event):
    """事件 event 需审批时保留阶段；批准后只执行一次脚本，原写入不重复且后续模型历史配对。"""
    async def scenario():
        """运行本地应用完整生命周期；返回 None，异常由 pytest 报告，不访问网络。"""
        app, model, workspace = hook_app(tmp_path, monkeypatch, event)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                await trust(client)
                first = await finished(client, await submit_run(client, json={"prompt": "first", "workspace_id": "files"}))
                assert first["result"]["stop_reason"] == "action_approval", first
                waits = [component for phase in first["lifecycle"] for component in phase["components"]
                         if component["outcome"] == "wait"]
                assert waits and waits[-1]["reason"] == "action_approval"
                assert not (workspace / "hook-count").exists()
                if event in {"PostToolUse", "Stop"}:
                    assert (workspace / "result.txt").read_text() == "first"
                    (workspace / "result.txt").write_text("kept", encoding="utf-8")
                second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
                assert second["result"]["execution_status"] == "completed", second
                startup = next(phase for phase in second["lifecycle"] if phase["stage"] == "start")
                resume = next(component for component in startup["components"] if component["name"] == "permission.resume")
                assert resume["source"] == "agent_harness.runtime.resume"
                assert resume["outcome"] == ("finish" if event == "Stop" else "proceed")
                assert (workspace / "hook-count").read_text() == "1"
                assert (workspace / "result.txt").read_text() == ("kept" if event in {"PostToolUse", "Stop"} else "first")
                assert len(model.requests) == 2
    asyncio.run(scenario())


def test_changed_definition_invalidates_pending_approval(tmp_path, monkeypatch):
    """等待批准期间脚本变化，旧动作无法执行且 marker 保持不存在。"""
    async def scenario():
        """驱动读取、审核、暂停和批准；返回 None，不修改仓库或启动外部模型。"""
        app, _, workspace = hook_app(tmp_path, monkeypatch, "PreToolUse")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                await trust(client)
                first = await finished(client, await submit_run(client, json={"prompt": "first", "workspace_id": "files"}))
                (tmp_path / "hook.py").write_text('print("{}")\n# changed', encoding="utf-8")
                second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
                assert second["result"]["execution_status"] == "failed"
                assert second["result"]["stop_reason"] in {"permission_stale", "hook_definition_changed"}
                assert not (workspace / "result.txt").exists()
                assert not (workspace / "hook-count").exists()
    asyncio.run(scenario())


def test_restart_preserves_post_tool_receipt(tmp_path, monkeypatch):
    """等待后处理时重建宿主，批准后只运行脚本，原文件工具不重放。"""
    async def scenario():
        """用两个真实应用生命周期验证持久检查点；返回 None，保留原记录用于核对。"""
        from agent_harness.web.api import create_app
        app, model, workspace = hook_app(tmp_path, monkeypatch)
        manager = app.state.manager
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                await trust(client)
                first = await finished(client, await submit_run(client, json={"prompt": "first", "workspace_id": "files"}))
        (workspace / "result.txt").write_text("after-restart", encoding="utf-8")
        restarted = create_app(manager.web_config, manager.run_config, manager.runs_root, model_client=model)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://localhost") as client:
                second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
                assert second["result"]["execution_status"] == "completed", second
                assert (workspace / "result.txt").read_text() == "after-restart"
                assert (workspace / "hook-count").read_text() == "1"
    asyncio.run(scenario())


def test_strict_mode_separately_approves_hook_and_original_write(tmp_path, monkeypatch):
    """Hook 许可不能授权原文件写入；两次具体动作决定分别消费，脚本累计只执行一次。"""
    async def scenario():
        """通过真实队列推进两次审批和最终回答，返回 None，不绕过严格权限。"""
        app, _, workspace = hook_app(tmp_path, monkeypatch, "PreToolUse")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                await trust(client)
                first = await finished(client, await submit_run(client, json={"prompt": "first", "workspace_id": "files", "config": {"permission_mode": "strict"}}))
                second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
                assert second["result"]["stop_reason"] == "action_approval", second
                assert second["result"]["permissions"]["pending"]["name"] == "write_file"
                assert not (workspace / "result.txt").exists()
                third = await finished(client, await submit_followup(client, f"/api/runs/{second['run_id']}/followups", json=decide(second)))
                assert third["result"]["execution_status"] == "completed", third
                assert (workspace / "hook-count").read_text() == "1"
                assert (workspace / "result.txt").read_text() == "first"
    asyncio.run(scenario())


def test_queued_subagent_events_survive_multiple_approvals(tmp_path, monkeypatch):
    """子起止事件排队后逐次审批，第二项在首项等待期间持久保留，各执行一次。"""
    async def scenario():
        """通过父子真实执行与两轮决定消费队列；返回 None，历史起止事实不重放。"""
        from tests.subagents.test_subagents import report
        read = call("read_file", path="api.py")
        app, model, workspace = hook_app(tmp_path, monkeypatch, "SubagentStart", responses=[
            reply(call("delegate_readonly", title="核对", objective="读取api.py", max_model_turns=3, max_tool_calls=2)),
            reply(read), reply(text=report(ids=[read.call_id])), reply(text="完成")])
        policy = app.state.manager.run_config.hooks
        policy.rules.append(policy.rules[0].model_copy(update={"id": "finish", "event": "SubagentStop"}))
        (workspace / "api.py").write_text("value = 1", encoding="utf-8")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                await trust(client)
                current = await finished(client, await submit_run(client, json={"prompt": "first", "workspace_id": "files"}))
                for _ in range(2):
                    assert current["result"]["stop_reason"] == "action_approval", current
                    current = await finished(client, await submit_followup(client, f"/api/runs/{current['run_id']}/followups", json=decide(current)))
                assert current["result"]["execution_status"] == "completed", current
                assert (workspace / "hook-count").read_text() == "11"
                assert not current["result"]["hooks"]["queue"]
                assert len(model.requests) == 4
    asyncio.run(scenario())


def test_precompact_approval_continues_same_logical_summary(tmp_path, monkeypatch):
    """批准压缩前脚本后继续同一次逻辑摘要，不因宿主审批消息改变区域而重复要求批准。"""
    async def scenario():
        """真实模型适配器使用离线传输，脚本与摘要阶段真实执行；返回 None，不访问供应商网络。"""
        from agent_harness.web.api import create_app
        from agent_harness.web.config import WebConfig, WebWorkspace
        from tests.context.test_compression import _manifest, CompressionServer
        monkeypatch.setenv("HARNESS_TEST_MODEL_KEY", "offline-hooks")
        value = _manifest(tmp_path, max_input_bytes=15000, inline_tool_bytes=20000, keep_recent_groups=1)
        value.config.tools.append("run_command")
        value.config.permission_policy.command = "ask"
        script = tmp_path / "hook.py"
        script.write_text('from pathlib import Path\np=Path("compact-count")\np.write_text((p.read_text() if p.exists() else "")+"1")\nprint("{}")', encoding="utf-8")
        value.config.hooks = HookPolicy(rules=[HookRule(id="before", event="PreCompact", argv=[sys.executable, str(script)])])
        server = CompressionServer(observations=3)
        config = WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "copies", frontend_dist=tmp_path / "dist", workspaces=[WebWorkspace(id="files", label="摘要", path=value.task.workspace)])
        app = create_app(config, value.config, tmp_path / "records", transport=httpx.MockTransport(server.handle))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                await trust(client)
                first = await finished(client, await submit_run(client, json={"prompt": "first", "workspace_id": "files"}))
                assert first["result"]["stop_reason"] == "action_approval", first
                current = first
                approved = set()
                for _ in range(4):
                    if current["result"]["execution_status"] != "waiting_input":
                        break
                    pending = current["result"]["permissions"]["pending"]
                    key = pending["hook"]["origin"]["key"]
                    assert key not in approved, "同一次逻辑摘要不应再次要求批准"
                    approved.add(key)
                    current = await finished(client, await submit_followup(client, f"/api/runs/{current['run_id']}/followups", json=decide(current)))
                assert current["result"]["execution_status"] == "completed", current
                assert (value.task.workspace / "compact-count").read_text() == "1" * len(server.summaries)
                assert len(approved) == len(server.summaries)
    asyncio.run(scenario())


def test_exhausted_hook_budget_does_not_request_another_approval(tmp_path, monkeypatch):
    """首个 Hook 已耗尽限额时，下一动作直接停止，不为不能执行的脚本继续请求批准。"""
    async def scenario():
        """通过一次真实审批耗尽共享限额，返回 None；第二个文件始终不存在。"""
        app, _, workspace = hook_app(tmp_path, monkeypatch, "PreToolUse", responses=[reply(call("write_file", path="first.txt", content="one")), reply(call("write_file", path="second.txt", content="two"))])
        app.state.manager.run_config.hooks.max_calls = 1
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                await trust(client)
                first = await finished(client, await submit_run(client, json={"prompt": "first", "workspace_id": "files"}))
                second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
                assert second["result"]["stop_reason"] == "hook_budget_exhausted", second
                assert second["result"]["permissions"]["pending"] is None
                assert (workspace / "first.txt").exists() and not (workspace / "second.txt").exists()
                assert (workspace / "hook-count").read_text() == "1"
    asyncio.run(scenario())
