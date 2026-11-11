"""通过真实 CLI 进程验证首个切片，不依赖密钥或网络。"""

from tests.helpers.streaming import model_stream_response
from tests.helpers.models import MODEL_TEST_TOML

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
import sys
from pathlib import Path
from threading import Thread

import pytest


def invoke(*args: object, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """使用独立 Python 进程调用 CLI，捕获 UTF-8 输出和退出码，可传入隔离环境。"""
    return subprocess.run(
        [sys.executable, "-m", "agent_harness", *(str(arg) for arg in args)],
        capture_output=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def test_invalid_workspace_does_not_create_run(tmp_path: Path):
    """验证无效工作目录在创建记录之前被拒绝，并返回配置错误码。"""
    records = tmp_path / "records"
    result = invoke(
        "run", "--task", "测试任务", "--workspace", tmp_path / "missing",
        "--runs-dir", records,
    )
    assert result.returncode == 2
    assert "工作目录" in result.stderr
    assert not records.exists()


@pytest.mark.parametrize("provider,key_env", [("custom-fixture", "HARNESS_TEST_MODEL_KEY"), ("custom-secondary", "HARNESS_TEST_OTHER_KEY")])
def test_missing_key_does_not_create_run_or_send_request(tmp_path: Path, provider: str, key_env: str):
    """验证显式 provider 缺少对应 key_env 时提前退出，另一供应商凭据不能代用。"""
    records = tmp_path / "records"
    env = os.environ.copy()
    env.update(HARNESS_TEST_MODEL_KEY="offline-first", HARNESS_TEST_OTHER_KEY="offline-second")
    env.pop(key_env)
    config = tmp_path / "providers.toml"
    config.write_text(MODEL_TEST_TOML + """
[custom_providers.custom-secondary]
name = "另一条测试连接"
base_url = "https://secondary.example.test/v1"
default_model = "second-model"
api_key_env = "HARNESS_TEST_OTHER_KEY"
""", encoding="utf-8")
    result = invoke("run", "--config", config, "--provider", provider, "--task", "测试任务", "--workspace", tmp_path, "--runs-dir", records, env=env)
    assert result.returncode == 2
    assert key_env in result.stderr
    assert not records.exists()


def test_custom_provider_cli_uses_toml_and_real_loopback_stream(tmp_path: Path):
    """启动本机合成端点，由真实 CLI 读取 TOML 并发送免密流式请求。"""
    requests: list[tuple[str, str | None, dict]] = []  # 收集接口路径、认证头和请求体。

    class CompatibleHandler(BaseHTTPRequestHandler):
        """返回固定 Chat Completions SSE，避免访问外部供应商。"""

        def do_POST(self):
            """记录本机 POST 并返回完整 SSE，响应后结束连接。"""
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            response = model_stream_response(200, json={"model": "loopback-actual", "choices": [{
                "message": {"role": "assistant", "content": "本机响应完成"}, "finish_reason": "stop",
            }]})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, format, *args):
            """抑制 format 与 args 的默认日志，测试只保留结构化请求事实。"""

    with ThreadingHTTPServer(("127.0.0.1", 0), CompatibleHandler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = tmp_path / "custom.toml"
            config.write_text(f'''[custom_providers.custom-loopback]
name = "本机测试"
base_url = "http://localhost:{server.server_port}/v1/"
default_model = "loopback-model"
models = ["loopback-other"]
auth = "none"
''', encoding="utf-8")
            result = invoke("run", "--provider", "custom-loopback", "--model", "loopback-other", "--task", "离线连通测试",
                            "--workspace", tmp_path, "--config", config, "--runs-dir", tmp_path / "records", "--json")
            assert result.returncode == 3, result.stderr  # 没有独立验收器时，CLI 用 3 表示执行完成但未验收。
            summary = json.loads(result.stdout)
            assert summary["execution_status"] == "completed" and summary["response_model"] == "loopback-actual"
            assert len(requests) == 1
            path, auth, body = requests[0]
            assert path == "/v1/chat/completions" and auth is None
            assert body["model"] == "loopback-other" and body["stream"] is True
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_config_error_does_not_echo_sensitive_value(tmp_path: Path):
    """验证非法配置的错误输出不回显敏感输入，且不会留下运行记录。"""
    config = tmp_path / "invalid.toml"
    secret = "dummy-secret-in-invalid-config"
    config.write_text(f'api_key = "{secret}"\n', encoding="utf-8")
    records = tmp_path / "records"
    result = invoke(
        "run", "--task", "测试", "--workspace", tmp_path,
        "--config", config, "--runs-dir", records,
    )
    assert result.returncode == 2
    assert secret not in result.stdout + result.stderr
    assert not records.exists()


def test_show_rejects_path_traversal(tmp_path: Path):
    """验证历史查看拒绝非 UUID 路径输入，并输出受控错误而非堆栈。"""
    result = invoke("show", "../outside", "--runs-dir", tmp_path)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr


def test_cli_execution_and_history_preserve_unverified_answer(tmp_path: Path, monkeypatch, capsys):
    """通过离线模型完成 CLI 文件读取闭环，验证回答持久化且未验收退出码为 3。"""
    import httpx
    from agent_harness import cli
    from agent_harness.runtime.runner import run_task as actual_run

    secret = "dummy-cli-key"
    monkeypatch.setenv("HARNESS_TEST_MODEL_KEY", secret)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.log").write_text("ERROR timeout", encoding="utf-8")
    requests = []

    def handler(request):
        """首轮返回读文件调用，第二轮返回统计回答，并捕获请求以检查轮次。"""
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "read-1", "type": "function", "function": {
                    "name": "read_file", "arguments": '{"path":"app.log"}',
                },
            }]}
            reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": "timeout：1 次。"}
            reason = "stop"
        return model_stream_response(200, json={"model": "mock-deepseek", "choices": [{"message": message, "finish_reason": reason}]})

    async def offline_run(manifest, root, api_key, **kwargs):
        """保留真实 Runner 行为，仅注入 MockTransport 以避免访问模型网络。"""
        return await actual_run(manifest, root, api_key, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cli, "run_task", offline_run)
    records = tmp_path / "records"
    profile = tmp_path / "harness.toml"
    profile.write_text("[limits]\nmax_model_turns = 4\nmax_tool_calls = 9\n" + MODEL_TEST_TOML, encoding="utf-8")
    code = cli.main(["run", "--task", "统计 app.log", "--workspace", str(workspace), "--runs-dir", str(records), "--config", str(profile), "--max-model-turns", "7", "--json"])
    captured = capsys.readouterr()
    assert code == 3
    result = json.loads(captured.out)
    assert result["final_answer"] == "timeout：1 次。"
    assert result["execution_status"] == "completed"
    assert result["validation_status"] == "unverified"
    assert secret not in captured.out + captured.err
    assert len(requests) == 2
    shown = invoke("show", result["run_id"], "--runs-dir", records, "--json")
    assert shown.returncode == 0
    history = json.loads(shown.stdout)
    assert history["final_answer"] == result["final_answer"]
    assert history["config"]["limits"]["max_model_turns"] == 7
    assert history["config"]["limits"]["max_tool_calls"] == 9


def test_unknown_tool_fails_before_creating_run(tmp_path: Path):
    """验证配置包含未识别工具名时，在创建运行记录前报错。"""
    config = tmp_path / "unsupported.toml"
    config.write_text('tools = ["read_file", "unknown_tool"]\n', encoding="utf-8")
    env = os.environ.copy()
    env["HARNESS_TEST_MODEL_KEY"] = "dummy-unused"
    records = tmp_path / "records"
    result = invoke("run", "--task", "测试", "--workspace", tmp_path, "--config", config, "--runs-dir", records, env=env)
    assert result.returncode == 2
    assert "运行配置" in result.stderr
    assert not records.exists()


def test_invalid_verifier_fails_before_creating_run(tmp_path: Path):
    """验收入口不存在时在创建记录前失败；tmp_path 是隔离目录，返回 None。"""
    import json as json_module

    verifier = tmp_path / "verifier.toml"
    verifier.write_text("argv = [" + json_module.dumps(str(tmp_path / "missing-verifier.exe")) + "]\n", encoding="utf-8")
    env = os.environ.copy()
    env["HARNESS_TEST_MODEL_KEY"] = "dummy-unused"
    records = tmp_path / "records"
    result = invoke("run", "--task", "测试", "--workspace", tmp_path, "--verifier", verifier, "--runs-dir", records, env=env)
    assert result.returncode == 2
    assert "验收入口" in result.stderr
    assert not records.exists()


@pytest.mark.parametrize("correct,expected_code,expected_status", [(True, 0, "passed"), (False, 1, "failed")])
def test_cli_verifier_and_show_preserve_independent_details(tmp_path: Path, monkeypatch, capsys, correct: bool, expected_code: int, expected_status: str):
    """correct 选择报告内容，核对CLI的 expected_code/status、真实验收输出与历史；返回 None。"""
    import httpx
    from agent_harness import cli
    from agent_harness.runtime.runner import run_task as actual_run

    monkeypatch.setenv("HARNESS_TEST_MODEL_KEY", "dummy-cli-verifier-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = {"counts": {"timeout": 3, "database": 2, "auth": 1}, "total_errors": 6 if correct else 7}
    (workspace / "report.json").write_text(json.dumps(report), encoding="utf-8")
    checker = Path(__file__).resolve().parents[1] / "examples/verifiers/verify_logs.py"
    verifier = tmp_path / "verifier.toml"
    verifier.write_text("argv = " + json.dumps([sys.executable, str(checker)]) + "\ntimeout_seconds = 5.0\n", encoding="utf-8")
    profile = tmp_path / "trusted.toml"
    profile.write_text('[permission_policy]\nverification="allow"\n' + MODEL_TEST_TOML, encoding="utf-8")  # 本用例显式预授权独立检查。
    requests = []

    def handler(request):
        """request 是离线HTTP请求；返回最终回答并记次数，检查验收不重新请求模型。"""
        requests.append(request)
        return model_stream_response(200, json={"model": "offline-model", "choices": [{
            "message": {"role": "assistant", "content": "报告已就绪。"}, "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})

    async def offline_run(manifest, root, api_key, **kwargs):
        """用 MockTransport 替换网络，保留 manifest/root/key 与信号参数，返回真实运行结果。"""
        return await actual_run(manifest, root, api_key, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cli, "run_task", offline_run)
    records = tmp_path / "records"
    code = cli.main([
        "run", "--task", "检查已有报告", "--workspace", str(workspace), "--runs-dir", str(records),
        "--verifier", str(verifier), "--config", str(profile), "--json",
    ])
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert code == expected_code
    assert result["execution_status"] == "completed"
    assert result["validation_status"] == result["verification"]["status"] == expected_status
    assert len(result["verification"]["artifacts"]) == 2
    assert len(result["artifacts"]) == 2
    assert len(requests) == result["http_attempts"] == 1
    assert "dummy-cli-verifier-key" not in output.out + output.err
    shown = invoke("show", result["run_id"], "--runs-dir", records, "--json")
    assert shown.returncode == 0
    assert json.loads(shown.stdout)["verification"] == result["verification"]
    readable = invoke("show", result["run_id"], "--runs-dir", records)
    assert readable.returncode == 0
    assert "验收原因：" in readable.stdout
    assert result["verification"]["reason"] in readable.stdout


def test_serve_help_describes_local_console_without_starting_service():
    """无参数构造服务帮助信息，不监听端口也不要求密钥；返回None。"""
    result = invoke("serve", "--help")
    assert result.returncode == 0
    assert "--web-config" in result.stdout
    assert "--port" in result.stdout


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_serve_rejects_invalid_port_before_initialization(tmp_path: Path, port: int):
    """port为越界服务端口，验证启动前返回2且不创建记录或工作区；返回None。"""
    root = tmp_path / "records"
    result = invoke("serve", "--port", port, "--runs-dir", root)
    assert result.returncode == 2
    assert "端口" in result.stderr
    assert not root.exists()
