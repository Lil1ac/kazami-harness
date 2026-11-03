"""权限判定和真实HTTP、队列、文件、子进程联动；不使用外部模型或网络。"""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_harness.permissions.models import PermissionPolicy
from agent_harness.permissions.policy import evaluate
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from agent_harness.web.config import WebVerifier
from agent_harness.permissions.store import PermissionStore
from tests.web.job_helpers import submit_run, submit_followup
from tests.web.test_planning_api import ScriptModel, call, reply, finished


def setup_app(tmp_path: Path, monkeypatch, responses: list, *, config: RunConfig | None = None):
    """为独立 tmp_path、离线 responses 和可选宿主 config 建应用；返回应用、模型、工作区，不联网。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-permissions")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    web = WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=tmp_path / "dist",
                    workspaces=[WebWorkspace(id="files", label="权限案例", path=workspace)])
    model = ScriptModel(responses)
    app = create_app(web, config or RunConfig(tools=["read_file", "write_file", "edit_file", "run_command", "search_files"]), tmp_path / "records", model_client=model)
    return app, model, workspace


def decide(view: dict, action: str = "approve") -> dict:
    """从 view 的固定待审批动作构造 action 决定，返回一次幂等请求，不发送网络。"""
    pending = view["result"]["permissions"]["pending"]
    return {"request_id": str(uuid4()), "prompt": "处理本次动作", "action_decision": {
        "action_id": pending["id"], "fingerprint": pending["fingerprint"], "action": action}}


@pytest.mark.parametrize("mode,name,host,expected", [
    ("workspace_edit", "write_file", "allow", "allow"), ("strict", "write_file", "allow", "ask"),
    ("research", "run_command", "allow", "deny"), ("strict", "run_command", "deny", "deny"),
    ("workspace_edit", "run_command", "ask", "ask"), ("workspace_edit", "unknown", "allow", "deny"),
])
def test_policy_intersection(mode, name, host, expected) -> None:
    """验证 mode、name 与宿主 host 组合的 expected 判定；无文件或执行副作用。"""
    policy = PermissionPolicy(write=host, command=host)
    assert evaluate(policy, mode, name) == expected
    if name in {"write_file", "run_command"}:
        assert evaluate(policy, mode, name, planning=True) == "deny"


def test_command_approval_executes_frozen_action_once_and_preserves_budget(tmp_path, monkeypatch) -> None:
    """真实命令等待时零副作用；批准同一动作一次，历史固定、同批后续跳过且预算延续。"""
    program = "from pathlib import Path; p=Path('counter.txt'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
    app, model, workspace = setup_app(tmp_path, monkeypatch, [
        reply(call("run_command", argv=[sys.executable, "-c", program]), call("write_file", path="skipped.txt", content="no")),
        reply(text="执行完成"),
    ])

    async def scenario():
        """通过正式HTTP作业接口等待、批准和重复批准；返回None，副作用仅在测试工作区。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "增加计数"}))
                assert first["result"]["stop_reason"] == "action_approval"
                assert not (workspace / "counter.txt").exists()
                events = (await client.get(f"/api/runs/{first['run_id']}/events")).json()["items"]
                assert not any(e["type"] == "tool_started" for e in events)
                before = (tmp_path / "records" / first["run_id"] / "events.jsonl").read_bytes()
                assert len(model.requests) == 1
                rejected = await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json={"request_id": str(uuid4()), "prompt": "批准"})
                assert rejected.status_code == 409
                body = decide(first)
                second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=body))
                assert second["result"]["execution_status"] == "completed", second
                assert (workspace / "counter.txt").read_text() == "1"
                assert not (workspace / "skipped.txt").exists()
                assert len(model.requests) == 2
                assert second["result"]["permissions"]["spent_model_turns"] == first["result"]["model_turns"]
                assert second["result"]["tool_calls"] == 0
                repeat = await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=body)
                assert repeat.status_code == 202 and repeat.json()["run_id"] == second["run_id"]
                assert (workspace / "counter.txt").read_text() == "1"
                assert (tmp_path / "records" / first["run_id"] / "events.jsonl").read_bytes() == before
                actions = (await client.get(f"/api/runs/{second['run_id']}/permissions")).json()
                assert actions["items"][0]["status"] == "executed"
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["reject", "cancel"])
def test_rejection_and_cancel_have_no_model_or_file_side_effect(tmp_path, monkeypatch, action) -> None:
    """严格模式的 action 决定只保存事实，不调用模型、写目标或启动测试。"""
    app, model, workspace = setup_app(tmp_path, monkeypatch, [reply(call("write_file", path="target.txt", content="pending"))])

    async def scenario():
        """提交严格审批任务并处理一次决定；返回None，仅写测试记录目录。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "写文件", "config": {"permission_mode": "strict"}}))
                second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first, action)))
                assert second["result"]["execution_status"] == "cancelled"
                assert len(model.requests) == 1 and not (workspace / "target.txt").exists()
    asyncio.run(scenario())


def test_changed_target_invalidates_approval(tmp_path, monkeypatch) -> None:
    """等待期间目标被外部写入后，批准不会覆盖现有字节或请求新模型。"""
    app, model, workspace = setup_app(tmp_path, monkeypatch, [reply(call("write_file", path="target.txt", content="agent", overwrite=True))])

    async def scenario():
        """在等待和批准之间模拟编辑器写入；返回None，核对实际文件与错误状态。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "写文件", "config": {"permission_mode": "strict"}}))
                (workspace / "target.txt").write_text("external", encoding="utf-8")
                second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
                assert second["result"]["stop_reason"] == "permission_stale", second
                assert (workspace / "target.txt").read_text() == "external" and len(model.requests) == 1
    asyncio.run(scenario())


def test_verification_approval_preserves_repair_limit_and_finishes_without_model(tmp_path, monkeypatch) -> None:
    """真实公开测试逐次审批；失败后修复计数延续，最终批准检查不新增模型调用。"""
    app, model, workspace = setup_app(tmp_path, monkeypatch, [
        reply(call("write_file", path="answer.txt", content="bad")), reply(text="等待检查"),
        reply(call("write_file", path="answer.txt", content="good", overwrite=True)), reply(text="修复完成"),
    ])
    check = tmp_path / "checks.toml"
    check.write_text('kind="project_tests"\nargv=' + json.dumps([sys.executable, "-c", "import os; from pathlib import Path; assert (Path(os.environ['HARNESS_WORKSPACE'])/'answer.txt').read_text()=='good'"]) + '\n', encoding="utf-8")
    app.state.manager.web_config.verifiers = [WebVerifier(id="checks", label="公开测试", path=check)]

    async def scenario():
        """通过真实HTTP批准失败检查和修复后检查，返回None；每次审批前验证检查没有启动。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "verifier_id": "checks", "prompt": "生成通过测试的文件", "config": {"auto_repair": True}}))
                assert first["result"]["permissions"]["pending"]["name"] == "verification"
                assert first["result"]["verification_attempts"] == []
                second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
                assert second["result"]["execution_status"] == "waiting_input", second
                assert second["result"]["repair_attempts"] == 1
                assert len(second["result"]["verification_attempts"]) == 1
                third = await finished(client, await submit_followup(client, f"/api/runs/{second['run_id']}/followups", json=decide(second)))
                assert third["result"]["validation_status"] == "passed", third
                assert third["result"]["repair_attempts"] == 1
                assert third["result"]["model_turns"] == 0 and len(model.requests) == 4
                assert third["result"]["permissions"]["spent_model_turns"] == 4
                assert (workspace / "answer.txt").read_text() == "good"
    asyncio.run(scenario())


def test_expired_approval_rejects_and_still_allows_cancellation(tmp_path, monkeypatch) -> None:
    """审批到期时批准被拒绝，取消仍能结束等待；GET不修改原始审批事实。"""
    app, model, workspace = setup_app(tmp_path, monkeypatch, [reply(call("write_file", path="expired.txt", content="no"))])

    async def scenario():
        """用受控时钟模拟过期而不睡眠，返回None，核对HTTP原因与零副作用。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "写文件", "config": {"permission_mode": "strict"}}))
                # 仅替换审批核验时钟，保留真实不可变动作及指纹。
                monkeypatch.setattr("agent_harness.permissions.store.utc_seconds", lambda: first["result"]["permissions"]["pending"]["expires_at"] + 1)
                rejected = await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first))
                assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "permission_expired"
                final = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first, "cancel")))
                assert final["result"]["execution_status"] == "cancelled" and not (workspace / "expired.txt").exists()
                assert len(model.requests) == 1
    asyncio.run(scenario())


def test_read_and_write_scopes_filter_recursive_results(tmp_path, monkeypatch) -> None:
    """目录范围过滤递归搜索和读取，并阻止越权写入；显式审批不能扩大宿主目录。"""
    config = RunConfig(tools=["read_file", "search_files", "write_file"], permission_policy=PermissionPolicy(read_roots=["public"], write_roots=["public"]))
    app, model, workspace = setup_app(tmp_path, monkeypatch, [reply(
        call("search_files", query="secret", directory="."), call("read_file", path="private/key.txt"),
        call("write_file", path="private/new.txt", content="no")), reply(text="检查结束")], config=config)
    (workspace / "public").mkdir()
    (workspace / "private").mkdir()
    (workspace / "public" / "visible.txt").write_text("secret visible", encoding="utf-8")
    (workspace / "private" / "key.txt").write_text("secret hidden", encoding="utf-8")

    async def scenario():
        """通过真实工具循环读取授权内容并核对后端回执，返回None，不向模型泄露受限正文。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                view = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "搜索和核对目录"}))
                assert view["result"]["execution_status"] == "completed"
                assert not (workspace / "private" / "new.txt").exists()
                request = json.dumps(model.requests[-1], ensure_ascii=False)
                assert "secret visible" in request and "secret hidden" not in request
                assert "permission_path_denied" in request
    asyncio.run(scenario())


def test_pending_survives_restart_without_automatic_execution(tmp_path, monkeypatch) -> None:
    """原服务退出后等待动作仍可审阅；重启与GET零模型、零工具，明确批准才执行一次。"""
    app, first_model, workspace = setup_app(tmp_path, monkeypatch, [reply(call("write_file", path="restart.txt", content="saved"))])

    async def scenario():
        """关闭首个真实生命周期后打开相同记录根，返回None，核对跨服务审批与固定旧快照。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "写结果", "config": {"permission_mode": "strict"}}))
        restarted, model, _ = setup_app(tmp_path, monkeypatch, [reply(text="接续结束")])
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://localhost") as client:
                original = (await client.get(f"/api/runs/{first['run_id']}")).json()
                assert original["result"]["permissions"]["pending"] == first["result"]["permissions"]["pending"]
                assert len(model.requests) == 0 and not (workspace / "restart.txt").exists()
                final = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
                assert final["result"]["execution_status"] == "completed", final
                assert (workspace / "restart.txt").read_text() == "saved" and len(model.requests) == 1
        assert len(first_model.requests) == 1
    asyncio.run(scenario())


def test_concurrent_approve_and_reject_have_one_winner(tmp_path, monkeypatch) -> None:
    """并发标签页提交相反决定时只有一个被接纳，不能启动两次原动作。"""
    app, model, workspace = setup_app(tmp_path, monkeypatch, [reply(call("write_file", path="race.txt", content="once")), reply(text="结束")])

    async def scenario():
        """同时投递两个正式请求，返回None，核对事务竞争及真实写入次数的边界。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "写结果", "config": {"permission_mode": "strict"}}))
                responses = await asyncio.gather(submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)), submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first, "reject")))
                assert sorted(item.status_code for item in responses) == [202, 409]
                await finished(client, next(item for item in responses if item.status_code == 202))
                assert len(model.requests) <= 2
                actions = PermissionStore(tmp_path / "records").list(first["session_id"])
                assert actions["total"] == 1 and actions["items"][0]["status"] in {"executed", "rejected"}
                assert (workspace / "race.txt").exists() == (actions["items"][0]["status"] == "executed")
    asyncio.run(scenario())


def test_cancel_queued_approval_revokes_grant_and_allows_new_task(tmp_path, monkeypatch) -> None:
    """批准后仍排队时取消会撤销许可，恢复队列不执行旧动作，普通新要求可以继续。"""
    app, model, workspace = setup_app(tmp_path, monkeypatch, [reply(call("write_file", path="cancelled.txt", content="no")), reply(text="新任务结束")])

    async def scenario():
        """手动暂停真实队列并取消已批准作业，返回None，不依赖模型或进程启动时序。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "写文件", "config": {"permission_mode": "strict"}}))
                app.state.manager.jobs.paused = True
                body = decide(first)
                response = await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=body)
                assert response.status_code == 202 and response.json()["status"] == "queued"
                cancelled = await client.post(f"/api/jobs/{response.json()['id']}/cancel")
                assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
                assert PermissionStore(tmp_path / "records").get(body["action_decision"]["action_id"])["status"] == "cancelled"
                await client.post("/api/queue/resume")
                final = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json={"request_id": str(uuid4()), "prompt": "开始新的只读任务"}))
                assert final["result"]["execution_status"] == "completed"
                assert not (workspace / "cancelled.txt").exists() and len(model.requests) == 2
    asyncio.run(scenario())


def test_unknown_action_after_storage_failure_is_not_replayed(tmp_path, monkeypatch) -> None:
    """原动作已认领但缺少回执时，重启标记unknown并暂停队列，不尝试重放。"""
    from agent_harness.execution.processes import ProcessRunner
    from agent_harness.storage.runs import StorageError
    app, model, workspace = setup_app(tmp_path, monkeypatch, [reply(call("run_command", argv=[sys.executable, "-c", "from pathlib import Path; Path('unknown.txt').write_text('no')"]))])

    async def fail_process(self, *args, **kwargs):
        """模拟已记录真实开始后存储故障；args/kwargs不执行，抛StorageError以保留结果不明边界。"""
        raise StorageError("模拟记录故障")

    async def scenario():
        """注入一个执行窗口故障后重开相同服务，返回None，核对恢复只读取日志。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "运行命令"}))
                body = decide(first)
                with monkeypatch.context() as patch:
                    patch.setattr(ProcessRunner, "run", fail_process)
                    second = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=body))
                assert second["interrupted"]
                assert (await client.get("/api/jobs")).json()["paused"]
        restarted, new_model, _ = setup_app(tmp_path, monkeypatch, [])
        async with restarted.router.lifespan_context(restarted):
            store = PermissionStore(tmp_path / "records")
            assert store.get(body["action_decision"]["action_id"])["status"] == "unknown"
            assert restarted.state.manager.jobs.paused
            assert not (workspace / "unknown.txt").exists() and len(new_model.requests) == 0
        assert len(model.requests) == 1
    asyncio.run(scenario())


def test_host_restriction_changed_after_approval_request_invalidates_action(tmp_path, monkeypatch) -> None:
    """宿主收紧权限后不能沿用旧批准，网页也不能提交宿主策略来重新放宽。"""
    app, model, workspace = setup_app(tmp_path, monkeypatch, [reply(call("write_file", path="host.txt", content="no"))])

    async def scenario():
        """等待期间收紧真实服务配置，再尝试网页越权和正式批准，返回None，核对零项目副作用。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "写文件", "config": {"permission_mode": "strict"}}))
                forbidden = await submit_run(client, json={"workspace_id": "files", "prompt": "越权", "config": {"permission_policy": {"write": "allow"}}})
                assert forbidden.status_code == 422
                app.state.manager.run_config.permission_policy.write = "deny"
                final = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
                assert final["result"]["stop_reason"] == "permission_stale"
                assert not (workspace / "host.txt").exists() and len(model.requests) == 1
                assert PermissionStore(tmp_path / "records").list(first["session_id"])["items"][0]["status"] == "invalidated"
    asyncio.run(scenario())


def test_approved_original_call_uses_no_extra_budget_and_no_new_model_turn(tmp_path, monkeypatch) -> None:
    """生成动作已用完模型额度时仍可执行已接纳原调用，之后明确耗尽，不因审批获得额外模型轮次。"""
    from agent_harness.schemas import RunLimits
    from dataclasses import replace
    config = RunConfig(tools=["write_file"], permission_mode="strict", limits=RunLimits(max_model_turns=1, max_tool_calls=1))
    response = replace(reply(call("write_file", path="bounded.txt", content="once")), usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
    app, model, workspace = setup_app(tmp_path, monkeypatch, [response], config=config)

    async def scenario():
        """以一轮一工具预算批准原动作，返回None，核对真实文件、累计额度和已知Token。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "有界写入"}))
                final = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=decide(first)))
                assert final["result"]["execution_status"] == "limit_exceeded"
                assert final["result"]["model_turns"] == final["result"]["tool_calls"] == 0
                assert final["result"]["permissions"]["spent_model_turns"] == final["result"]["permissions"]["spent_tool_calls"] == 1
                assert final["result"]["permissions"]["spent_usage"]["total_tokens"] == 5
                assert (workspace / "bounded.txt").read_text() == "once" and len(model.requests) == 1
    asyncio.run(scenario())


def test_expired_queued_grant_fails_without_needs_review_or_replay(tmp_path, monkeypatch) -> None:
    """排队期间过期且尚未创建Run时明确失效，不伪造结果不明，也不会阻塞后续新任务。"""
    app, model, workspace = setup_app(tmp_path, monkeypatch, [reply(call("write_file", path="queue-expired.txt", content="no")), reply(text="后续任务结束")])

    async def scenario():
        """冻结队列后批准，再推进审批时钟并恢复；返回None，核对未启动作业的明确失败。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "排队写入", "config": {"permission_mode": "strict"}}))
                app.state.manager.jobs.paused = True
                body = decide(first)
                queued = await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json=body)
                # 模拟排队消耗有效期，不修改固定动作或其指纹。
                with monkeypatch.context() as patch:
                    patch.setattr("agent_harness.permissions.store.utc_seconds", lambda: first["result"]["permissions"]["pending"]["expires_at"] + 1)
                    await client.post("/api/queue/resume")
                    await app.state.manager.jobs.worker
                job = (await client.get(f"/api/jobs/{queued.json()['id']}")).json()
                assert job["status"] == "failed" and job["error_code"] == "permission_expired"
                assert not (tmp_path / "records" / job["run_id"] / "run.json").exists()
                assert not app.state.manager.jobs.paused and not (workspace / "queue-expired.txt").exists()
                final = await finished(client, await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json={"request_id": str(uuid4()), "prompt": "开始新任务"}))
                assert final["result"]["execution_status"] == "completed" and len(model.requests) == 2
    asyncio.run(scenario())


def test_child_inherits_parent_directory_limits_and_has_no_write_authority(tmp_path) -> None:
    """子Agent不能读取父级目录范围外的正文，报告只引用授权内容，其有效写权限始终拒绝。"""
    from tests.subagents.test_subagents import setup_run, delegate, report
    from tests.runtime.test_runner import response, call as provider_call, run_script
    from agent_harness.subagents.service import read_child
    value = setup_run(tmp_path)
    (tmp_path / "public").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "public" / "api.py").write_text("public value=42", encoding="utf-8")
    (tmp_path / "private" / "secret.txt").write_text("PRIVATE_DO_NOT_INJECT", encoding="utf-8")
    value.config.permission_policy = PermissionPolicy(read_roots=["public"], write_roots=["public"])
    outcome, requests, _ = run_script(value, tmp_path / "records", [response(delegate()),
        response(provider_call("denied", "read_file", '{"path":"private/secret.txt"}')),
        response(provider_call("allowed", "read_file", '{"path":"public/api.py"}')),
        response(content=report("授权文件返回42", ids=["allowed"])), response(content="研究结束")])
    assert "PRIVATE_DO_NOT_INJECT" not in json.dumps(requests, ensure_ascii=False)
    assert "permission_path_denied" in json.dumps(requests, ensure_ascii=False)
    child = read_child(tmp_path / "records", value.run_id, outcome.result.subagents[0].run_id)
    assert child.result.permissions.effective["write_file"] == "deny"
    assert child.manifest.config.permission_policy.read_roots == ["public"]


def test_partial_host_config_override_preserves_denials(tmp_path) -> None:
    """覆盖单个目录配置不能意外清除宿主命令拒绝规则，返回值保留双方明确配置。"""
    from agent_harness.config import load_config
    profile = tmp_path / "host.toml"
    profile.write_text('[permission_policy]\ncommand="deny"\nverification="deny"\n', encoding="utf-8")
    config = load_config(profile, {"permission_policy": {"write_roots": ["public"]}})
    assert config.permission_policy.command == config.permission_policy.verification == "deny"
    assert config.permission_policy.write_roots == ["public"]


def test_host_read_denial_applies_to_automatic_project_instructions(tmp_path, monkeypatch) -> None:
    """宿主禁止项目读取时，自动AGENTS加载也不能泄露正文；必需规则不可读则零模型停止。"""
    config = RunConfig(permission_policy=PermissionPolicy(read="deny"))
    app, model, workspace = setup_app(tmp_path, monkeypatch, [reply(text="不应调用")], config=config)
    (workspace / "AGENTS.md").write_text("PRIVATE_PROJECT_RULE_DO_NOT_SEND", encoding="utf-8")

    async def scenario():
        """通过真实任务入口验证上下文加载限制，返回None，仅检查本地事件和调用次数。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                final = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "研究项目"}))
                assert final["result"]["execution_status"] == "failed"
                assert len(model.requests) == 0
                events = (tmp_path / "records" / final["run_id"] / "events.jsonl").read_text(encoding="utf-8")
                assert "PRIVATE_PROJECT_RULE_DO_NOT_SEND" not in events
    asyncio.run(scenario())


def test_purge_removes_action_content_and_invalidates_old_decision(tmp_path, monkeypatch) -> None:
    """彻底删除会话同时清理动作正文与许可，项目文件不变，旧审批不能恢复执行。"""
    app, model, workspace = setup_app(tmp_path, monkeypatch, [reply(call("write_file", path="purged.txt", content="PRIVATE_PENDING_BODY"))])

    async def scenario():
        """通过会话管理的正式确认接口删除等待记录，返回None，验证数据库和API访问边界。"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                first = await finished(client, await submit_run(client, json={"workspace_id": "files", "prompt": "生成文件", "config": {"permission_mode": "strict"}}))
                session = first["session_id"]
                metadata = (await client.get(f"/api/conversations/{session}")).json()
                trashed = await client.patch(f"/api/conversations/{session}", json={"request_id": str(uuid4()), "expected_revision": metadata["revision"], "action": "trash"})
                assert trashed.status_code == 200
                purged = await client.request("DELETE", f"/api/conversations/{session}", json={"request_id": str(uuid4()), "expected_revision": trashed.json()["revision"], "confirm": True})
                assert purged.status_code == 200, purged.text
                assert PermissionStore(tmp_path / "records").list(session)["total"] == 0
                assert (await client.get(f"/api/runs/{first['run_id']}/permissions")).status_code == 410
                events = (await client.get("/api/work-events")).json()
                assert "PRIVATE_PENDING_BODY" not in json.dumps(events)
                assert not (workspace / "purged.txt").exists() and len(model.requests) == 1
    asyncio.run(scenario())
