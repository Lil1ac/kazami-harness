"""通过生产 API 验证模型命令、宿主测试、多轮和独立工作树使用当轮源码。"""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest

from agent_harness.projects.models import ProjectPreset
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


PROBE = (
    "import agent_harness,json,os,sys;"
    "print(json.dumps({'source':agent_harness.__file__,'value':agent_harness.VALUE,"
    "'has_key':'DEEPSEEK_API_KEY' in os.environ,'executable':sys.executable}))"
)  # 普通 Python 导入，不插入 sys.path 或 pytest pythonpath 参数。


class ImportModel:
    """首轮和续轮都先读取继承值，再真实修改源码并通过命令核对新值。"""

    def __init__(self) -> None:
        """初始化真实观察列表；不连接模型服务或修改文件。"""
        self.calls = 0  # 统计所有模型调用，核对配置失败时完全未调用模型。
        self.observations: list[dict] = []  # 每轮修改前/后的导入路径与源码值。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """按本次用户请求后的工具结果推进；tools 来自真实执行器，返回标准模型协议对象。"""
        self.calls += 1
        latest = max(index for index, message in enumerate(messages) if message["role"] == "user")
        observations = [json.loads(message["content"]) for message in messages[latest + 1:] if message["role"] == "tool"]
        stage = len(observations)
        if stage in {1, 3}:
            observed = observations[-1]
            assert observed["ok"], observed
            self.observations.append(json.loads(observed["data"]["stdout"]))
        if stage == 1:
            value = "first-change" if messages[latest]["content"] == "first" else "second-change-expanded"
            name, arguments = "write_file", {"path": "src/agent_harness/__init__.py", "content": f"VALUE = {value!r}\n", "overwrite": True}
        elif stage in {0, 2}:
            name, arguments = "run_command", {"argv": ["python", "-c", PROBE], "cwd": "tests"}
        else:
            return ModelResponse(message={"role": "assistant", "content": "已核对继承代码和测试结果。"}, tool_calls=[], finish_reason="stop", response_model="offline-source-paths")
        call = ToolCall(uuid4().hex, name, json.dumps(arguments))
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}},
        ]}
        return ModelResponse(message=message, tool_calls=[call], finish_reason="tool_calls", response_model="offline-source-paths")


def configuration(tmp_path: Path, *, verified: bool = True, kind: str = "project") -> WebConfig:
    """在 tmp_path 创建真实源码与可选 Git 基线，返回生产配置；verified 决定是否登记宿主测试。"""
    selected = tmp_path / "selected"
    (selected / "src/agent_harness").mkdir(parents=True)
    (selected / "src/agent_harness/__init__.py").write_text("VALUE = 'baseline'\n", encoding="utf-8")
    (selected / "tests").mkdir()
    (selected / "tests/README.md").write_text("保留子目录供普通命令执行。\n", encoding="utf-8")
    (selected / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    if kind == "project":
        for arguments in (["init", "-q"], ["config", "core.autocrlf", "false"], ["add", "."],
                          ["-c", "user.name=HarnessTest", "-c", "user.email=test@localhost", "commit", "-qm", "source baseline"]):
            subprocess.run(["git", *arguments], cwd=selected, capture_output=True, check=True, timeout=10)
    check = (
        "from pathlib import Path; import agent_harness,os;"
        "assert Path(agent_harness.__file__).resolve().is_relative_to(Path.cwd()/'src');"
        "assert agent_harness.VALUE in {'first-change','second-change-expanded'};"
        "assert 'DEEPSEEK_API_KEY' not in os.environ; print(agent_harness.__file__)"
    )
    return WebConfig(
        project_dir=tmp_path, worktrees_root=tmp_path / "copies", frontend_dist=tmp_path / "dist",
        projects=[ProjectPreset(id="code", label="导入路径项目", path=selected,
                                test_argv=[sys.executable, "-c", check] if verified else [])] if kind == "project" else [],
        workspaces=[WebWorkspace(id="folder", label="普通目录", path=selected)] if kind == "workspace" else [],
    )


@asynccontextmanager
async def client_for(config: WebConfig, model: ImportModel):
    """以 config/model 打开真实 API 及生命周期，产出客户端和管理器，结束时清理后台任务。"""
    app = create_app(config, RunConfig(tools=["write_file", "run_command"]), config.project_dir / "runs", model_client=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client, app.state.manager


async def finish(client: httpx.AsyncClient, response: httpx.Response) -> dict:
    """等待已接纳 response 的真实运行完成及快照保存，返回视图；超过20秒失败以避免假通过。"""
    assert response.status_code == 202, response.text
    identifier = response.json()["run_id"]
    async with asyncio.timeout(20):
        while True:
            view = (await client.get(f"/api/runs/{identifier}")).json()
            if not view["active"]:
                return view
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("mode", ["local", "isolated"])
def test_followups_share_current_sources_and_preserve_old_snapshot(tmp_path: Path, monkeypatch, mode: str) -> None:
    """mode 决定原目录或工作树；两轮命令/验收都使用该轮路径，旧运行快照原字节保持。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-source-paths")
    config = configuration(tmp_path)
    model = ImportModel()

    async def scenario() -> None:
        """提交首轮及真实追加，并核对配置、导入结果与旧记录不受新工作树影响。"""
        async with client_for(config, model) as (client, manager):
            first = await finish(client, await client.post("/api/runs", json={"prompt": "first", "project_id": "code", "base_ref": "HEAD", "execution_mode": mode}))
            assert first["result"]["validation_status"] == "passed", first["result"]
            first_root = Path(first["workspace"])
            expected = [str(first_root), str(first_root / "src")]
            assert first["python_execution"]["python_paths"] == expected
            manifest = manager.runs_root / first["run_id"] / "run.json"
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            second = await finish(client, await client.post(f"/api/runs/{first['run_id']}/followups", json={"request_id": str(uuid4()), "prompt": "second"}))
            assert second["result"]["validation_status"] == "passed", second["result"]
            second_root = Path(second["workspace"])
            assert second_root == first_root  # 原目录和隔离会话均持续复用各自工作区。
            assert second["python_execution"]["python_paths"] == [str(second_root), str(second_root / "src")]
            assert [item["value"] for item in model.observations] == ["baseline", "first-change", "first-change", "second-change-expanded"]
            assert all(Path(item["source"]).is_relative_to(first_root if index < 2 else second_root) and not item["has_key"] for index, item in enumerate(model.observations))
            assert hashlib.sha256(manifest.read_bytes()).hexdigest() == digest
            old = (await client.get(f"/api/runs/{first['run_id']}")).json()
            assert old["python_execution"] == first["python_execution"]
            assert second["parent_run_id"] == first["run_id"]

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["project", "workspace"])
def test_no_verifier_still_uses_selected_source(tmp_path: Path, monkeypatch, kind: str) -> None:
    """kind 为 Git 或普通目录；未登记验收也应使用所选源码，结果如实保持未验收。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-source-paths")
    config = configuration(tmp_path, verified=False, kind=kind)
    model = ImportModel()

    async def scenario() -> None:
        """通过真实提交链路验证无验收时的源码路径与命令观察。"""
        async with client_for(config, model) as (client, _):
            source = {"project_id": "code", "base_ref": "HEAD"} if kind == "project" else {"workspace_id": "folder"}
            view = await finish(client, await client.post("/api/runs", json={"prompt": "first", **source}))
            assert view["result"]["execution_status"] == "completed"
            assert view["result"]["validation_status"] == "unverified"
            assert all(Path(item["source"]).is_relative_to(Path(view["workspace"])) for item in model.observations)

    asyncio.run(scenario())


def test_invalid_project_source_config_rejects_before_model(tmp_path: Path, monkeypatch) -> None:
    """显式配置缺失源码目录时，模型未调用且不建立运行或隐藏回退路径。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-source-paths")
    config = configuration(tmp_path)
    config.projects[0].python_paths = ["missing"]
    model = ImportModel()

    async def scenario() -> None:
        """核对启动错误为受控响应，真实模型夹具没有收到任何执行观察。"""
        async with client_for(config, model) as (client, manager):
            response = await client.post("/api/runs", json={"prompt": "first", "project_id": "code"})
            assert response.status_code == 400, response.text
            assert model.observations == []
            assert not list(manager.runs_root.glob("*/run.json"))

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["project", "workspace"])
def test_project_settings_select_real_python_and_survive_restart(tmp_path: Path, monkeypatch, kind: str) -> None:
    """kind 对应 Git/普通目录；保存另一真实解释器不运行，实际工具与项目验收使用它，重启仍保存。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-project-execution")
    config = configuration(tmp_path, kind=kind)
    environment = tmp_path / "selected-python"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True, capture_output=True)
    executable = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    identifier = "code" if kind == "project" else "folder"
    model = ImportModel()

    async def scenario() -> None:
        """经生产设置 API 保存并提交任务，再重启服务读取同一登记和固定快照。"""
        async with client_for(config, model) as (client, manager):
            check = "import sys; assert sys.executable == " + repr(str(executable)) + "; print(sys.executable)"
            command = subprocess.list2cmdline(["python", "-c", check]) if kind == "project" else ""
            response = await client.put(f"/api/projects/{identifier}/settings", json={
                "python_executable": str(executable), "test_command": command, "test_timeout_seconds": 30,
            })
            assert response.status_code == 200, response.text
            assert response.json()["python_executable"] == str(executable)
            assert model.calls == 0 and not list(manager.runs_root.glob("*/run.json"))
            payload = {"prompt": "first", "project_id" if kind == "project" else "workspace_id": identifier}
            view = await finish(client, await client.post("/api/runs", json=payload))
            assert view["python_execution"]["executable"] == str(executable)
            assert all(item["executable"] == str(executable) for item in model.observations)
            assert view["result"]["validation_status"] == ("passed" if kind == "project" else "unverified")
            if kind == "project":
                assert view["result"]["verification"]["stdout"].strip() == str(executable)
            run_id = view["run_id"]
        async with client_for(config, ImportModel()) as (client, _):
            options = (await client.get("/api/options")).json()
            option = next(item for item in options["recent_projects"] if item["id"] == identifier)
            assert option["python_executable"] == str(executable)
            assert (await client.get(f"/api/runs/{run_id}")).json()["python_execution"] == view["python_execution"]
    asyncio.run(scenario())


def test_interpreter_disappears_before_followup_preserves_shared_workspace(tmp_path: Path, monkeypatch) -> None:
    """有效解释器在首轮后消失时，新轮在模型前拒绝并还原管理状态；恢复入口后可正常续聊。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-missing-interpreter")
    config = configuration(tmp_path, verified=False)
    environment = tmp_path / "project-python"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True, capture_output=True)
    executable = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    config.projects[0].python_executable = str(executable)
    model = ImportModel()

    async def scenario() -> None:
        """验证失败不删除会话目录、不调用模型、不污染旧证据，随后使用新请求编号继续。"""
        async with client_for(config, model) as (client, manager):
            first = await finish(client, await client.post("/api/runs", json={"project_id":"code","execution_mode":"isolated","prompt":"first"}))
            root = Path(first["workspace"])
            saved = (manager.runs_root / first["run_id"] / "run.json").read_bytes()
            count = model.calls
            backup = executable.with_name(executable.name + ".disabled")
            executable.rename(backup)
            try:
                response = await client.post(f'/api/runs/{first["run_id"]}/followups', json={"request_id":str(uuid4()),"prompt":"second"})
                assert response.status_code == 409, response.text
                assert model.calls == count and root.is_dir()
                state = manager.projects.workspace(__import__("uuid").UUID(first["run_id"]))
                assert str(state.latest_run_id) == first["run_id"]
                assert (manager.runs_root / first["run_id"] / "run.json").read_bytes() == saved
            finally:
                backup.rename(executable)
            second = await finish(client, await client.post(f'/api/runs/{first["run_id"]}/followups', json={"request_id":str(uuid4()),"prompt":"second"}))
            assert second["workspace"] == first["workspace"]
            assert second["result"]["execution_status"] == "completed"
    asyncio.run(scenario())
