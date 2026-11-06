"""自动化的审批接续、排队暂停、合并、漏跑、重启和精确授权边界。"""

import asyncio
from datetime import datetime, UTC, timedelta
from pathlib import Path
import sys
import subprocess
from uuid import uuid4, UUID

import httpx

from tests.web.test_automations_api import application, definition, action, settled
from tests.web.test_jobs_api import wait_job
from tests.web.test_planning_api import call, reply
from tests.permissions.test_permissions import decide
from tests.web.job_helpers import submit_followup


def test_approval_resume_same_fire_and_budget(tmp_path: Path):
    """未授权命令等待人工批准，接续仍属于原触发且沿用预算，不重放已完成工具。"""
    async def scenario():
        """通过真实审批请求执行固定命令，核对文件、模型轮次及唯一触发身份。"""
        argv = [sys.executable, "-c", "from pathlib import Path; Path('approved.txt').write_text('once')"]
        app, workspace, _ = application(tmp_path, [reply(call("run_command", argv=argv)), reply(text="完成")])
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            rule = (await client.post("/api/automations", json=definition())).json()
            await action(client, rule, "run")
            original = await settled(client, app, rule)
            assert original["status"] == "waiting_input" and not workspace.joinpath("approved.txt").exists()
            view = (await client.get("/api/runs/" + original["run_id"])).json()
            response = await submit_followup(client, "/api/runs/" + original["run_id"] + "/messages", json=decide(view))
            assert response.status_code == 202, response.text
            resumed = response.json()
            await app.state.manager.wait_idle(resumed["run_id"])
            for _ in range(100):
                await app.state.manager.automations.tick()
                history = (await client.get(f"/api/automations/{rule['id']}/history")).json()["items"]
                if history[0]["status"] == "completed":
                    break
                await asyncio.sleep(.02)
            assert len(history) == 1 and history[0]["id"] == original["id"] and history[0]["status"] == "completed"
            assert workspace.joinpath("approved.txt").read_text() == "once"
            final = (await client.get("/api/runs/" + history[0]["run_id"])).json()
            assert final["automation"]["fire_id"] == original["id"]
            assert final["result"]["model_turns"] + final["result"]["permissions"]["spent_model_turns"] == 2
    asyncio.run(scenario())


def test_pause_cancels_unstarted_and_stop_cancels_waiting(tmp_path: Path):
    """暂停撤销未开始请求；停止已等待审批的任务撤销动作，不伪造旧运行已执行。"""
    async def scenario():
        """分别制造暂停队列与待审批两种状态，核对模型未调用及固定动作取消。"""
        argv = [sys.executable, "-c", "print('blocked')"]
        app, _, model = application(tmp_path, [reply(call("run_command", argv=argv))])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            rule = (await client.post("/api/automations", json=definition())).json()
            app.state.manager.jobs.paused = True
            await action(client, rule, "run")
            fire = (await client.get(f"/api/automations/{rule['id']}/history")).json()["items"][0]
            assert fire["status"] == "queued" and not model.requests
            await action(client, rule, "pause")
            assert app.state.manager.jobs.get(fire["job_id"])["status"] == "cancelled"
            app.state.manager.jobs.resume()
            await action(client, rule, "run")
            fire = await settled(client, app, rule)
            assert fire["status"] == "waiting_input"
            assert (await action(client, rule, "stop")).status_code == 200
            fire = (await client.get(f"/api/automations/{rule['id']}/history")).json()["items"][0]
            assert fire["status"] == "cancelled"
            old = (await client.get("/api/runs/" + fire["run_id"])).json()
            assert old["result"]["execution_status"] == "waiting_input"
        await app.state.manager.shutdown()
    asyncio.run(scenario())


def test_coalescing_waits_until_current_finishes_and_keeps_latest(tmp_path: Path):
    """同规则活动期间保留一个合并触发，较早合并项记录跳过；完成后才开始最近一次。"""
    async def scenario():
        """使用真实队列暂停制造重叠，释放后核对恰好两次模型执行和完整历史。"""
        app, _, model = application(tmp_path, [reply(text="第一次完成"), reply(text="最近一次完成")])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            body = definition()
            body["overlap"] = "coalesce"
            rule = (await client.post("/api/automations", json=body)).json()
            await action(client, rule, "enable")
            app.state.manager.jobs.paused = True
            await action(client, rule, "run")
            first = (await client.get(f"/api/automations/{rule['id']}/history")).json()["items"][0]
            await action(client, rule, "run")
            await action(client, rule, "run")
            history = (await client.get(f"/api/automations/{rule['id']}/history")).json()["items"]
            assert [fire["status"] for fire in history] == ["coalesced", "skipped", "queued"]
            app.state.manager.jobs.resume()
            await wait_job(client, first["job_id"])
            await app.state.manager.automations.tick()
            latest = await settled(client, app, rule)
            assert latest["status"] == "completed" and len(model.requests) == 2
        await app.state.manager.shutdown()
    asyncio.run(scenario())


def test_catchup_only_latest_and_once_disables_after_result(tmp_path: Path):
    """长时间漏跑明确补最近一次；一次性任务在已接纳请求完成后停用而不取消排队。"""
    async def scenario():
        """注入未来时刻核对补跑时间，随后创建一次性排队任务并验证终态停用。"""
        app, _, model = application(tmp_path, [reply(text="补跑完成"), reply(text="一次性完成")])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            rule = (await client.post("/api/automations", json=definition(kind="interval", cron=None, interval_minutes=1, catch_up=True, catch_up_minutes=10))).json()
            await action(client, rule, "enable")
            due = datetime.fromisoformat(rule["next_at"])
            await app.state.manager.automations.tick(due + timedelta(hours=3, seconds=20))
            fire = await settled(client, app, rule)
            assert datetime.fromisoformat(fire["scheduled_at"]) == due + timedelta(hours=3)
            await action(client, rule, "pause")
            at = datetime.now(UTC) + timedelta(minutes=2)
            once = (await client.post("/api/automations", json=definition(kind="once", cron=None, at=at.isoformat()))).json()
            await action(client, once, "enable")
            app.state.manager.jobs.paused = True
            await app.state.manager.automations.tick(at)
            queued = (await client.get(f"/api/automations/{once['id']}/history")).json()["items"][0]
            assert queued["status"] == "queued"
            app.state.manager.jobs.resume()
            await wait_job(client, queued["job_id"])
            await app.state.manager.automations.tick(at)
            assert not app.state.manager.automations.read(once["id"])["enabled"]
            assert len(model.requests) == 2
        await app.state.manager.shutdown()
    asyncio.run(scenario())


def test_native_notifications_claimed_once_and_read_remains_durable(tmp_path: Path):
    """原生桥故障不会重复弹窗、重跑任务或丢失通知；刷新仍可读取并明确已读。"""
    class Native:
        """记录宿主通知调用并模拟父进程断线，不替代任何模型或文件副作用。"""
        def __init__(self):
            """创建空调用列表，初始没有通知。"""
            self.calls = []  # 每次实际原生桥请求。

        async def request(self, method, parameters):
            """记录method/parameters后抛失联错误，返回值不存在。"""
            self.calls.append((method, parameters))
            raise RuntimeError("offline")

    async def scenario():
        """生成真实文件产物并反复tick，核对只尝试一次原生通知且收件箱保留。"""
        app, _, _ = application(tmp_path, [reply(call("write_file", path="report.txt", content="ok")), reply(text="完成")])
        native = Native()
        app.state.manager.automations.native = native
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            rule = (await client.post("/api/automations", json=definition())).json()
            await action(client, rule, "run")
            await settled(client, app, rule)
            await app.state.manager.automations.tick()
            assert len(native.calls) == 1 and native.calls[0][0] == "notification.show"
            items = (await client.get("/api/automation-notifications")).json()["items"]
            assert len(items) == 1 and native.calls[0][1]["job_id"] == items[0]["job_id"]
        await app.state.manager.shutdown()
    asyncio.run(scenario())


def test_isolated_hook_grant_and_project_verification(tmp_path: Path):
    """已审核Hook和固定验收可在新工作树精确授权，来源目录不产生报告或Hook副作用。"""
    async def scenario():
        """建立独立Git样例，通过公开审核/授权API运行真实Hook、模型文件工具及项目测试。"""
        from agent_harness.projects.models import ProjectPreset
        from agent_harness.permissions.models import PermissionPolicy
        from agent_harness.schemas import RunConfig
        from agent_harness.web.config import WebConfig
        from agent_harness.web.api import create_app
        from tests.web.test_planning_api import ScriptModel
        source = tmp_path / "source"
        source.mkdir()
        (source / ".agents").mkdir()
        (source / ".agents" / "hooks.toml").write_text('[[hooks]]\nid="setup"\nevent="RunStart"\nargv=["python","hook.py"]\nrequired=true\n', encoding="utf-8")
        (source / "hook.py").write_text('from pathlib import Path\nPath("hook.txt").write_text("hook-ok")\n', encoding="utf-8")
        (source / "check.py").write_text('from pathlib import Path\nassert Path("hook.txt").read_text()=="hook-ok"\nassert Path("report.txt").read_text()=="ready"\nprint("verified")\n', encoding="utf-8")
        for args in (["init", "-q"], ["add", "."], ["-c", "user.name=AutomationTest", "-c", "user.email=test@localhost", "commit", "-qm", "baseline"]):
            subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True, stdin=subprocess.DEVNULL)
        model = ScriptModel([reply(call("write_file", path="report.txt", content="ready")), reply(text="完成")])
        web = WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=tmp_path / "dist", projects=[ProjectPreset(id="git", label="隔离项目", path=source, test_argv=[sys.executable, "check.py"])])
        app = create_app(web, RunConfig(tools=["write_file", "run_command"], permission_policy=PermissionPolicy(automation_grants=True)), tmp_path / "runs", model_client=model)
        app.state.manager._key_provider = lambda config: "offline-automation"  # 仅注入离线测试凭据。
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            hook = (await client.get("/api/hooks?project_id=git")).json()["items"][0]
            reviewed = await client.post("/api/hooks/review?project_id=git", json={"key": hook["key"], "fingerprint": hook["fingerprint"], "action": "trust"})
            assert reviewed.status_code == 200, reviewed.text
            body = definition()
            body["task"] = {"project_id": "git", "execution_mode": "isolated", "prompt": "创建报告"}
            created = await client.post("/api/automations", json=body)
            assert created.status_code == 201, created.text
            rule = created.json()
            review = (await client.get(f"/api/automations/{rule['id']}/review")).json()
            grant = await client.post(f"/api/automations/{rule['id']}/grant", json={"revision": 1, "fingerprint": review["fingerprint"], "hooks": ["project:setup"], "verification": True})
            assert grant.status_code == 200, grant.text
            await action(client, rule, "run")
            fire = await settled(client, app, rule)
            assert fire["status"] == "completed" and fire["validation_status"] == "passed", fire
            view = (await client.get("/api/runs/" + fire["run_id"])).json()
            assert view["execution_mode"] == "isolated" and Path(view["workspace"]).joinpath("hook.txt").read_text() == "hook-ok"
            assert not (source / "report.txt").exists() and not (source / "hook.txt").exists()
    asyncio.run(scenario())


def test_once_coalesced_trigger_survives_earlier_manual_completion(tmp_path: Path):
    """一次性计划与手动试跑重叠时，合并触发仍须执行，不能被先完成的手动项误停用。"""
    async def scenario():
        """暂停队列制造确定重叠，先完成手动项再执行原计划，核对两条独立运行及规则停用。"""
        app, _, model = application(tmp_path, [reply(text="手动完成"), reply(text="计划完成")])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            at = datetime.now(UTC) + timedelta(minutes=2)
            body = definition(kind="once", cron=None, at=at.isoformat())
            body["overlap"] = "coalesce"
            rule = (await client.post("/api/automations", json=body)).json()
            await action(client, rule, "enable")
            app.state.manager.jobs.paused = True
            await action(client, rule, "run")
            first = (await client.get(f"/api/automations/{rule['id']}/history")).json()["items"][0]
            await app.state.manager.automations.tick(at)
            history = (await client.get(f"/api/automations/{rule['id']}/history")).json()["items"]
            assert [item["status"] for item in history] == ["coalesced", "queued"]
            app.state.manager.jobs.resume()
            await wait_job(client, first["job_id"])
            await app.state.manager.automations.tick(at)
            assert app.state.manager.automations.read(rule["id"])["enabled"]
            planned = await settled(client, app, rule)
            assert planned["status"] == "completed" and len(model.requests) == 2
            assert not app.state.manager.automations.read(rule["id"])["enabled"]
        await app.state.manager.shutdown()
    asyncio.run(scenario())


def test_accepted_job_response_loss_never_replays_trigger(tmp_path: Path, monkeypatch):
    """作业已入库但投递回包丢失时，触发只对账原作业，不再执行第二次文件修改。"""
    async def scenario():
        """在真实队列接纳后注入一次异常，重复tick仍只保留一条作业和实际写入。"""
        app, workspace, model = application(tmp_path, [reply(call("write_file", path="once.txt", content="one")), reply(text="完成")])
        original = app.state.manager.jobs.enqueue
        async def lost_response(*args, **kwargs):
            """先真实接纳args/kwargs再模拟回包丢失；不伪造数据库或文件执行结果。"""
            await original(*args, **kwargs)
            raise RuntimeError("response lost")
        monkeypatch.setattr(app.state.manager.jobs, "enqueue", lost_response)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            rule = (await client.post("/api/automations", json=definition())).json()
            assert (await action(client, rule, "run")).status_code == 200
            await settled(client, app, rule)
            await app.state.manager.automations.tick()
            await app.state.manager.automations.tick()
            assert len(app.state.manager.jobs.list()) == 1 and len(model.requests) == 2
            assert workspace.joinpath("once.txt").read_text() == "one"
        await app.state.manager.shutdown()
    asyncio.run(scenario())
