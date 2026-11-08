"""验证 Electron 后台的公开设置、认证边界、标准流原生请求和父进程失联清理。"""

from tests.web.job_helpers import submit_run, submit_followup

import asyncio
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import httpx
import pytest

from agent_harness.config import ConfigurationError
from agent_harness.context.builder import ContextBuilder
from agent_harness.desktop.bridge import NativeBridge
from agent_harness.desktop.host import DesktopHost
from agent_harness.desktop.storage import DesktopRunSettings, PreferenceStore, resource_root
from agent_harness.schemas import PythonExecution, RunConfig, TaskSpec
from agent_harness.storage.runs import RunStore
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig
from agent_harness.execution.workspace import Workspace


@pytest.mark.parametrize("state", ["configured", "missing", "unprepared"])
def test_model_uses_project_interpreter_instead_of_frozen_host(tmp_path: Path, monkeypatch, state: str) -> None:
    """state 分别模拟已配置、未找到及未准备解释器；提示与执行快照一致，不能推荐宿主 EXE。"""
    frozen_host = tmp_path / "harness-backend.exe"
    interpreter = tmp_path / "project-python.exe"
    monkeypatch.setattr(sys, "executable", str(frozen_host))
    execution = None if state == "unprepared" else PythonExecution(
        workspace=tmp_path, executable=interpreter if state == "configured" else None, python_paths=[tmp_path],
    )
    task = TaskSpec(prompt="检查项目", workspace=tmp_path, python_execution=execution)
    message = ContextBuilder(Workspace(tmp_path, tmp_path / "runs"), task, RunConfig()).initial_messages()[0]["content"]
    assert str(frozen_host) not in message
    if state == "configured":
        assert str(interpreter) in message
    else:
        assert str(interpreter) not in message
        assert ("python 别名不可用" if state == "missing" else "解释器尚未准备") in message


class NativeMock:
    """模拟 Electron 请求结果；业务服务、认证和设置保存仍走真实实现。"""

    def __init__(self, selected: Path) -> None:
        """固定 selected 目录；初始化空请求记录和可用状态，不创建原生窗口。"""
        self.selected = selected  # 本例唯一允许返回的目录。
        self.calls: list[tuple[str, dict]] = []  # 原生请求，仅用于断言，不写入产品记录。
        self.failed = False  # 显式模拟加密或父进程故障。

    async def request(self, method: str, parameters: dict) -> object:
        """记录 method/parameters 并返回测试结果；failed=True 抛受控故障，不改公开设置。"""
        self.calls.append((method, parameters))
        if self.failed:
            raise ConfigurationError("原生请求失败")
        return str(self.selected) if method == "folder.choose" else None


def test_public_settings_survive_restart_without_credentials(tmp_path: Path, monkeypatch) -> None:
    """保存模型和下载目录后跨启动保留；资源定位不依赖当前目录，密钥不能混入配置。"""
    store = PreferenceStore(tmp_path)
    settings = DesktopRunSettings(provider="qwen", requested_model="desktop-model", tools=["read_file"], limits={"max_model_turns": 7, "max_tool_calls": 16, "run_timeout_seconds": 60.0})
    store.update(config=settings, downloads_directory=tmp_path / "downloads")
    loaded = PreferenceStore(tmp_path)
    assert loaded.value.config.provider == "qwen"
    assert loaded.value.config.requested_model == "desktop-model"
    assert loaded.value.downloads_directory == tmp_path / "downloads"
    assert "secret" not in store.path.read_text(encoding="utf-8")
    expected = resource_root()
    monkeypatch.chdir(tmp_path)
    assert resource_root() == expected
    store.path.write_text("corrupt", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        PreferenceStore(tmp_path)
    assert store.path.read_text(encoding="utf-8") == "corrupt"


def test_desktop_session_settings_and_native_credential_failures(tmp_path: Path) -> None:
    """桌面接口要求私有 Cookie/令牌；原生加密失败不污染内存密钥，公开响应不泄密。"""
    async def scenario() -> None:
        """通过真实 HTTP 中间件保存设置并模拟凭据故障；所有记录只包含存在状态。"""
        native = NativeMock(tmp_path)
        host = DesktopHost(PreferenceStore(tmp_path / "data"), native, "a" * 64, {"deepseek": "desktop-test-secret", "qwen": "qwen-test-secret"})
        web = WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=tmp_path / "dist")
        app = create_app(web, RunConfig(), tmp_path / "runs", desktop=host)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                assert (await client.get("/api/options")).status_code == 403
                client.cookies.set(host.cookie_name, host.token)
                options = await client.get("/api/options")
                assert options.status_code == 200 and "script-src 'self'" in options.headers["content-security-policy"]
                assert host.token not in options.text and "desktop-test-secret" not in options.text
                assert options.json()["desktop"]["credential_source"] == "electron_safe_storage"
                for origin in ("https://evil.example", "http://localhost:5173"):
                    assert (await client.post("/api/projects/pick", headers={"Origin": origin})).status_code == 403
                body = {"config": {"requested_model": "desktop-model", "tools": ["read_file"], "limits": {"max_model_turns": 7, "max_tool_calls": 16, "run_timeout_seconds": 60}}, "downloads_directory": str(tmp_path / "downloads")}
                assert (await client.put("/api/desktop/settings", json=body)).status_code == 200
                saved = host.store.path.read_bytes()
                assert (await client.put("/api/desktop/settings", json={**body, "downloads_directory": "relative"})).status_code == 422
                assert host.store.path.read_bytes() == saved
                native.failed = True
                assert (await client.put("/api/desktop/credentials/deepseek", json={"secret": "replacement"})).status_code == 503
                assert host.api_key(host.manager.run_config) == "desktop-test-secret"
                native.failed = False
                response = await client.put("/api/desktop/credentials/deepseek", json={"secret": ""})
                assert response.json() == {"key_configured": False}
                assert host.api_key(RunConfig(provider="qwen")) == "qwen-test-secret"
                assert "qwen-test-secret" not in options.text
                assert "desktop-test-secret" not in host.store.path.read_text(encoding="utf-8")
                assert (await client.post("/api/desktop/external", json={"url": "javascript:alert(1)"})).status_code == 400
    asyncio.run(scenario())


def test_native_bridge_pairs_results_and_closes_pending_on_eof() -> None:
    """真实管道返回结果后解除异步等待；父进程 EOF 会取消未完成原生请求并通知服务退出。"""
    async def scenario() -> None:
        """通过有界标准流完成一次原生配对，验证断开时不会留悬挂 Future。"""
        read_descriptor, write_descriptor = os.pipe()
        reader = os.fdopen(read_descriptor, "r", encoding="utf-8")
        writer = os.fdopen(write_descriptor, "w", encoding="utf-8")
        output = io.StringIO()
        stopped = asyncio.Event()
        bridge = NativeBridge(reader, output)
        bridge.start(asyncio.get_running_loop(), stopped.set)
        pending = asyncio.create_task(bridge.request("folder.choose", {}))
        await asyncio.sleep(.01)
        request = json.loads(output.getvalue())
        writer.write(json.dumps({"type": "result", "id": request["id"], "result": None}) + "\n")
        writer.flush()
        assert await asyncio.wait_for(pending, 2) is None
        pending = asyncio.create_task(bridge.request("folder.choose", {}))
        await asyncio.sleep(.01)
        writer.close()
        await asyncio.wait_for(stopped.wait(), 2)
        with pytest.raises(ConfigurationError):
            await pending
        reader.close()
    asyncio.run(scenario())


def test_parent_pipe_loss_cancels_active_run_and_exits(tmp_path: Path) -> None:
    """Electron 异常退出造成标准输入 EOF 时，真实后台取消活动任务并保存唯一终态后自行退出。"""
    root = resource_root()
    token = "b" * 64
    log = (tmp_path / "backend.log").open("w", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "tests.helpers.desktop_fixture", "--data-dir", str(tmp_path), "--runs-dir", str(tmp_path / "runs")], cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8")
    lines: queue.Queue[str] = queue.Queue()

    def read_ready() -> None:
        """读取第一条启动消息并放入 lines；守护线程避免异常后台阻塞测试超时收尾。"""
        lines.put(process.stdout.readline())

    try:
        process.stdin.write(json.dumps({"token": token, "model_keys": {"deepseek": "offline-parent-pipe"}}) + "\n")
        process.stdin.flush()
        threading.Thread(target=read_ready, daemon=True).start()
        ready = json.loads(lines.get(timeout=15))
        assert ready["type"] == "ready", ready
        with httpx.Client(base_url=f"http://127.0.0.1:{ready['port']}", headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
            # 直接登记测试项目仅为建立任务输入；产品页面仍通过父进程原生选择器登记。
            web = WebConfig(project_dir=tmp_path, worktrees_root=tmp_path / "trees", frontend_dist=root / "frontend/dist")
            from agent_harness.web.project_catalog import ProjectCatalog
            identifier = ProjectCatalog(tmp_path / "runs", web).register(tmp_path / "project")
            response = client.post("/api/jobs", json={"request_id": str(__import__('uuid').uuid4()), "create": {"project_id": identifier, "prompt": "等待取消"}})
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if client.get("/api/runs/" + run_id).json()["phase"] == "model_request":
                    break
                time.sleep(.02)
            process.stdin.close()
        assert process.wait(timeout=15) == 0
        history = RunStore.read(tmp_path / "runs", run_id)
        assert history.result.execution_status == "cancelled"
        assert history.result.stop_reason == "service_shutdown"
        assert sum(event.type == "run_finished" for event in history.events) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        log.close()
