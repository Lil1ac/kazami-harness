"""用真实 Git、Runner、文件工具和测试进程验证项目任务与代码审阅 API。"""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import subprocess
import sys
from typing import AsyncIterator
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from agent_harness.config import ConfigurationError
from agent_harness.projects import ProjectPreset
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace, load_web_config
from agent_harness.web.schemas import CreateRunRequest


class ProjectModel:
    """离线模型调用真实 write_file 修复已提交代码；可在后续模型轮次等待取消。"""

    def __init__(self, *, repair: bool = True, gated: bool = False) -> None:
        """repair 决定是否修改代码，gated 决定写入后是否等待；均不访问网络，返回 None。"""
        self.repair = repair  # False 时直接回答，用来验证原有失败测试不会被误判通过。
        self.gated = gated  # True 时在第二轮阻塞，测试可于真实写入后请求取消。
        self.calls = 0  # 实际模型调用次数。
        self.entered = asyncio.Event()  # 阻塞轮次已开始。
        self.release = asyncio.Event()  # 置位才结束阻塞。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """首轮返回两个文件工具调用，后续返回最终回答；messages/tools 为 Runner 输入，无网络副作用。"""
        self.calls += 1
        calls = []
        if self.repair and self.calls == 1:
            for index, (path, content) in enumerate([
                ("src/app.py", "def add(a, b):\n    return a + b\n"),
                ("NEW.txt", "新增变更必须出现在补丁里。\n"),
            ]):
                calls.append(ToolCall(str(index), "write_file", json.dumps({"path": path, "content": content, "overwrite": True})))
        elif self.gated:
            self.entered.set()
            await self.release.wait()
        message = {"role": "assistant", "content": "修改后请检查项目测试。"}
        if calls:
            message["tool_calls"] = [
                {"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}}
                for call in calls
            ]
        return ModelResponse(message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop", response_model="offline-project")


def git(repository: Path, *arguments: str) -> bytes:
    """在临时 repository 运行固定 Git arguments 并返回 stdout；失败立即终止测试，不执行 shell。"""
    return subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True, timeout=10).stdout


@pytest.fixture
def project_config(tmp_path: Path) -> WebConfig:
    """建立有失败测试、已有提交和 dirty 改动的临时 Git 项目；返回仅含项目的服务配置。"""
    repository = tmp_path / "source"
    (repository / "src").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repository / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repository / "tests" / "check.py").write_text(
        "import os\nfrom pathlib import Path\nimport app\n"
        "print('cwd=' + str(Path.cwd()))\nprint('import=' + app.__file__)\n"
        "assert Path(app.__file__).resolve().is_relative_to(Path.cwd().resolve())\n"
        "assert 'DEEPSEEK_API_KEY' not in os.environ\nassert app.add(2, 3) == 5\n", encoding="utf-8",
    )
    git(repository, "init", "-q")
    git(repository, "add", ".")
    git(repository, "-c", "user.name=HarnessTest", "-c", "user.email=test@localhost", "commit", "-qm", "test baseline")
    # 源目录未提交内容必须完全保留，且不能带入 Agent 任务。
    (repository / "src" / "app.py").write_text("def add(a, b):\n    return 999\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("源目录尚未提交的文件", encoding="utf-8")
    return WebConfig(
        project_dir=tmp_path, runtime_root=tmp_path / "runtime" / "samples", frontend_dist=tmp_path / "dist",
        workspaces=[], projects=[ProjectPreset(
            id="backend", label="临时后端", path=repository,
            test_argv=[sys.executable, "tests/check.py"], test_timeout_seconds=10.0,
        )],
    )


@asynccontextmanager
async def client_for(config: WebConfig, model: ProjectModel | None = None) -> AsyncIterator[tuple]:
    """按 config 启动真实 ASGI 生命周期，产出 HTTP 客户端和应用；退出时等待任务清理。"""
    app = create_app(config, RunConfig(tools=["read_file", "write_file"]), config.project_dir / "records", model_client=model or ProjectModel())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            yield client, app


async def start(client: httpx.AsyncClient, **extra) -> dict:
    """向 client 提交固定项目修复任务并合并 extra；要求202，返回完整初始运行视图。"""
    response = await client.post("/api/runs", json={"prompt": "修复 add 并新增说明", "project_id": "backend", **extra})
    assert response.status_code == 202, response.text
    return response.json()


async def finished(client: httpx.AsyncClient, run_id: str) -> dict:
    """在十秒内等待 run_id 完成 Runner 和代码快照；返回最终视图，超时终止测试。"""
    async with asyncio.timeout(10):
        while True:
            response = await client.get(f"/api/runs/{run_id}")
            assert response.status_code == 200, response.text
            view = response.json()
            if not view["active"]:
                return view
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("body", [
    {"prompt": "x"},
    {"prompt": "x", "workspace_id": "w", "project_id": "p"},
    {"prompt": "x", "project_id": "p", "verifier_id": "v"},
    {"prompt": "x", "workspace_id": "w", "base_ref": "main"},
])
def test_task_sources_are_mutually_exclusive(body: dict):
    """拒绝 body 中未选择或混用项目/普通工作区的请求；返回 None，不触发 Git 或模型。"""
    with pytest.raises(ValidationError):
        CreateRunRequest.model_validate(body)


def test_project_options_probe_failure_does_not_break_ordinary_tasks(project_config: WebConfig, monkeypatch):
    """Git 目录失效只让该项目不可用，普通工作区提交仍正常；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")
    project_config.projects[0].path = project_config.project_dir / "missing"
    ordinary = project_config.project_dir / "ordinary"
    ordinary.mkdir()
    project_config.workspaces.append(WebWorkspace(id="ordinary", label="普通任务", path=ordinary))

    async def scenario():
        """检查选项受控错误并执行普通任务；项目探测不阻断原功能。"""
        async with client_for(project_config, ProjectModel(repair=False)) as (client, app):
            options = (await client.get("/api/options")).json()
            assert options["projects"][0]["available"] is False
            assert options["projects"][0]["error"]
            response = await client.post("/api/runs", json={"prompt": "普通任务", "workspace_id": "ordinary"})
            assert response.status_code == 202
            assert (await finished(client, response.json()["run_id"]))["project"] is None
    asyncio.run(scenario())


def test_project_config_resolves_relative_paths_and_fixed_program(project_config: WebConfig):
    """纯项目 TOML 可无普通工作区，相对目录和解释器入口在宿主边界固定；返回 None。"""
    location = project_config.project_dir / "web.toml"
    location.write_text(
        '[[projects]]\nid="backend"\nlabel="后端"\npath="source"\n'
        + 'test_argv=[' + json.dumps(sys.executable) + ',"tests/check.py"]\n', encoding="utf-8",
    )
    loaded = load_web_config(location, project_dir=project_config.project_dir)
    assert loaded.workspaces == []
    assert loaded.projects[0].path == project_config.projects[0].path
    assert Path(loaded.projects[0].test_argv[0]).is_absolute()
    location.write_text('workspaces=[]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_web_config(location, project_dir=project_config.project_dir)


def test_missing_key_prevents_git_worktree_creation(project_config: WebConfig, monkeypatch):
    """密钥缺失的提交在工作树创建前失败，源目录与登记均不变化；返回 None。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    before = git(project_config.projects[0].path, "worktree", "list", "--porcelain")

    async def scenario():
        """提交无密钥任务并检查受控400响应，不产生后台任务。"""
        async with client_for(project_config) as (client, app):
            response = await client.post("/api/runs", json={"prompt": "修复", "project_id": "backend"})
            assert response.status_code == 400
            assert app.state.manager.busy_run_id is None
    asyncio.run(scenario())
    assert git(project_config.projects[0].path, "worktree", "list", "--porcelain") == before


def test_real_tools_project_test_diff_and_keep_discard(project_config: WebConfig, monkeypatch):
    """真实写文件修复基线，测试加载副本，新增文件入补丁，保留/丢弃不碰 dirty 源目录；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")
    source = project_config.projects[0].path
    before_status = git(source, "status", "--porcelain=v1")
    before_index = (source / ".git" / "index").read_bytes()
    before_code = (source / "src" / "app.py").read_bytes()

    async def scenario():
        """完整走提交、项目检查、diff、下载、保留和丢弃；检查快照在删除后仍可下载。"""
        async with client_for(project_config) as (client, app):
            option = (await client.get("/api/options")).json()["projects"][0]
            assert option["source_dirty"] is True and option["available"] is True
            created = await start(client)
            identifier = created["run_id"]
            view = await finished(client, identifier)
            assert view["result"]["execution_status"] == "completed"
            assert view["result"]["validation_status"] == "passed", view
            review = view["project"]
            assert review["source_dirty"] is True and review["snapshot_status"] == "ready"
            worktree = Path(review["worktree"])
            assert not (worktree / "untracked.txt").exists()
            assert str(worktree) in view["result"]["verification"]["stdout"]
            assert {item["path"] for item in review["files"]} >= {"src/app.py", "NEW.txt"}
            diff = await client.get(f"/api/runs/{identifier}/diff")
            assert diff.status_code == 200
            assert "NEW.txt" in diff.json()["content"]
            patch = await client.get(f"/api/runs/{identifier}/patch")
            assert patch.status_code == 200 and "attachment" in patch.headers["content-disposition"]
            assert diff.json()["size_bytes"] == len(patch.content)
            kept = await client.post(f"/api/runs/{identifier}/review", json={"decision": "keep"})
            assert kept.status_code == 200 and kept.json()["status"] == "kept"
            assert worktree.exists()
            discarded = await client.post(f"/api/runs/{identifier}/review", json={"decision": "discard"})
            assert discarded.status_code == 200, discarded.text
            assert discarded.json()["status"] == "discarded" and not worktree.exists()
            assert (await client.get(f"/api/runs/{identifier}/patch")).content == patch.content
            again = await client.post(f"/api/runs/{identifier}/review", json={"decision": "discard"})
            assert again.status_code == 200
    asyncio.run(scenario())
    assert (source / "src" / "app.py").read_bytes() == before_code
    assert (source / ".git" / "index").read_bytes() == before_index
    assert git(source, "status", "--porcelain=v1") == before_status


def test_failed_project_check_is_not_reported_as_success(project_config: WebConfig, monkeypatch):
    """模型仅回答时原始错误仍使真实项目测试失败，执行完成与测试失败分别呈现；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")

    async def scenario():
        """使用不修复模型完成任务，读取失败检查与可用空 diff 快照。"""
        async with client_for(project_config, ProjectModel(repair=False)) as (client, app):
            created = await start(client)
            view = await finished(client, created["run_id"])
            assert view["result"]["execution_status"] == "completed"
            assert view["result"]["validation_status"] == "failed"
            assert view["result"]["verification"]["exit_code"] == 1
            assert view["project"]["snapshot_status"] == "ready"
    asyncio.run(scenario())


def test_cancel_then_restart_keeps_partial_diff_reviewable(project_config: WebConfig, monkeypatch):
    """真实写入后取消任务，部分变更仍可审阅；新服务读取相同补丁与决定，不恢复模型；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")

    async def scenario():
        """等待第二模型轮次后验证活动保护并取消，再重新构造应用读历史。"""
        model = ProjectModel(gated=True)
        async with client_for(project_config, model) as (client, app):
            created = await start(client)
            identifier = created["run_id"]
            await asyncio.wait_for(model.entered.wait(), 5)
            for suffix in ("review", "diff", "patch"):
                assert (await client.get(f"/api/runs/{identifier}/{suffix}")).status_code == 409
            assert (await client.post(f"/api/runs/{identifier}/review", json={"decision": "discard"})).status_code == 409
            assert (await client.post("/api/runs", json={"prompt": "第二项", "project_id": "backend"})).status_code == 409
            await client.post(f"/api/runs/{identifier}/cancel")
            view = await finished(client, identifier)
            assert view["result"]["execution_status"] == "cancelled"
            assert view["project"]["snapshot_status"] == "ready"
            patch = (await client.get(f"/api/runs/{identifier}/patch")).content
            assert b"NEW.txt" in patch
        async with client_for(project_config, ProjectModel(repair=False)) as (client, app):
            historical = (await client.get(f"/api/runs/{identifier}")).json()
            assert historical["active"] is False and historical["project"]["status"] == "pending"
            assert (await client.get(f"/api/runs/{identifier}/patch")).content == patch
            kept = await client.post(f"/api/runs/{identifier}/review", json={"decision": "keep"})
            assert kept.status_code == 200
    asyncio.run(scenario())


def test_snapshot_keeps_slot_and_cannot_be_cancelled_mid_capture(project_config: WebConfig, monkeypatch):
    """固化快照期间仍持有单槽，取消不会打断捕获线程并提前标记可审阅；返回 None。"""
    import threading
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")

    async def scenario():
        """在线程门闩处阻塞真实capture，检查active/phase/拒绝并发，然后放行。"""
        entered, release = threading.Event(), threading.Event()
        async with client_for(project_config, ProjectModel(repair=False)) as (client, app):
            original = app.state.manager.projects.capture

            def capture(identifier):
                """通知已进入真实快照并等待测试释放；最多五秒，之后调用原capture返回审阅。"""
                entered.set()
                assert release.wait(5)
                return original(identifier)
            monkeypatch.setattr(app.state.manager.projects, "capture", capture)
            created = await start(client)
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                identifier = created["run_id"]
                view = (await client.get(f"/api/runs/{identifier}")).json()
                assert view["active"] is True and view["phase"] == "project_snapshot"
                await client.post(f"/api/runs/{identifier}/cancel")
                assert (await client.get(f"/api/runs/{identifier}")).json()["cancelling"] is False
                response = await client.post("/api/runs", json={"prompt": "第二项", "project_id": "backend"})
                assert response.status_code == 409
            finally:
                release.set()
            assert (await finished(client, created["run_id"]))["project"]["snapshot_status"] == "ready"
    asyncio.run(scenario())


def test_request_disconnect_during_prepare_remains_service_owned(project_config: WebConfig, monkeypatch):
    """请求在 Git 准备中取消后，服务仍保存运行与快照，不遗留无法查询的工作树；返回 None。"""
    import threading
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")

    async def scenario():
        """线程门闩模拟慢Git，取消请求协程后继续后台运行，再从历史列表查询结果。"""
        entered, release = threading.Event(), threading.Event()
        async with client_for(project_config, ProjectModel(repair=False)) as (client, app):
            original = app.state.manager.projects.prepare

            def prepare(*arguments):
                """阻塞已开始的宿主准备，释放后执行真实创建并返回审阅；最多等待五秒。"""
                entered.set()
                assert release.wait(5)
                return original(*arguments)
            monkeypatch.setattr(app.state.manager.projects, "prepare", prepare)
            request = asyncio.create_task(client.post("/api/runs", json={"prompt": "修复", "project_id": "backend"}))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            finally:
                release.set()
            await asyncio.wait_for(asyncio.shield(app.state.manager._submission), 10)
            await asyncio.wait_for(app.state.manager.wait_idle(), 10)
            listed = (await client.get("/api/runs")).json()
            assert listed["total"] == 1
            assert listed["items"][0]["project"]["snapshot_status"] == "ready"
            assert listed["items"][0]["active"] is False
    asyncio.run(scenario())


def test_shutdown_during_prepare_creates_cancel_record_without_model(project_config: WebConfig, monkeypatch):
    """停服等待 Git 准备并生成可信取消终态与审阅快照，模型零调用，无遗失工作树；返回 None。"""
    import threading
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")

    async def scenario():
        """在prepare阻塞期间停服，等待两者收尾后对照模型次数与真实RunStore记录。"""
        entered, release = threading.Event(), threading.Event()
        model = ProjectModel(repair=False)
        async with client_for(project_config, model) as (client, app):
            original = app.state.manager.projects.prepare

            def prepare(*arguments):
                """通知慢准备已开始，测试释放后执行真实创建；五秒超时避免停服死锁。"""
                entered.set()
                assert release.wait(5)
                return original(*arguments)
            monkeypatch.setattr(app.state.manager.projects, "prepare", prepare)
            request = asyncio.create_task(client.post("/api/runs", json={"prompt": "修复", "project_id": "backend"}))
            assert await asyncio.to_thread(entered.wait, 5)
            shutdown = asyncio.create_task(app.state.manager.shutdown())
            try:
                await asyncio.sleep(0)
                assert app.state.manager._closing is True
            finally:
                release.set()
            response = await asyncio.wait_for(request, 10)
            assert response.status_code == 202, response.text
            await asyncio.wait_for(shutdown, 10)
            view = (await client.get("/api/runs/" + response.json()["run_id"])).json()
            assert model.calls == 0
            assert view["result"]["execution_status"] == "cancelled"
            assert view["result"]["stop_reason"] == "service_shutdown"
            assert view["project"]["snapshot_status"] == "ready"
            assert view["active"] is False
    asyncio.run(scenario())


def test_runner_start_failure_cleans_registered_worktree(project_config: WebConfig, monkeypatch):
    """Runner 建立记录前失败，已登记且未执行工具的工作树受控移除，源项目不受影响；返回 None。"""
    from agent_harness import web
    from agent_harness.store import StorageError
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")

    async def fail_before_context(*arguments, **keywords):
        """模拟宿主记录目录不可写；arguments/keywords 不读取，直接抛StorageError且未执行进程。"""
        raise StorageError("offline startup failure")
    monkeypatch.setattr(web.service, "run_task", fail_before_context)

    async def scenario():
        """提交失败后等待后台完整收尾，Git登记中只剩源目录。"""
        async with client_for(project_config) as (client, app):
            response = await client.post("/api/runs", json={"prompt": "修复", "project_id": "backend"})
            assert response.status_code == 503
            await app.state.manager.wait_idle()
            registered = git(project_config.projects[0].path, "worktree", "list", "--porcelain")
            assert registered.count(b"worktree ") == 1
    asyncio.run(scenario())


def test_broken_git_identity_keeps_snapshot_error_and_refuses_delete(project_config: WebConfig, monkeypatch):
    """工作树身份文件被修改后，快照明确error且不提供补丁或删除动作；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")

    async def scenario():
        """工具写入完成后改动工作树.git并取消，检查审阅失败事实与防误删保护。"""
        model = ProjectModel(gated=True)
        async with client_for(project_config, model) as (client, app):
            created = await start(client)
            await asyncio.wait_for(model.entered.wait(), 5)
            worktree = Path(created["project"]["worktree"])
            # Windows 隐藏文件不能用创建/截断模式打开，原位改写用于模拟外部身份破坏。
            with (worktree / ".git").open("r+b") as git_file:
                git_file.write(b"gitdir: does-not-exist")
                git_file.truncate()
            await client.post(f"/api/runs/{created['run_id']}/cancel")
            view = await finished(client, created["run_id"])
            assert view["project"]["snapshot_status"] == "error"
            assert view["project"]["patch_available"] is False
            assert (await client.get(f"/api/runs/{created['run_id']}/patch")).status_code == 409
            assert (await client.post(f"/api/runs/{created['run_id']}/review", json={"decision": "discard"})).status_code == 409
            assert worktree.exists()
    asyncio.run(scenario())


def test_large_diff_preview_is_bounded_but_patch_is_complete(project_config: WebConfig, monkeypatch):
    """大文本diff按256KiB展示且可下载完整补丁，预览不以截断快照冒充完整结果；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")

    async def scenario():
        """在模型等待期间扩大新文件，再完成真实测试并比较预览与完整下载字节数。"""
        model = ProjectModel(gated=True)
        async with client_for(project_config, model) as (client, app):
            created = await start(client)
            await asyncio.wait_for(model.entered.wait(), 5)
            worktree = Path(created["project"]["worktree"])
            (worktree / "NEW.txt").write_text("中文审阅行\n" * 40000, encoding="utf-8")
            model.release.set()
            view = await finished(client, created["run_id"])
            assert view["project"]["snapshot_status"] == "ready"
            endpoint = f"/api/runs/{created['run_id']}"
            preview = (await client.get(endpoint + "/diff")).json()
            patch = (await client.get(endpoint + "/patch")).content
            assert preview["truncated"] is True
            assert len(preview["content"].encode("utf-8")) <= 262144
            assert len(patch) == preview["size_bytes"] > 262144
            assert "中文审阅行" in patch.decode("utf-8")
    asyncio.run(scenario())


def test_restarted_interrupted_run_can_capture_once_but_not_delete(project_config: WebConfig, monkeypatch):
    """重启遗留的未终态项目可补捕获审阅，但未知进程清理时禁止删除；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")

    async def scenario():
        """先真实执行，再模拟磁盘只保留终态前缀及pending快照，重启后检查一次恢复和删除保护。"""
        async with client_for(project_config) as (client, app):
            created = await start(client)
            await finished(client, created["run_id"])
        identifier = created["run_id"]
        directory = project_config.project_dir / "records" / identifier
        events = directory / "events.jsonl"
        rows = events.read_text(encoding="utf-8").splitlines()
        assert json.loads(rows[-1])["type"] == "run_finished"
        events.write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")
        (directory / "summary.json").unlink()
        review_path = directory.parent / ".projects" / identifier / "review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review.update(snapshot_status="pending", patch_available=False)
        review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        async with client_for(project_config, ProjectModel(repair=False)) as (client, app):
            captured = await client.get(f"/api/runs/{identifier}/review")
            assert captured.status_code == 200 and captured.json()["snapshot_status"] == "ready"
            assert UUID(identifier) in app.state.manager._recovered
            current = (await client.get(f"/api/runs/{identifier}")).json()
            assert current["interrupted"] is True and current["active"] is False
            refused = await client.post(f"/api/runs/{identifier}/review", json={"decision": "discard"})
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "project_cleanup_unconfirmed"
            assert Path(current["project"]["worktree"]).exists()
    asyncio.run(scenario())


def test_post_worktree_preflight_failure_removes_unstarted_copy(project_config: WebConfig, monkeypatch):
    """创建副本后的二次路径校验失败时清理已登记工作树，不启动模型也不遗留孤立副本；返回 None。"""
    from agent_harness import web
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")
    original = web.service.validate_output_paths
    calls = 0

    def validate(task, root):
        """首次源目录预检沿用真实实现，第二次工作树预检模拟受控错误；返回 None 或抛配置错误。"""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConfigurationError("offline path changed")
        return original(task, root)
    monkeypatch.setattr(web.service, "validate_output_paths", validate)

    async def scenario():
        """提交到副本创建后失败，检查400、模型零调用和源目录唯一Git登记。"""
        model = ProjectModel()
        async with client_for(project_config, model) as (client, app):
            response = await client.post("/api/runs", json={"prompt": "修复", "project_id": "backend"})
            assert response.status_code == 400
            assert model.calls == 0
            registered = git(project_config.projects[0].path, "worktree", "list", "--porcelain")
            assert registered.count(b"worktree ") == 1
    asyncio.run(scenario())


def test_concurrent_review_requests_wait_for_same_recovery(project_config: WebConfig, monkeypatch):
    """两个页面同时恢复pending快照时共用任务，第二请求不提前缓存pending；返回 None。"""
    import threading
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-key")

    async def scenario():
        """制造历史pending并阻塞一次capture，两个GET均等待同一真实完成结果。"""
        async with client_for(project_config, ProjectModel(repair=False)) as (client, app):
            created = await start(client)
            await finished(client, created["run_id"])
        identifier = created["run_id"]
        record = project_config.project_dir / "records" / ".projects" / identifier / "review.json"
        stored = json.loads(record.read_text(encoding="utf-8"))
        stored.update(snapshot_status="pending", patch_available=False)
        record.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        entered, release = threading.Event(), threading.Event()
        count = 0
        async with client_for(project_config, ProjectModel(repair=False)) as (client, app):
            original = app.state.manager.projects.capture

            def capture(run_id):
                """记录恢复次数，等待测试放行后执行真实capture；五秒超时防止并发测试挂起。"""
                nonlocal count
                count += 1
                entered.set()
                assert release.wait(5)
                return original(run_id)
            monkeypatch.setattr(app.state.manager.projects, "capture", capture)
            endpoint = f"/api/runs/{identifier}/review"
            first = asyncio.create_task(client.get(endpoint))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                second = asyncio.create_task(client.get(endpoint))
                await asyncio.sleep(0.03)
                assert not first.done() and not second.done()
                assert count == 1
            finally:
                release.set()
            responses = await asyncio.wait_for(asyncio.gather(first, second), 10)
            assert [response.status_code for response in responses] == [200, 200]
            assert all(response.json()["snapshot_status"] == "ready" for response in responses)
            assert count == 1
    asyncio.run(scenario())
