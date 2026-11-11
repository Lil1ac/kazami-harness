"""真实HTTP、SQLite队列及Runner验证子研究报告、轨迹、隔离与重启只读语义。"""

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx

from agent_harness.schemas import RunConfig, RunManifest, SubagentRecord, TaskSpec
from tests.helpers.models import configured_run
from agent_harness.storage.runs import RunStore
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from tests.web.test_planning_api import ScriptModel, call, reply
from tests.subagents.test_subagents import report
from tests.web.job_helpers import submit_followup


def app_config(tmp_path: Path, monkeypatch) -> WebConfig:
    """创建tmp_path内独立项目和离线凭据；返回配置，不读取真实密钥或项目。"""
    monkeypatch.setenv("HARNESS_TEST_MODEL_KEY", "offline-subagents")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "api.py").write_text("value = 42\n", encoding="utf-8")
    return WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=tmp_path / "dist",
                     workspaces=[WebWorkspace(id="sample", label="子研究", path=workspace)])


async def wait_finished(client: httpx.AsyncClient, run_id: str) -> dict:
    """等待client中的run_id最多10秒完成；返回真实视图，不以固定睡眠推断执行成功。"""
    async with asyncio.timeout(10):
        while True:
            response = await client.get(f"/api/runs/{run_id}")
            if response.status_code == 200 and not response.json()["active"]:
                return response.json()
            await asyncio.sleep(0.01)


def test_http_queue_delegation_result_pagination_and_restart_do_not_reexecute(tmp_path: Path, monkeypatch):
    """同一作业重复接纳只委派一次，报告与来源可分页，服务重建后读取不调用模型。"""
    async def scenario():
        """创建应用、完成真实文件交付、重建应用并检查历史；返回None，断言失败保留证据。"""
        config = app_config(tmp_path, monkeypatch)
        reading = call("read_file", path="api.py")
        model = ScriptModel([reply(call("delegate_readonly", title="检查接口", objective="读取api.py。", role="review")),
                             reply(reading), reply(text=report("api.py定义value=42。", ids=[reading.call_id])),
                             reply(call("write_file", path="result.txt", content="42")), reply(text="已使用报告。")])
        app = create_app(config, configured_run(tools=["read_file", "write_file"]), tmp_path / "records", model_client=model)
        payload = {"request_id": str(uuid4()), "create": {"workspace_id": "sample", "prompt": "检查接口后生成结果。"}}
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            accepted = await client.post("/api/jobs", json=payload)
            assert accepted.status_code == 202, accepted.text
            run_id = accepted.json()["run_id"]
            assert (await client.post("/api/jobs", json=payload)).json()["run_id"] == run_id
            view = await wait_finished(client, run_id)
            assert view["result"]["model_turns"] == 5
            assert view["result"]["validation_status"] == "unverified"
            children = (await client.get(f"/api/runs/{run_id}/subagents")).json()["items"]
            assert len(children) == 1 and children[0]["status"] == "completed"
            assert "审阅职责" in model.requests[1]["messages"][0]["content"]
            child_id = children[0]["run_id"]
            detail = (await client.get(f"/api/runs/{run_id}/subagents/{child_id}?limit=2")).json()
            assert detail["events"]["has_more"] is True
            assert detail["subagent"]["report"]["summary"] == "api.py定义value=42。"
            assert detail["subagent"]["report"]["partial"] is False
            assert detail["subagent"]["report"]["evidence"][0]["path"] == "api.py"
            assert detail["subagent"]["parent_reserve"]["model_turns"] == 2
            assert detail["context"]["instruction"]["status"] == "missing"
            page = (await client.get(f"/api/runs/{run_id}/subagents/{child_id}?after=2&limit=2")).json()
            assert page["events"]["items"][0]["seq"] == 3
            assert (await client.get(f"/api/runs/{run_id}/subagents/{uuid4()}")).status_code == 404
            assert (await client.get(f"/api/runs/{run_id}/subagents/{child_id}?offset=99999")).status_code == 422
            assert (config.workspaces[0].path / "result.txt").read_text() == "42"
        dormant_model = ScriptModel([])
        restarted = create_app(config, configured_run(), tmp_path / "records", model_client=dormant_model)
        async with restarted.router.lifespan_context(restarted), httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://127.0.0.1") as client:
            assert (await client.get(f"/api/runs/{run_id}/subagents/{child_id}")).json()["report_page"] == detail["report_page"]
            assert not dormant_model.requests
            conversations = (await client.get("/api/conversations")).json()["items"]
            assert len(conversations) == 1
    asyncio.run(scenario())


def test_interrupted_parent_reconstructs_subagent_counts_without_replay(tmp_path: Path, monkeypatch):
    """只有父快照而无终态时保留计数并标记中断，不把孤儿子项显示为正常活动。"""
    async def scenario():
        """构造已提交中断前缀后启动只读服务；返回None，不创建任何新的模型请求。"""
        config = app_config(tmp_path, monkeypatch)
        root = tmp_path / "records"
        manifest = RunManifest(run_id=uuid4(), task=TaskSpec(prompt="中断样例", workspace=config.workspaces[0].path), config=configured_run())
        store = RunStore.create(root, manifest)
        store.append("run_started", {"messages": [], "tools": []})
        store.append("model_request_started", {"model_turn": 1})
        record = SubagentRecord(run_id=uuid4(), title="未结束研究", objective="检查调用方", role="explore", model={"provider": "custom-fixture", "requested_model": "fixture-model"}, max_model_turns=4, max_tool_calls=8, timeout_seconds=60.0, model_turns=2, tool_calls=1, usage={"total_tokens": 20})
        store.append("subagent_snapshot", {"subagent": record.model_dump(mode="json")})
        store.append("subagent_snapshot", {"subagent": record.model_dump(mode="json")})
        model = ScriptModel([])
        app = create_app(config, configured_run(), root, model_client=model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            view = (await client.get(f"/api/runs/{manifest.run_id}")).json()
            assert view["interrupted"] is True
            assert view["result"]["model_turns"] == 3
            assert view["result"]["usage"]["total_tokens"] == 20
            items = (await client.get(f"/api/runs/{manifest.run_id}/subagents")).json()["items"]
            assert items[0]["status"] == "interrupted"
            assert not model.requests
    asyncio.run(scenario())


def test_partial_report_is_visible_and_historical_followup_keeps_original(tmp_path: Path, monkeypatch):
    """实际HTTP展示未完成事项和证据；后续新轮不改写原子报告或继承临时末轮指令。"""
    async def scenario():
        """创建有限父子执行和续轮，返回None；只模拟模型，全部任务、工具与记录真实执行。"""
        config = app_config(tmp_path, monkeypatch)
        reading = call("read_file", path="api.py")
        model = ScriptModel([reply(call("delegate_readonly", title="初步研究", objective="读取接口", max_model_turns=2)),
                             reply(reading), reply(text=report("仅完成初步读取。", ids=[reading.call_id], complete=False)),
                             reply(text="研究未完成，暂不修改。"), reply(text="这是新轮答复。")])
        app = create_app(config, configured_run(tools=["read_file", "write_file"]), tmp_path / "records", model_client=model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            accepted = await client.post("/api/jobs", json={"request_id": str(uuid4()), "create": {"workspace_id": "sample", "prompt": "先研究，不修改。"}})
            first = await wait_finished(client, accepted.json()["run_id"])
            child_id = first["result"]["subagents"][0]["run_id"]
            detail = (await client.get(f"/api/runs/{first['run_id']}/subagents/{child_id}")).json()
            original = detail["subagent"]["report"]
            assert original["partial"] and original["pending"] and original["evidence"][0]["path"] == "api.py"
            followup = await submit_followup(client, f"/api/runs/{first['run_id']}/followups", json={"request_id": str(uuid4()), "prompt": "仅解释已有结果。"})
            second = await wait_finished(client, followup.json()["run_id"])
            assert second["result"]["execution_status"] == "completed"
            assert not any(item["role"] == "system" and "宿主收尾交接" in item["content"] for item in model.requests[-1]["messages"])
            assert (await client.get(f"/api/runs/{first['run_id']}/subagents/{child_id}")).json()["subagent"]["report"] == original
    asyncio.run(scenario())
