"""通过真实 HTTP、Runner、文件与进程验收跨项目调度，模型仅提供确定响应。"""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
from uuid import UUID, uuid4

import httpx
import pytest

from agent_harness.execution.scheduling import resources, conflicts
from agent_harness.execution.scheduling import Coordinator
from agent_harness.work.tasks import WorkStore, encode, TaskError
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from tests.helpers.permissions import trusted_config
from tests.web.test_jobs_api import wait_job
from tests.web.test_planning_api import call, reply


class ProjectModel:
    """按用户任务身份分别阻塞，验证并发调用、取消和工具结果不会串线。"""

    def __init__(self, commands: bool = False):
        """commands=True 运行真实长命令，False 写文件后等待；初始所有任务均未进入。"""
        self.commands = commands  # 是否用真实进程验证运行级取消隔离。
        self.entered = {name: asyncio.Event() for name in ("TASK_A", "TASK_B", "TASK_C")}  # 每个任务独立的可观察入口。
        self.gates = {name: asyncio.Event() for name in self.entered}  # 只允许测试释放指定任务。
        self.steps: dict[str, int] = {}  # 各项目已响应次数，不用全局响应列表决定归属。
        self.cancelled: set[str] = set()  # 真实模型等待中收到取消的任务集合。

    async def complete(self, messages: list[dict], tools: list[dict]):
        """从 messages 用户要求识别项目；tools 沿用真实定义，返回工具调用或最终响应。"""
        text = json.dumps([message for message in messages if message["role"] == "user"], ensure_ascii=False)
        name = next(name for name in self.entered if name in text)
        index = self.steps.get(name, 0)
        self.steps[name] = index + 1
        if index == 0:
            if self.commands:
                return reply(call("run_command", argv=[sys.executable, "-c", "from pathlib import Path; import time; Path('started').write_text('ready'); time.sleep(60)"]))
            return reply(call("write_file", path="result.txt", content=name))
        self.entered[name].set()
        try:
            await self.gates[name].wait()
        except asyncio.CancelledError:
            self.cancelled.add(name)
            raise
        return reply(text=name + "完成")


def application(root: Path, model: ProjectModel, **settings):
    """在 root 创建三个独立工作区；settings 覆盖宿主容量，返回应用与目录列表，不联网。"""
    directories = [root / name for name in ("a", "b", "c")]
    for directory in directories:
        directory.mkdir(exist_ok=True)
    config = WebConfig(project_dir=root, worktrees_root=root / "trees", frontend_dist=root / "dist",
                       workspaces=[WebWorkspace(id=path.name, label=path.name, path=path) for path in directories], **settings)
    app = create_app(config, trusted_config(tools=["write_file", "run_command"]), root / "runs", model_client=model)
    app.state.manager._key_provider = lambda config: "offline-scheduler"  # 不读取用户真实凭据。
    return app, directories


async def enqueue(client: httpx.AsyncClient, project: str, name: str) -> dict:
    """通过 client 为 project 提交 name 要求，返回持久作业；失败保留完整受控响应用于断言。"""
    response = await client.post("/api/jobs", json={"request_id": str(uuid4()), "create": {"workspace_id": project, "prompt": name}})
    assert response.status_code == 202, response.text
    return response.json()


def test_independent_projects_parallel_cancel_and_capacity(tmp_path: Path):
    """两个项目真实写入后同时等待，第三项排队；取消一个不终止另一个，并让第三项接替。"""
    async def scenario():
        """驱动真实生命周期并核对文件、活动集合、定向取消和持久终态，返回 None。"""
        model = ProjectModel()
        app, directories = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            first = await enqueue(client, "a", "TASK_A")
            assert "resources" not in first  # 内部路径/对象身份仅供宿主协调，不向页面公开。
            await asyncio.wait_for(model.entered["TASK_A"].wait(), 10)
            second = await enqueue(client, "b", "TASK_B")
            await asyncio.wait_for(model.entered["TASK_B"].wait(), 10)
            state = (await client.get("/api/options")).json()
            assert set(state["active_run_ids"]) == {first["run_id"], second["run_id"]}
            assert [path.joinpath("result.txt").read_text() for path in directories[:2]] == ["TASK_A", "TASK_B"]
            third = await enqueue(client, "c", "TASK_C")
            assert third["status"] == "queued" and third["blocked_reason"] == "等待运行槽位"
            await client.post("/api/jobs/" + first["id"] + "/cancel")
            await asyncio.wait_for(model.entered["TASK_C"].wait(), 10)
            assert (await client.get("/api/runs/" + second["run_id"])).json()["active"]
            assert model.cancelled == {"TASK_A"}
            model.gates["TASK_B"].set()
            model.gates["TASK_C"].set()
            assert (await wait_job(client, second["id"]))["status"] == "completed"
            assert (await wait_job(client, third["id"]))["status"] == "completed"
            assert (await wait_job(client, first["id"]))["status"] == "cancelled"
    asyncio.run(scenario())


def test_blocked_project_does_not_block_independent_project(tmp_path: Path):
    """队首同目录等待不能挡住后续独立项目；页面返回可定位的占用作业。"""
    async def scenario():
        """将同目录请求排在独立项目之前，核对真实进入模型的任务和文件，返回 None。"""
        model = ProjectModel()
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            first = await enqueue(client, "a", "TASK_A")
            await asyncio.wait_for(model.entered["TASK_A"].wait(), 10)
            blocked = await enqueue(client, "a", "TASK_C")
            assert blocked["status"] == "queued" and blocked["blocked_by"] == first["id"]
            second = await enqueue(client, "b", "TASK_B")
            await asyncio.wait_for(model.entered["TASK_B"].wait(), 10)
            assert "TASK_C" not in model.steps
            assert (await client.get("/api/runs/" + second["run_id"])).json()["active"]
            await client.post("/api/jobs/" + blocked["id"] + "/cancel")
    asyncio.run(scenario())


def test_shared_model_limit_applies_across_runs(tmp_path: Path):
    """主运行容量为二而模型容量为一时，第二个项目等待容量；取消第一项才进入供应商。"""
    async def scenario():
        """基于实际模型入口和文件副作用核对共享限流，不用运行状态标签代替并发事实。"""
        model = ProjectModel()
        app, paths = application(tmp_path, model, max_model_requests=1)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            first = await enqueue(client, "a", "TASK_A")
            await asyncio.wait_for(model.entered["TASK_A"].wait(), 10)
            second = await enqueue(client, "b", "TASK_B")
            assert (await client.get("/api/runs/" + second["run_id"])).json()["active"]
            assert not paths[1].joinpath("result.txt").exists()
            await client.post("/api/jobs/" + first["id"] + "/cancel")
            await asyncio.wait_for(model.entered["TASK_B"].wait(), 10)
            assert paths[1].joinpath("result.txt").read_text() == "TASK_B"
    asyncio.run(scenario())


def test_restart_only_resumes_unstarted_requests(tmp_path: Path):
    """重启恢复偏好持久化；干净退出后的未开始请求可自动调度，旧运行不会再执行。"""
    async def scenario():
        """先占满单槽并入队，然后正常关闭重启；核对旧运行调用次数和新任务写入，返回 None。"""
        model = ProjectModel()
        app, _ = application(tmp_path, model, max_concurrent_runs=1)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await enqueue(client, "a", "TASK_A")
            await asyncio.wait_for(model.entered["TASK_A"].wait(), 10)
            pending = await enqueue(client, "b", "TASK_B")
            assert pending["status"] == "queued"
            assert (await client.put("/api/queue/settings", json={"resume_on_start": True})).status_code == 200
        restarted_model = ProjectModel()
        restarted_model.gates["TASK_B"].set()
        restarted, _ = application(tmp_path, restarted_model)
        async with restarted.router.lifespan_context(restarted), httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://testserver") as client:
            assert (await wait_job(client, pending["id"]))["status"] == "completed"
            assert "TASK_A" not in restarted_model.steps
            assert (await client.get("/api/jobs")).json()["scheduler"]["resume_on_start"] is True
    asyncio.run(scenario())


def test_git_common_directory_and_overlapping_paths_are_serial(tmp_path: Path):
    """同仓库不同工作树、父子目录和显式共享组都判为冲突，独立目录可并行。"""
    repository, worktree, other = (tmp_path / name for name in ("repository", "tree", "other"))
    repository.mkdir()
    other.mkdir()
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Harness", "-c", "user.email=harness@example.invalid", "commit", "--allow-empty", "-m", "base"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "worktree", "add", "--detach", str(worktree)], check=True, capture_output=True)
    nested = repository / "nested"
    nested.mkdir()
    assert conflicts(resources([repository], {}), resources([worktree], {}))
    assert conflicts(resources([repository], {}), resources([nested], {}))
    assert not conflicts(resources([repository], {}), resources([other], {}))
    groups = {"database": [repository, other]}
    assert conflicts(resources([repository], groups), resources([other], groups))


def test_cancelling_one_real_process_preserves_other(tmp_path: Path):
    """两个项目的真实长进程同时运行；取消一项后，另一项进程及运行仍受原 Run 管理。"""
    async def scenario():
        """等待真实进程创建标记，再定向取消并核对双端命令清理状态，返回 None。"""
        app, paths = application(tmp_path, ProjectModel(commands=True))
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            first = await enqueue(client, "a", "TASK_A")
            second = await enqueue(client, "b", "TASK_B")
            async with asyncio.timeout(15):
                while not all(path.joinpath("started").exists() for path in paths[:2]):
                    await asyncio.sleep(.02)
            await client.post("/api/jobs/" + first["id"] + "/cancel")
            finished = await wait_job(client, first["id"])
            assert finished["status"] == "cancelled"
            assert (await client.get("/api/runs/" + first["run_id"])).json()["result"]["cleanup_status"] == "complete"
            assert (await client.get("/api/runs/" + second["run_id"])).json()["active"]
            await client.post("/api/jobs/" + second["id"] + "/cancel")
            assert (await wait_job(client, second["id"]))["status"] == "cancelled"
    asyncio.run(scenario())


def test_unknown_resource_only_blocks_related_project(tmp_path: Path):
    """缺失回执只阻塞原项目；重启不执行未知动作，人工核对后允许新的请求执行。"""
    async def scenario():
        """注入持久未知租约并重启真实宿主，从 HTTP 核对局部阻塞及人工解除，返回 None。"""
        seed, paths = application(tmp_path, ProjectModel())
        coordinator = seed.state.manager.coordinator
        owner = str(uuid4())
        coordinator.acquire(owner, resources([paths[0]], {}), None, None)
        coordinator.release(owner, uncertain=True)  # 模拟动作已开始但没有可信结束回执。
        with pytest.raises(TaskError, match="原执行归属尚未核对"):
            coordinator.acquire(owner, resources([paths[0]], {}), None, None)
        model = ProjectModel()
        app, _ = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await client.post("/api/queue/resume")
            blocked = await enqueue(client, "a", "TASK_A")
            assert blocked["status"] == "queued" and blocked["blocked_by"] == owner
            independent = await enqueue(client, "b", "TASK_B")
            await asyncio.wait_for(model.entered["TASK_B"].wait(), 10)
            assert "TASK_A" not in model.steps
            assert (await client.post("/api/jobs/" + owner + "/resolve")).status_code == 200
            await asyncio.wait_for(model.entered["TASK_A"].wait(), 10)
            assert (await client.get("/api/runs/" + independent["run_id"])).json()["active"]
    asyncio.run(scenario())


def test_completed_run_reconciles_job_without_reexecution(tmp_path: Path):
    """Run 已落终态而作业提交丢失时，启动自动对账，绝不重新调用模型或工具。"""
    async def scenario():
        """完成真实写入后注入丢失最后作业事务的状态，重启核对终态和空模型轨迹，返回 None。"""
        model = ProjectModel()
        model.gates["TASK_A"].set()
        app, paths = application(tmp_path, model)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            job = await enqueue(client, "a", "TASK_A")
            job = await wait_job(client, job["id"])
        coordinator = Coordinator(WorkStore(tmp_path / "runs"), 2, 2, 4)
        coordinator.acquire(job["id"], resources([paths[0]], {}), job["session_id"], job["run_id"])
        job["status"] = "running"
        with coordinator.store.transaction() as db:
            db.execute("UPDATE jobs SET body=? WHERE id=?", (encode(job), job["id"]))
        coordinator.release(job["id"], uncertain=True)
        restarted_model = ProjectModel()
        restarted, _ = application(tmp_path, restarted_model)
        async with restarted.router.lifespan_context(restarted), httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://testserver") as client:
            assert (await client.get("/api/jobs/" + job["id"])).json()["status"] == "completed"
            assert not restarted.state.manager.coordinator.records()
            assert not restarted_model.steps
            assert paths[0].joinpath("result.txt").read_text() == "TASK_A"
    asyncio.run(scenario())


def test_shutdown_waits_for_manual_workspace_action(tmp_path: Path):
    """没有活动主 Run 时，宿主仍须等待人工工作树操作的线程结束，不能提前释放文件资源。"""
    import threading

    async def scenario():
        """完成真实运行后建立人工操作，关停保持等待，线程结束后才释放租约并返回 None。"""
        model = ProjectModel()
        model.gates["TASK_A"].set()
        app, _ = application(tmp_path, model)
        entered, release = threading.Event(), threading.Event()
        manager = app.state.manager

        def file_operation():
            """模拟正在操作文件的受控线程；等 release 明确释放后返回 None，不触碰其他目录。"""
            entered.set()
            assert release.wait(5)

        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            job = await enqueue(client, "a", "TASK_A")
            await wait_job(client, job["id"])
            await manager.jobs.worker

            async def manual():
                """在真实运行来源租约中执行人工文件线程，返回 None，退出时由宿主释放资源。"""
                async with manager.workspace_action(UUID(job["run_id"])):
                    await asyncio.to_thread(file_operation)

            operation = asyncio.create_task(manual())
            assert await asyncio.to_thread(entered.wait, 5)
            closing = asyncio.create_task(manager.shutdown())
            try:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(closing), .05)
                assert manager.coordinator.owned
            finally:
                release.set()
            await operation
            await closing
            assert not manager.coordinator.owned
    asyncio.run(scenario())


def test_plain_directory_without_git_and_unverifiable_repository(tmp_path: Path, monkeypatch):
    """未安装 Git 时普通目录仍可执行；出现仓库标识后必须拒绝无法核对公共目录的执行。"""
    workspace = tmp_path / "plain"
    workspace.mkdir()
    monkeypatch.setattr("agent_harness.execution.scheduling.shutil.which", lambda name: None)  # 模拟宿主没有 Git，不运行替代程序。
    scope = resources([workspace], {})
    assert scope["repositories"] == [] and scope["paths"]
    (workspace / ".git").mkdir()
    with pytest.raises(TaskError, match="Git 项目需要可用的 Git"):
        resources([workspace], {})
