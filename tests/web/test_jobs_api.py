"""真实 API、队列及 Runner 任务工具的端到端回归，模型仅提供固定响应。"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4
import httpx

from tests.helpers.permissions import trusted_config
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from tests.web.test_planning_api import ScriptModel, reply, call
from agent_harness.storage.runs import RunStore
from agent_harness.web.service import _unfinished_result


def application(tmp_path: Path, model):
    """创建 tmp_path 专属项目和记录，注入 model，返回应用及工作目录。"""
    workspace = tmp_path / "project"
    workspace.mkdir(exist_ok=True)
    config = WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=tmp_path / "dist", workspaces=[WebWorkspace(id="files", label="测试项目", path=workspace)])
    app = create_app(config, trusted_config(tools=["read_file", "write_file", "run_command"]), tmp_path / "runs", model_client=model)
    app.state.manager._key_provider = lambda config: "offline-key"  # 测试凭据不读用户环境。
    return app, workspace


async def wait_job(client, identifier: str) -> dict:
    """轮询 identifier 至真实收尾，最多十秒；返回作业，不重投请求。"""
    async with asyncio.timeout(10):
        while True:
            job = (await client.get("/api/jobs/" + identifier)).json()
            if job["status"] not in {"queued", "preparing", "running"}:
                return job
            await asyncio.sleep(.01)


def test_queue_cancel_dedupe_and_restart(tmp_path: Path):
    """活动 Run 阻塞时第二项持久排队，重复投递不复制，取消不执行；重启保留暂停队列。"""
    async def scenario():
        """驱动真实应用生命周期；所有资源在场景结束时清理，返回 None。"""
        model = ScriptModel([], gated=True)
        app, workspace = application(tmp_path, model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                first = (await client.post("/api/jobs", json={"request_id": str(uuid4()), "create": {"workspace_id": "files", "prompt": "第一个"}})).json()
                await model.entered.wait()
                observer, _ = application(tmp_path, ScriptModel([]))
                assert observer.state.manager.jobs.paused
                assert observer.state.manager.jobs.get(first['id'])['status'] == 'running'  # 不把另一宿主的活动 Run 标为中断。
                body = {"request_id": str(uuid4()), "create": {"workspace_id": "files", "prompt": "第二个"}}
                second = (await client.post("/api/jobs", json=body)).json()
                assert second["status"] == "queued"
                assert (await client.post("/api/jobs", json=body)).json()["id"] == second["id"]
                assert len(model.requests) == 1
                assert not (tmp_path / "runs" / second["run_id"]).exists()
                assert (await client.post("/api/jobs/" + second["id"] + "/cancel")).json()["status"] == "cancelled"
                third = (await client.post("/api/jobs", json={**body, "request_id": str(uuid4())})).json()
                await app.state.manager.shutdown()
        restarted, _ = application(tmp_path, ScriptModel([reply()]))
        assert restarted.state.manager.jobs.paused
        assert restarted.state.manager.jobs.get(third["id"])["status"] == "queued"
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://testserver") as client:
                assert (await client.post("/api/queue/resume")).status_code == 200
                assert (await wait_job(client, third["id"]))["status"] == "completed"
        assert workspace.is_dir()
    asyncio.run(scenario())


class TaskModel:
    """动态读取真实工具回执，以任务认领、写文件和证据完成推动现有 Runner。"""

    def __init__(self):
        """初始无工作项；计数只控制固定脚本步骤，不模拟工具副作用。"""
        self.index = 0  # 当前响应序号。
        self.identifier = None  # 从真实 task_create 回执获得的稳定任务编号。
        self.requests = []  # 实际模型输入，检查最新任务状态刷新。

    async def complete(self, messages: list, tools: list):
        """根据 messages 的真实工具结果产生下一响应；tools 供核对定义，返回模型响应。"""
        self.requests.append(messages)
        index = self.index
        self.index += 1
        if index == 0:
            assert "task_update" in [tool["function"]["name"] for tool in tools]
            return reply(call("task_create", subject="交付文件", acceptance=["结果为完成"]))
        if index == 1:
            self.identifier = json.loads(messages[0]["content"].split("\n")[-1])["items"][0]["id"]
            return reply(call("task_update", task_id=self.identifier, expected_revision=1, status="in_progress"))
        if index == 2:
            return reply(call("write_file", path="result.txt", content="完成"))
        if index == 3:
            reference = messages[-1]["tool_call_id"]
            # 当前运行编号只从宿主工具证据的关联外部注入，不接受任意任务清单。
            return reply(call("task_update", task_id=self.identifier, expected_revision=2, status="completed", completion_summary="已生成结果文件", evidence_refs=[{"run_id": self.run_id, "tool_call_id": reference}]))
        return reply(text="已交付")


def test_task_tools_real_runner_and_history(tmp_path: Path):
    """从入队到工具执行、任务证据、固定历史及前端接口读取完成全链路。"""
    async def scenario():
        """使用固定模型和真实文件工具执行，返回 None；所有输出留在专属测试目录。"""
        model = TaskModel()
        app, workspace = application(tmp_path, model)
        # 在真正模型调用前从 Runner 宿主读取运行编号，只供离线模型构造证据引用。
        launch = app.state.manager._launch
        async def traced_launch(manifest, api_key):
            """记录 manifest 编号后原样启动；api_key 只交给真实 launch，返回 RunView。"""
            model.run_id = str(manifest.run_id)
            return await launch(manifest, api_key)
        app.state.manager._launch = traced_launch
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                response = await client.post("/api/jobs", json={"request_id": str(uuid4()), "create": {"workspace_id": "files", "prompt": "生成并核对结果"}})
                assert response.status_code == 202, response.text
                job = await wait_job(client, response.json()["id"])
                assert job["status"] == "completed", job
                assert (workspace / "result.txt").read_text(encoding="utf8") == "完成"
                view = (await client.get("/api/runs/" + job["run_id"])).json()
                assert view["result"]["tasks"]["items"][0]["status"] == "completed", view["result"]
                assert view["result"]["validation_status"] == "unverified"
                interrupted = RunStore.read(tmp_path / "runs", job["run_id"])
                interrupted.result = None  # 模拟终态未提交，只采用前面的持久任务事件。
                interrupted.events = [event for event in interrupted.events if event.type != "run_finished"]
                interrupted.interrupted = True
                assert _unfinished_result(interrupted).tasks["items"][0]["status"] == "completed"
                path = "/api/runs/" + job["run_id"] + "/tasks"
                fixed = (await client.get(path + "?historical=true")).json()
                item = fixed["items"][0]
                changed = await client.patch(path, json={"request_id": "reopen", "task_id": item["id"], "expected_revision": item["revision"], "status": "pending"})
                assert changed.status_code == 200, changed.text
                assert (await client.get(path + "?historical=true")).json() == fixed
                assert (await client.get(path)).json()["items"][0]["status"] == "pending"
                assert (await client.get("/api/work-events")).json()["items"]
    asyncio.run(scenario())


def test_queued_source_policy_change_needs_review(tmp_path: Path):
    """排队期间源码/解释器设置变化须重新核对，不能静默使用新策略执行。"""
    async def scenario():
        """在真实活动 Run 后排队第二项，再改变宿主配置，返回 None，保留原请求。"""
        model = ScriptModel([], gated=True)
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                first = (await client.post('/api/jobs', json={"request_id": str(uuid4()), "create": {"workspace_id": "files", "prompt": "等待停止"}})).json()
                await model.entered.wait()
                second = (await client.post('/api/jobs', json={"request_id": str(uuid4()), "create": {"workspace_id": "files", "prompt": "保留原策略"}})).json()
                app.state.manager.web_config.workspaces[0].python_paths = []
                await client.post('/api/jobs/' + first['id'] + '/cancel')
                result = await wait_job(client, second['id'])
                assert result['status'] == 'needs_review' and result['error_code'] == 'job_source_changed'
                assert not (tmp_path / 'runs' / second['run_id']).exists()
                assert len(model.requests) == 1
    asyncio.run(scenario())
