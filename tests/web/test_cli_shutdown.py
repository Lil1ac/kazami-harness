"""用真实CLI进程、HTTP/SSE连接和SIGINT验证停服会取消后台命令并持久化收尾。"""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

from agent_harness.store import RunStore


# 子进程始终覆盖模型凭据为离线值，模型客户端也由本地替身提供，不访问供应商。
DUMMY_KEY = "offline-cli-shutdown-dummy-key"
# 子进程位于临时目录，通过显式源目录保证导入本次待测CLI实现。
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
# 该脚本只运行在本测试创建的Python进程；监听停止标记后给自身发送真实SIGINT。
SERVER_SCRIPT = r'''"""本测试专用离线服务：保留CLI和Web/Runner实现，只替换模型响应。"""
import asyncio
import json
from pathlib import Path
import signal
import sys
import threading
import time

from agent_harness import cli
from agent_harness.web import api as web_api
from agent_harness.providers.base import ModelResponse, ToolCall

# 保存真实factory，测试仅注入离线模型，不替换HTTP、生命周期或Runner。
original_factory = web_api.create_app
# 宿主停止标记只触发一次信号，不由网络请求控制。
stop_marker = Path("stop.requested")


class BlockingToolModel:
    """返回真实长时间命令调用，让停服测试覆盖进程清理而非仅模型协程取消。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """接收消息和工具定义，返回写就绪标记后等待60秒的命令；没有供应商网络访问。"""
        source = "import pathlib,time;pathlib.Path('command.started').write_text('ready',encoding='utf-8');print('started',flush=True);time.sleep(60)"
        arguments = json.dumps({"argv": [sys.executable, "-c", source]})
        call = ToolCall(call_id="blocking-command", name="run_command", arguments_json=arguments)
        message = {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": arguments}}],
        }
        return ModelResponse(
            message=message, tool_calls=[call], finish_reason="tool_calls", response_model="offline-shutdown-test",
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )


def offline_factory(*args, **kwargs):
    """原样转交CLI的factory参数，只将model_client设置为离线模型，返回真实FastAPI应用。"""
    kwargs["model_client"] = BlockingToolModel()
    return original_factory(*args, **kwargs)


def interrupt_when_requested() -> None:
    """轮询临时停止标记，出现后对本进程发SIGINT；守护线程退出不影响其他进程。"""
    while not stop_marker.exists():
        time.sleep(0.02)
    signal.raise_signal(signal.SIGINT)


web_api.create_app = offline_factory
threading.Thread(target=interrupt_when_requested, daemon=True).start()
raise SystemExit(cli.main([
    "serve", "--web-config", "web.toml", "--runs-dir", "records", "--port", sys.argv[1],
]))
'''


def unused_loopback_port() -> int:
    """临时绑定本机端口0并返回系统分配的端口号，关闭探测套接字后交给待测CLI监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for_local_server(client: httpx.Client, process: subprocess.Popen, log_path: Path) -> None:
    """最多8秒等待client指向的CLI就绪；process提前退出时读取本测试日志给出失败原因。"""
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("离线CLI提前退出：" + log_path.read_text(encoding="utf-8", errors="replace")[-4000:])
        try:
            if client.get("/api/options", timeout=0.3).status_code == 200:
                return
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.02)
    pytest.fail("离线CLI未在8秒内监听本机端口。")


def wait_for_command_marker(path: Path, process: subprocess.Popen) -> None:
    """最多8秒等path出现以确认真实命令已启动；process提前退出或超时使测试失败。"""
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        assert process.poll() is None, "CLI在命令启动前退出。"
        if path.is_file():
            return
        time.sleep(0.02)
    pytest.fail("真实命令未在8秒内写入就绪标记。")


def cleanup_owned_server(process: subprocess.Popen) -> None:
    """仅强制清理本测试创建的process及其进程树；正常退出时无操作，返回 None。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5,
        )
    else:
        # Popen为本测试建立独立session，进程组编号必须等于所属子进程PID。
        if os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    process.wait(timeout=5)


def test_cli_sigint_with_open_sse_cleans_command_and_persists_shutdown(tmp_path: Path):
    """保持真实SSE订阅时给CLI发送SIGINT，要求12秒内退出并保存唯一停服终态和完整进程清理。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "server_helper.py").write_text(SERVER_SCRIPT, encoding="utf-8")
    (tmp_path / "web.toml").write_text(
        'runtime_root="runtime"\nfrontend_dist="dist"\n'
        '[[workspaces]]\nid="workspace"\nlabel="离线停服测试"\npath="workspace"\n',
        encoding="utf-8",
    )
    child_environment = os.environ.copy()
    child_environment.update({"DEEPSEEK_API_KEY": DUMMY_KEY, "PYTHONPATH": str(SOURCE_ROOT), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
    port = unused_loopback_port()
    log_path = tmp_path / "server.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(tmp_path / "server_helper.py"), str(port)],
            cwd=tmp_path, env=child_environment, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        try:
            # 本机请求必须绕过宿主代理环境，既避免外网也不要求安装SOCKS依赖。
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5) as client:
                wait_for_local_server(client, process, log_path)
                response = client.post("/api/runs", json={
                    "prompt": "执行离线阻塞命令，等待服务关闭。", "workspace_id": "workspace",
                    "config": {"tools": ["run_command"], "limits": {"command_timeout_seconds": 60.0, "run_timeout_seconds": 90.0}},
                })
                assert response.status_code == 202, response.text
                identifier = response.json()["run_id"]
                wait_for_command_marker(workspace / "command.started", process)
                with client.stream("GET", f"/api/runs/{identifier}/stream") as stream:
                    assert stream.status_code == 200
                    assert "text/event-stream" in stream.headers["content-type"]
                    lines = stream.iter_lines()
                    assert next(lines).startswith("id:")
                    assert stream.is_closed is False
                    # 整个wait期间保留此真实SSE连接，不能靠父进程提前关闭订阅使服务退出。
                    requested_at = time.monotonic()
                    (tmp_path / "stop.requested").write_text("stop", encoding="utf-8")
                    process.wait(timeout=12)
                    assert time.monotonic() - requested_at < 12
            assert process.returncode in {0, 130}, log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            history = RunStore.read(tmp_path / "records", identifier)
            assert history.interrupted is False
            assert history.result.execution_status == "cancelled"
            assert history.result.stop_reason == "service_shutdown"
            assert history.result.cleanup_status == "complete"
            assert [event.type for event in history.events].count("run_finished") == 1
            cancellation = next(event for event in history.events if event.type == "cancellation_received")
            assert cancellation.payload["source"] == "service_shutdown"
            command_results = [event.payload["result"] for event in history.events if event.type == "tool_result"]
            assert len(command_results) == 1
            assert command_results[0]["data"]["cancelled"] is True
            assert command_results[0]["data"]["cleanup_status"] == "complete"
        finally:
            cleanup_owned_server(process)
