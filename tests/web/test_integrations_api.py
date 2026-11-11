"""通过真实HTTP、持久队列、Git工作树和本机检查验证成果采纳完整链路。"""

import asyncio
from pathlib import Path
from uuid import uuid4

from tests.web.test_projects import project_config, client_for, start, finished, git


async def integration_finished(client, identifier: str, operation: str | None = None) -> dict:
    """等待identifier关联作业真实收尾并返回最新记录；operation可限定检查阶段，不重投请求。"""
    async with asyncio.timeout(45):
        while True:
            jobs = (await client.get("/api/jobs")).json()["items"]
            active = [job for job in jobs if job.get("integration_id") == identifier and (operation is None or job.get("operation") == operation) and job["status"] in {"queued", "preparing", "running"}]
            if not active:
                response = await client.get("/api/integrations/" + identifier)
                assert response.status_code == 200, response.text
                return response.json()
            await asyncio.sleep(.03)


async def action(client, value: dict, operation: str, request_id: str | None = None):
    """明确批准value当前版本的operation并返回响应；request_id非空时用于验证重复决定。"""
    return await client.post(f"/api/integrations/{value['id']}/actions", json={"request_id": request_id or str(uuid4()), "expected_revision": value["revision"], "fingerprint": value["fingerprint"], "action": operation})


def test_queue_conflict_checks_apply_history_and_undo(project_config, monkeypatch):
    """人工解决真实冲突，候选与目标分别检查；重复批准不重写，暂存区和来源模型计数保持。"""
    monkeypatch.setenv("HARNESS_TEST_MODEL_KEY", "offline-integration")

    async def scenario():
        """执行桌面与Web共用HTTP链路并核对实际文件、队列、审批、历史及撤销预览。"""
        async with client_for(project_config) as (client, app):
            run = await start(client)
            view = await finished(client, run["run_id"])
            assert view["result"]["execution_status"] == "completed"
            repository = project_config.projects[0].path
            (repository / "staged.txt").write_text("staged", encoding="utf-8")
            git(repository, "add", "staged.txt")
            index = (repository / ".git" / "index").read_bytes()
            targets = await client.get(f"/api/runs/{run['run_id']}/integration-targets")
            assert targets.status_code == 200, targets.text
            assert targets.json()["items"][0]["path"] == str(repository)
            request_id = str(uuid4())
            body = {"request_id": request_id, "target_id": "backend"}
            response = await client.post(f"/api/runs/{run['run_id']}/integrations", json=body)
            assert response.status_code == 202, response.text
            value = await integration_finished(client, request_id)
            assert value["status"] == "conflicts", value
            repeated = await client.post(f"/api/runs/{run['run_id']}/integrations", json=body)
            assert repeated.status_code == 202, repeated.text
            resolution = await client.post(f"/api/integrations/{request_id}/resolutions", json={"request_id": str(uuid4()), "expected_revision": value["revision"], "resolutions": [{"path": "src/app.py", "choice": "source"}]})
            assert resolution.status_code == 200, resolution.text
            value = resolution.json()
            before = await action(client, value, "apply")
            assert before.status_code == 202, before.text
            blocked = await integration_finished(client, request_id)
            assert blocked["status"] == "failed"  # 候选尚未通过检查，未修改目标。
            assert "999" in (repository / "src/app.py").read_text()
            # 失败的批准不重用；重新准备完整候选后先运行项目检查。
            request_id = str(uuid4())
            response = await client.post(f"/api/runs/{run['run_id']}/integrations", json={"request_id": request_id, "target_id": "backend"})
            assert response.status_code == 202, response.text
            value = await integration_finished(client, request_id)
            response = await client.post(f"/api/integrations/{request_id}/resolutions", json={"request_id": str(uuid4()), "expected_revision": value["revision"], "resolutions": [{"path": "src/app.py", "choice": "source"}]})
            value = response.json()
            response = await action(client, value, "check_candidate")
            assert response.status_code == 202, response.text
            value = await integration_finished(client, request_id)
            assert value["checks"][-1]["result"]["status"] == "passed", value
            assert value["checks"][-1]["applicable"]
            approve_id = str(uuid4())
            response = await action(client, value, "apply", approve_id)
            assert response.status_code == 202, response.text
            applied = await integration_finished(client, request_id)
            assert applied["status"] == "applied", applied
            assert (repository / ".git" / "index").read_bytes() == index
            assert "a + b" in (repository / "src/app.py").read_text()
            response = await action(client, value, "apply", approve_id)
            assert response.status_code == 202, response.text
            assert response.json()["status"] == "applied"
            response = await action(client, applied, "check_target")
            assert response.status_code == 202, response.text
            checked = await integration_finished(client, request_id)
            assert checked["checks"][-1]["scope"] == "target"
            assert checked["checks"][-1]["result"]["status"] == "passed", checked
            # 模拟复验进程退出前宿主失联：采纳事实不能冒充这次复验作业的回执。
            manager = app.state.manager
            interrupted = next(job for job in manager.jobs.list() if job.get("integration_id") == request_id and job.get("operation") == "check_target")
            interrupted.update(status="running")
            manager.jobs.save(interrupted)
            manager.jobs.paused = True
            manager.jobs.reconcile()
            recovered = (await client.get("/api/jobs/" + interrupted["id"])).json()
            assert recovered["status"] == "needs_review" and recovered["recovery_kind"] == "process"
            assert manager.integrations.store.get(request_id)["status"] == "applied"
            response = await client.post("/api/jobs/" + interrupted["id"] + "/resolve")
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "failed"
            response = await client.post("/api/queue/resume")
            assert response.status_code == 200, response.text

            # 准备阶段失联不能留下永久准备中；解除进程阻塞后重新准备，而非重放旧请求。
            manager.jobs.paused = True
            preparation_id = str(uuid4())
            response = await client.post(f"/api/runs/{run['run_id']}/integrations", json={"request_id": preparation_id, "target_id": "backend"})
            assert response.status_code == 202, response.text
            interrupted = manager.jobs.get(preparation_id)
            interrupted.update(status="running")
            manager.jobs.save(interrupted)
            manager.jobs.reconcile()
            assert manager.integrations.store.get(preparation_id)["status"] == "failed"
            assert manager.jobs.get(preparation_id)["recovery_kind"] == "process"
            assert (await client.post("/api/jobs/" + preparation_id + "/resolve")).status_code == 200
            assert (await client.post("/api/queue/resume")).status_code == 200
            history = await client.get(f"/api/runs/{run['run_id']}/integrations")
            assert len(history.json()["items"]) == 3
            permissions = await client.get(f"/api/runs/{run['run_id']}/permissions")
            assert any(item.get("owner_kind") == "integration" and item["status"] == "executed" for item in permissions.json()["items"])
            undone_id = str(uuid4())
            response = await action(client, checked, "undo", undone_id)
            assert response.status_code == 202, response.text
            await integration_finished(client, request_id)
            reverse = (await client.get("/api/integrations/" + undone_id)).json()
            assert reverse["kind"] == "undo" and reverse["status"] == "ready", reverse
            response = await action(client, reverse, "check_candidate")
            assert response.status_code == 202, response.text
            reverse = await integration_finished(client, undone_id)
            assert reverse["checks"][-1]["result"]["status"] == "failed"
            response = await action(client, reverse, "apply")
            assert response.status_code == 202, response.text
            reverse = await integration_finished(client, undone_id)
            assert reverse["status"] == "applied"
            assert (await client.get("/api/integrations/" + request_id)).json()["status"] == "undone"
            assert "999" in (repository / "src/app.py").read_text()
            assert (repository / ".git" / "index").read_bytes() == index
    asyncio.run(scenario())


def test_concurrent_prepare_cancel_permissions_and_stale_approval(project_config, monkeypatch):
    """并发准备幂等、排队取消撤权、宿主禁止不能绕过；批准后目标变化也不会写回。"""
    monkeypatch.setenv("HARNESS_TEST_MODEL_KEY", "offline-integration")
    project_config.projects[0].test_argv = []

    async def scenario():
        """通过正式入口操作暂停队列，核对全部真实文件与权限状态，不伪造写回结果。"""
        async with client_for(project_config) as (client, app):
            source = await start(client)
            await finished(client, source["run_id"])
            manager = app.state.manager
            manager.jobs.paused = True
            identifier = str(uuid4())
            path = f"/api/runs/{source['run_id']}/integrations"
            body = {"request_id": identifier, "target_id": "backend"}
            responses = await asyncio.gather(client.post(path, json=body), client.post(path, json=body))
            assert all(response.status_code == 202 for response in responses)
            blocked = await client.post(path, json={"request_id": str(uuid4()), "target_id": "backend"})
            assert blocked.status_code == 409
            response = await client.post(f"/api/jobs/{identifier}/cancel")
            assert response.status_code == 200
            assert (await client.get("/api/integrations/" + identifier)).json()["status"] == "cancelled"
            await client.post("/api/queue/resume")
            manager.run_config.permission_policy.read = "deny"
            response = await client.post(path, json={"request_id": str(uuid4()), "target_id": "backend"})
            assert response.status_code == 409
            manager.run_config.permission_policy.read = "allow"
            identifier = str(uuid4())
            response = await client.post(path, json={"request_id": identifier, "target_id": "backend"})
            assert response.status_code == 202, response.text
            value = await integration_finished(client, identifier)
            response = await client.post(f"/api/integrations/{identifier}/resolutions", json={"request_id": str(uuid4()), "expected_revision": value["revision"], "resolutions": [{"path": "src/app.py", "choice": "source"}]})
            assert response.status_code == 200, response.text
            value = response.json()
            manager.run_config.permission_policy.write = "deny"
            assert (await action(client, value, "apply")).status_code == 409
            manager.run_config.permission_policy.write = "allow"
            manager.run_config.permission_policy.verification = "deny"
            assert (await action(client, value, "check_candidate")).status_code == 409
            manager.run_config.permission_policy.verification = "ask"
            manager.jobs.paused = True
            grant = str(uuid4())
            assert (await action(client, value, "apply", grant)).status_code == 202
            assert (await client.post(f"/api/jobs/{grant}/cancel")).status_code == 200
            from agent_harness.permissions.store import PermissionStore
            assert PermissionStore(manager.runs_root).get(grant)["status"] == "cancelled"
            new_grant = str(uuid4())
            assert (await action(client, value, "apply", new_grant)).status_code == 202
            repository = project_config.projects[0].path
            index = (repository / ".git/index").read_bytes()
            (repository / "src/app.py").write_text("manual after approval\n", encoding="utf-8")
            await client.post("/api/queue/resume")
            value = await integration_finished(client, identifier)
            assert value["status"] == "stale", value
            assert (repository / "src/app.py").read_text() == "manual after approval\n"
            assert (repository / ".git/index").read_bytes() == index
            assert PermissionStore(manager.runs_root).get(new_grant)["status"] == "failed"
    asyncio.run(scenario())
