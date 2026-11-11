"""通过真实HTTP、队列、Runner和本机文件验证自动化闭环，不联网调用模型。"""

import asyncio
from datetime import datetime, UTC, timedelta
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx

from tests.helpers.models import configured_run
from agent_harness.permissions.models import PermissionPolicy
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from agent_harness.automations.models import Schedule, next_time
from tests.web.test_planning_api import ScriptModel, call, reply
from tests.web.test_jobs_api import wait_job


def application(root: Path, responses: list, *, grants: bool = False):
    """在root创建普通项目与真实服务；responses为脚本模型响应，grants明确宿主预授权开关。"""
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    model = ScriptModel(responses)
    config = WebConfig(project_dir=root, worktrees_root=root / "trees", frontend_dist=root / "dist",
                       workspaces=[WebWorkspace(id="project", label="项目", path=workspace)])
    app = create_app(config, configured_run(tools=["write_file", "read_file", "run_command"], permission_policy=PermissionPolicy(automation_grants=grants)), root / "runs", model_client=model)
    app.state.manager._key_provider = lambda config: "offline-automation"  # 离线测试不访问用户密钥。
    return app, workspace, model


def definition(**schedule) -> dict:
    """返回每日自动化完整公开定义；schedule覆盖触发类型，非Cron需显式cron=None。"""
    return {"name": "项目巡检", "task": {"workspace_id": "project", "prompt": "检查项目并报告结果"},
            "schedule": {"kind": "cron", "cron": "0 9 * * mon-fri", **schedule}}


async def action(client, rule: dict, name: str, request_id: str | None = None):
    """通过client对rule执行name，request_id=None生成新编号；返回响应供成功或拒绝断言。"""
    return await client.post(f"/api/automations/{rule['id']}/actions/{name}", json={"revision": rule["revision"], "request_id": request_id or str(uuid4())})


async def settled(client, app, rule: dict) -> dict:
    """等待rule最新触发产生的真实作业终态并对账，返回最新触发；没有作业立即断言失败。"""
    history = (await client.get(f"/api/automations/{rule['id']}/history")).json()["items"]
    assert history and history[0]["job_id"], history
    await wait_job(client, history[0]["job_id"])
    await app.state.manager.automations.tick()
    return (await client.get(f"/api/automations/{rule['id']}/history")).json()["items"][0]


def test_manual_run_persistent_origin_notifications_and_idempotence(tmp_path: Path):
    """页面保存后保持暂停；试跑真实写文件且重复投递只生成一个运行和通知。"""
    async def scenario():
        """驱动规则创建、真实产物、历史、已读与重复请求，返回None。"""
        app, workspace, model = application(tmp_path, [reply(call("write_file", path="report.txt", content="巡检完成")), reply(text="完成")])
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            request = definition()
            request["task"]["config"] = {"provider": "custom-secondary", "requested_model": "fixture-review"}
            created = await client.post("/api/automations", json=request)
            assert created.status_code == 201, created.text
            rule = created.json()
            assert not rule["enabled"] and len(rule["next_times"]) == 5 and not model.requests
            identifier = str(uuid4())
            response = await action(client, rule, "run", identifier)
            assert response.status_code == 200, response.text
            fire = await settled(client, app, rule)
            assert fire["status"] == "completed" and fire["validation_status"] == "unverified"
            assert workspace.joinpath("report.txt").read_text(encoding="utf-8") == "巡检完成"
            run = (await client.get("/api/runs/" + fire["run_id"])).json()
            assert run["automation"]["rule_id"] == rule["id"]
            assert run["config"]["provider"] == "custom-secondary" and run["config"]["requested_model"] == "fixture-review"
            assert (await action(client, rule, "run", identifier)).status_code == 200
            assert len((await client.get(f"/api/automations/{rule['id']}/history")).json()["items"]) == 1
            messages = (await client.get("/api/automation-notifications")).json()["items"]
            assert len(messages) == 1 and not messages[0]["read"]
            assert (await client.post("/api/automation-notifications/" + messages[0]["id"] + "/read")).json()["read"]
    asyncio.run(scenario())


def test_exact_command_grant_and_revocation(tmp_path: Path):
    """宿主显式开启后精确命令可自动执行；撤销后旧授权不能用于后续运行。"""
    async def scenario():
        """通过HTTP审核并授权真实Python命令，核对文件副作用及授权撤销后的审批等待。"""
        argv = [sys.executable, "-c", "from pathlib import Path; Path('command.txt').write_text('ok')"]
        app, workspace, _ = application(tmp_path, [reply(call("run_command", argv=argv)), reply(text="完成"), reply(call("run_command", argv=argv))], grants=True)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            rule = (await client.post("/api/automations", json=definition())).json()
            review = (await client.get(f"/api/automations/{rule['id']}/review")).json()
            response = await client.post(f"/api/automations/{rule['id']}/grant", json={"revision": rule["revision"], "fingerprint": review["fingerprint"], "commands": [{"argv": argv}]})
            assert response.status_code == 200, response.text
            assert (await action(client, rule, "run")).status_code == 200
            fire = await settled(client, app, rule)
            assert fire["status"] == "completed", fire
            assert workspace.joinpath("command.txt").read_text() == "ok"
            assert (await action(client, rule, "revoke")).status_code == 200
            workspace.joinpath("command.txt").unlink()
            assert (await action(client, rule, "run")).status_code == 200
            fire = await settled(client, app, rule)
            assert fire["status"] == "waiting_input", fire
            assert not workspace.joinpath("command.txt").exists()
    asyncio.run(scenario())


def test_schedule_persistence_misfire_pause_and_generation(tmp_path: Path):
    """到期触发只接纳一次，长时间漏跑默认跳过，暂停及编辑使未开始请求失效。"""
    async def scenario():
        """注入调度时间而不真实等待；重启读取规则及旧执行，核对没有重复模型调用。"""
        app, _, model = application(tmp_path, [reply(text="正常")])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            rule = (await client.post("/api/automations", json=definition(kind="interval", cron=None, interval_minutes=1))).json()
            assert (await action(client, rule, "enable")).status_code == 200
            service = app.state.manager.automations
            due = datetime.fromisoformat(service.read(rule["id"])["next_at"])
            await service.tick(due)
            fire = await settled(client, app, rule)
            await service.tick(due)
            assert len(model.requests) == 1 and fire["status"] == "completed"
            await service.tick(due + timedelta(hours=2))
            history = (await client.get(f"/api/automations/{rule['id']}/history")).json()["items"]
            assert history[0]["status"] == "skipped" and "漏跑" in history[0]["reason"]
            assert (await action(client, rule, "pause")).status_code == 200
            assert not service.read(rule["id"])["enabled"]
            edit = await client.put(f"/api/automations/{rule['id']}", json={"revision": rule["revision"], "definition": definition()})
            assert edit.status_code == 200, edit.text
            assert edit.json()["revision"] == 2 and len(edit.json()["history_versions"]) == 1
            assert (await action(client, rule, "enable")).status_code == 409
        await app.state.manager.shutdown()
        restarted, _, new_model = application(tmp_path, [])
        assert restarted.state.manager.automations.list()["items"][0]["revision"] == 2
        await restarted.state.manager.automations.tick()
        assert not new_model.requests
        await restarted.state.manager.shutdown()
    asyncio.run(scenario())


def test_event_triggers_only_normal_runs_and_deduplicates(tmp_path: Path):
    """普通运行可信终态触发分析；自动化自身完成不产生循环，重复终态事件只接纳一次。"""
    async def scenario():
        """完成普通任务后消费持久事件并观察新会话，重复tick不能新增模型调用。"""
        app, _, model = application(tmp_path, [reply(text="普通任务完成"), reply(text="分析完成")])
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            rule = (await client.post("/api/automations", json=definition(kind="event", cron=None, event="completed"))).json()
            assert (await action(client, rule, "enable")).status_code == 200
            job = (await client.post("/api/jobs", json={"request_id": str(uuid4()), "create": {"workspace_id": "project", "prompt": "普通运行"}})).json()
            await wait_job(client, job["id"])
            await app.state.manager.automations.tick()
            fire = await settled(client, app, rule)
            assert fire["source_run_id"] == job["run_id"] and fire["run_id"] != job["run_id"]
            await app.state.manager.automations.tick()
            await app.state.manager.automations.tick()
            assert len(model.requests) == 2
    asyncio.run(scenario())


def test_grant_disabled_and_review_change_rejected(tmp_path: Path):
    """宿主关闭不能从页面放权；审核后项目入口变更不能用旧指纹批准。"""
    async def scenario():
        """先验证宿主上限，再改变AGENTS内容，核对拒绝且没有任务启动。"""
        app, workspace, model = application(tmp_path, [])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            rule = (await client.post("/api/automations", json=definition())).json()
            review = (await client.get(f"/api/automations/{rule['id']}/review")).json()
            denied = await client.post(f"/api/automations/{rule['id']}/grant", json={"revision": 1, "fingerprint": review["fingerprint"]})
            assert denied.status_code == 409 and denied.json()["error"]["code"] == "automation_grants_disabled"
            workspace.joinpath("AGENTS.md").write_text("新增约束", encoding="utf-8")
            changed = await action(client, rule, "enable")
            assert changed.status_code == 409 and not model.requests
        await app.state.manager.shutdown()
    asyncio.run(scenario())


def test_dst_cron_skips_nonexistent_time_and_preserves_timezone():
    """春季不存在的02:30跳过，普通日期正确返回UTC；Windows通过tzdata取得IANA规则。"""
    schedule = Schedule(cron="30 2 * * *", timezone="America/New_York")
    after = datetime(2026, 3, 7, 8, tzinfo=UTC)
    assert next_time(schedule, after, after) == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


def test_dst_fall_uses_first_occurrence_only():
    """秋季重复01:30只计划第一次，已经过去第一次时下一次直接进入翌日。"""
    schedule = Schedule(cron="30 1 * * *", timezone="America/New_York")
    before = datetime(2026, 11, 1, 5, tzinfo=UTC)
    assert next_time(schedule, before, before) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    after = datetime(2026, 11, 1, 5, 31, tzinfo=UTC)
    assert next_time(schedule, after, before) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)
