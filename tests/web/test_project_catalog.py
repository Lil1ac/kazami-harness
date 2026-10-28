"""验证本机目录登记、原生选择的API边界、持久化及未配置测试时的真实运行。"""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig
from agent_harness.web.project_catalog import ProjectCatalog, CatalogError


def repository(path: Path, *, commit: bool = True) -> Path:
    """在path创建独立Git测试项目；commit=False保留尚无提交状态，返回根目录。"""
    path.mkdir(parents=True)
    (path / "app.py").write_text("value = 1\n", encoding="utf-8")
    for args in (["init", "-q"], ["config", "core.autocrlf", "false"], ["add", "."]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    if commit:
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@invalid",
                        "commit", "-qm", "baseline"], cwd=path, check=True, capture_output=True)
    return path


def configuration(path: Path) -> WebConfig:
    """构造无预设的首次启动配置；返回模型，不创建目录或执行程序。"""
    return WebConfig(project_dir=path, worktrees_root=path / "copies", frontend_dist=path / "dist")


class EditingModel:
    """仅替换供应商，保留真实Runner、文件工具、Git与项目验收。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """针对最新用户要求写入app.py一次，然后完成；不访问外网，返回供应商统一响应。"""
        last = max(index for index, item in enumerate(messages) if item["role"] == "user")
        used = any(item["role"] == "tool" for item in messages[last + 1:])
        calls = [] if used else [ToolCall(uuid4().hex, "write_file", json.dumps(
            {"path": "app.py", "content": "value = 2\n", "overwrite": True}))]
        message = {"role": "assistant", "content": "已更新。" if used else None}
        if calls:
            message["tool_calls"] = [{"id": call.call_id, "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json}} for call in calls]
        return ModelResponse(message=message, tool_calls=calls, finish_reason="stop" if used else "tool_calls", response_model="offline-open-project")


@asynccontextmanager
async def client_for(root: Path):
    """启动无项目预设的真实应用并产出客户端和manager，退出等待所有任务收尾。"""
    app = create_app(configuration(root), RunConfig(tools=["write_file"]), root / "runs", model_client=EditingModel())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://localhost", trust_env=False) as client:
            yield client, app.state.manager


async def finish(client: httpx.AsyncClient, identifier: str) -> dict:
    """最多等待15秒让identifier完成Runner及代码快照收尾，返回真实最终视图。"""
    async with asyncio.timeout(15):
        while True:
            view = (await client.get("/api/runs/" + identifier)).json()
            if not view["active"]:
                return view
            await asyncio.sleep(.01)


def test_directory_registration_reuses_id_and_survives_restart(tmp_path: Path):
    """中文目录重复打开只保存一个稳定ID；重启保留且GET最近列表不改文件。"""
    selected = repository(tmp_path / "真实 项目")
    config = configuration(tmp_path)
    catalog = ProjectCatalog(tmp_path / "runs", config)
    identifier = catalog.register(selected)
    before = catalog.path.read_bytes()
    assert catalog.recent()[0]["path"] == str(selected)
    assert catalog.path.read_bytes() == before
    assert ProjectCatalog(tmp_path / "runs", config).recent()[0]["id"] == identifier
    assert catalog.register(selected) == identifier
    assert len(catalog.recent()) == 1
    assert catalog.project_presets()[0].test_argv == []
    assert (selected / "app.py").read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.parametrize("kind,code", [("missing", "project_directory_unavailable"),
                                       ("file", "project_directory_unavailable"),
                                       ("subdirectory", "project_root_required"),
                                       ("bare", "project_bare_unsupported")])
def test_invalid_directories_do_not_create_registration(tmp_path: Path, kind: str, code: str):
    """无效、非根及裸仓库选项明确拒绝，目录登记文件不创建，也不修改源项目。"""
    selected = tmp_path / "selected"
    if kind == "file":
        selected.write_text("file", encoding="utf-8")
    elif kind == "subdirectory":
        selected = repository(selected) / "src"
        selected.mkdir()
    elif kind == "bare":
        subprocess.run(["git", "init", "--bare", "-q", str(selected)], check=True)
    catalog = ProjectCatalog(tmp_path / "runs", configuration(tmp_path))
    with pytest.raises(CatalogError) as captured:
        catalog.register(selected)
    assert captured.value.code == code
    assert not catalog.path.exists()


def test_picker_cancel_and_cross_origin_do_not_register(tmp_path: Path):
    """取消原生窗口不改目录、不创建任务；跨源请求不能弹出本机选择框。"""
    async def scenario():
        """通过真实ASGI路由核对取消与跨源状态，返回None。"""
        async with client_for(tmp_path) as (client, manager):
            calls = []
            async def cancel():
                """记录明确选择请求后返回取消，不访问文件。"""
                calls.append(True)
                return None
            manager.folder_picker.choose = cancel
            assert (await client.post("/api/projects/pick")).json() == {"cancelled": True, "project": None}
            assert (await client.post("/api/projects/pick", headers={"Origin": "https://other.invalid"})).status_code == 403
            assert len(calls) == 1 and not manager.catalog.path.exists()
            assert (await client.get("/api/runs")).json()["total"] == 0
    asyncio.run(scenario())


def test_open_git_without_tests_then_configure_and_continue_after_restart(tmp_path: Path, monkeypatch):
    """从选目录开始完整跑改码：无测试明确未验收，重启后保存测试并继续，真实测试通过。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-open-project")
    selected = repository(tmp_path / "个人项目")
    async def scenario():
        """核对目录选择、受限ID提交、旧记录不改、重启继承与测试设置，返回None。"""
        async with client_for(tmp_path) as (client, manager):
            async def choose():
                """模拟系统返回用户选定目录；API后续逻辑全部真实。"""
                return selected
            manager.folder_picker.choose = choose
            result = await client.post("/api/projects/pick")
            assert result.status_code == 200
            project = result.json()["project"]
            assert project["kind"] == "project" and project["available"] and project["test_argv"] == []
            assert project["branch"]
            assert (await client.get("/api/runs")).json()["total"] == 0
            response = await client.post("/api/runs", json={"project_id": project["id"], "prompt": "修改value", "execution_mode": "isolated"})
            assert response.status_code == 202, response.text
            first = await finish(client, response.json()["run_id"])
            assert first["result"]["execution_status"] == "completed"
            assert first["result"]["validation_status"] == "unverified"
            assert (selected / "app.py").read_text(encoding="utf-8") == "value = 1\n"
            assert (Path(first["workspace"]) / "app.py").read_text(encoding="utf-8") == "value = 2\n"
            saved = (manager.runs_root / first["run_id"] / "events.jsonl").read_bytes()
        async with client_for(tmp_path) as (client, manager):
            options = (await client.get("/api/options")).json()
            assert options["recent_projects"][0]["id"] == project["id"]
            opened = await client.post("/api/projects/" + project["id"] + "/open")
            assert opened.status_code == 200
            program = subprocess.list2cmdline([sys.executable, "-c", "import app; assert app.value == 2"]) if sys.platform == "win32" else __import__("shlex").join([sys.executable, "-c", "import app; assert app.value == 2"])
            response = await client.put("/api/projects/" + project["id"] + "/settings",
                json={"test_command": program, "test_timeout_seconds": 10.0})
            assert response.status_code == 200, response.text
            follow = await client.post("/api/runs/" + first["run_id"] + "/followups",
                json={"request_id":str(uuid4()), "prompt":"继续核对"})
            assert follow.status_code == 202, follow.text
            second = await finish(client, follow.json()["run_id"])
            assert second["result"]["validation_status"] == "passed"
            assert second["project"]["status"] == "pending"
            assert (manager.runs_root / first["run_id"] / "events.jsonl").read_bytes() == saved
            cleared = await client.put("/api/projects/" + project["id"] + "/settings", json={"test_command":""})
            assert cleared.status_code == 200 and cleared.json()["test_argv"] == []
            assert (await client.post("/api/runs",json={"prompt":"绕过目录","project_id":project["id"],"path":str(tmp_path)})).status_code == 422
    asyncio.run(scenario())


def test_plain_directory_and_unborn_git_have_distinct_modes(tmp_path: Path):
    """普通目录原地执行；无提交Git仍标记Git但不可执行，不静默改为普通模式。"""
    async def scenario():
        """打开两种目录并只读核对能力；返回None，不请求模型。"""
        ordinary = tmp_path / "文件目录"
        ordinary.mkdir()
        unborn = repository(tmp_path / "尚无提交", commit=False)
        async with client_for(tmp_path) as (client, manager):
            ordinary_id = manager.catalog.register(ordinary)
            unborn_id = manager.catalog.register(unborn)
            options = (await client.get("/api/options")).json()
            by_id = {item["id"]:item for item in options["recent_projects"]}
            assert by_id[ordinary_id]["kind"] == "workspace"
            assert by_id[unborn_id]["kind"] == "project" and by_id[unborn_id]["available"] is False
            assert next(item for item in options["workspaces"] if item["id"] == ordinary_id)["path"] == str(ordinary)
    asyncio.run(scenario())


def test_catalog_damage_does_not_get_overwritten(tmp_path: Path):
    """损坏最近项目文件时明确失败，保留原字节，避免静默清空用户列表。"""
    catalog = ProjectCatalog(tmp_path / "runs", configuration(tmp_path))
    catalog.root.mkdir(parents=True)
    catalog.path.write_bytes(b"{broken")
    with pytest.raises(CatalogError, match="无法读取"):
        catalog.recent()
    assert catalog.path.read_bytes() == b"{broken"


def test_same_directory_keeps_identity_when_git_kind_changes(tmp_path: Path):
    """普通配置目录初始化或移走Git元数据后仍复用ID，不写出同路径重复记录。"""
    from agent_harness.web.config import WebWorkspace

    selected = tmp_path / "kind-change"
    selected.mkdir()
    config = configuration(tmp_path).model_copy(update={
        "workspaces": [WebWorkspace(id="alpha", label="我的项目", path=selected)],
    })
    catalog = ProjectCatalog(tmp_path / "runs", config)
    assert catalog.register(selected) == "alpha"
    # 分离Git元数据后只移动.git指针文件；避免Windows后台索引占用目录影响类型切换验收。
    subprocess.run(["git", "init", "-q", "--separate-git-dir", str(tmp_path / "git-data"), str(selected)], check=True, capture_output=True)
    assert catalog.register(selected) == "alpha"
    assert catalog.workspace_presets() == []
    assert len(catalog.project_presets()) == 1
    assert catalog.recent()[0]["kind"] == "project"
    (selected / ".git").rename(selected / ".saved-git")
    assert catalog.reopen("alpha") == "alpha"
    assert catalog.project_presets() == []
    assert len(catalog.workspace_presets()) == 1
    restarted = ProjectCatalog(tmp_path / "runs", config)
    assert len(restarted.recent()) == 1 and restarted.recent()[0]["kind"] == "workspace"


def test_duplicate_catalog_write_preserves_previous_bytes(tmp_path: Path):
    """写入端与读取端共用唯一性校验，拒绝重复路径且不替换合法旧记录。"""
    selected = repository(tmp_path / "source")
    catalog = ProjectCatalog(tmp_path / "runs", configuration(tmp_path))
    catalog.register(selected)
    before = catalog.path.read_bytes()
    entry = catalog._read()[0]
    duplicate = entry.model_copy(update={"id": "another"})
    with pytest.raises(CatalogError) as captured:
        catalog._write([entry, duplicate])
    assert captured.value.code == "project_catalog_write_failed"
    assert catalog.path.read_bytes() == before


@pytest.mark.parametrize("argument", ['print("hello world")', '{"目录": "中文 space"}', '', 'C:/tail with space/'])
def test_test_command_round_trip_preserves_quoted_arguments(argument: str):
    """命令页面回显后重新解析，保留嵌套引号、空参数和中文，不经过Shell执行。"""
    import os
    import shlex
    from agent_harness.web.project_catalog import _split_test_command

    argv = [sys.executable, "-c", argument]
    shown = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    assert _split_test_command(shown) == argv


def test_options_remains_consistent_during_project_registration(tmp_path: Path, monkeypatch):
    """Git探测期间登记新目录时，本次选项属于旧快照，下次读取包含新项目且不返回500。"""
    first = repository(tmp_path / "first")
    second = repository(tmp_path / "second")
    app = create_app(configuration(tmp_path), runs_root=tmp_path / "runs")
    manager = app.state.manager
    initial = manager.catalog.register(first)
    inspect = manager._project_option
    added = []  # 在首次探测过程中模拟另一个浏览器标签的明确目录登记。

    def inspect_and_register(preset):
        """返回真实Git探测结果；首次调用时插入另一个真实登记以触发旧实现的身份错配。"""
        if not added:
            added.append(manager.catalog.register(second))
        return inspect(preset)

    monkeypatch.setattr(manager, "_project_option", inspect_and_register)
    current = manager.options()
    assert [item["id"] for item in current["recent_projects"]] == [initial]
    assert {item["id"] for item in manager.options()["recent_projects"]} == {initial, added[0]}


def test_plain_project_evidence_excludes_other_run_records(tmp_path: Path, monkeypatch):
    """在指定目录直接修改代码，逐轮证据排除记录树，不复制或改写其他会话。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-open-project")
    selected = tmp_path / "source"
    selected.mkdir()
    (selected / "app.py").write_text("value = 1\n", encoding="utf-8")
    records = selected / ".runs"
    private = records / "previous" / "events.jsonl"
    private.parent.mkdir(parents=True)
    private.write_text("private previous conversation", encoding="utf-8")

    async def scenario():
        """经真实API与Runner修改原文件，核对逐轮文件清单不包含记录树。"""
        app = create_app(configuration(tmp_path), RunConfig(tools=["write_file"]), records, model_client=EditingModel())
        manager = app.state.manager
        identifier = manager.catalog.register(selected)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", trust_env=False) as client:
                created = await client.post("/api/runs", json={"workspace_id": identifier, "prompt": "修改value"})
                assert created.status_code == 202, created.text
                result = await finish(client, created.json()["run_id"])
                copied = Path(result["workspace"])
                assert result["result"]["execution_status"] == "completed"
                assert (copied / "app.py").read_text(encoding="utf-8") == "value = 2\n"
                assert copied == selected
                assert all(not item['path'].startswith('.runs/') for item in result['local_changes']['files'])
                assert private.read_text(encoding="utf-8") == "private previous conversation"
                assert (selected / "app.py").read_text(encoding="utf-8") == "value = 2\n"

    asyncio.run(scenario())
