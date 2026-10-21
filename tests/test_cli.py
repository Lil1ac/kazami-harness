"""通过真实 CLI 进程验证首个切片，不依赖密钥或网络。"""

import json
import os
import subprocess
import sys
from pathlib import Path

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


def test_prepare_and_show_preserve_created_state_without_credentials(tmp_path: Path):
    """验证无密钥时仍能准备和查看任务，状态保持未执行、未验收。"""
    workspace = tmp_path / "工作区"
    workspace.mkdir()
    records = tmp_path / "records"
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)
    prepared = invoke(
        "run", "--prepare-only", "--task", "统计中文日志", "--workspace", workspace,
        "--runs-dir", records, "--json", env=env,
    )
    assert prepared.returncode == 0, prepared.stderr
    data = json.loads(prepared.stdout)
    assert data["execution_status"] == "created"
    assert data["validation_status"] == "unverified"
    assert data["model_turns"] == data["http_attempts"] == data["tool_calls"] == 0

    shown = invoke("show", data["run_id"], "--runs-dir", records, "--json", env=env)
    assert shown.returncode == 0, shown.stderr
    history = json.loads(shown.stdout)
    assert history["task"]["prompt"] == "统计中文日志"
    assert history["execution_status"] == "created"
    assert history["interrupted"] is False
    assert history["event_count"] == 1
    assert not (Path(data["run_dir"]) / "summary.json").exists()


def test_preparation_does_not_persist_secret_value(tmp_path: Path):
    """验证准备记录只保存密钥来源名称，不将环境变量中的凭据落盘。"""
    env = os.environ.copy()
    secret = "dummy-do-not-persist-credential"
    env["DEEPSEEK_API_KEY"] = secret
    records = tmp_path / "records"
    prepared = invoke(
        "run", "--prepare-only", "--task", "测试任务", "--workspace", tmp_path,
        "--runs-dir", records, "--json", env=env,
    )
    assert prepared.returncode == 0, prepared.stderr
    for path in records.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8")
    manifest = json.loads(next(records.glob("*/run.json")).read_text(encoding="utf-8"))
    assert manifest["config"]["api_key_env"] == "DEEPSEEK_API_KEY"


def test_invalid_workspace_does_not_create_run(tmp_path: Path):
    """验证无效工作目录在创建记录之前被拒绝，并返回配置错误码。"""
    records = tmp_path / "records"
    result = invoke(
        "run", "--prepare-only", "--task", "测试任务", "--workspace", tmp_path / "missing",
        "--runs-dir", records,
    )
    assert result.returncode == 2
    assert "工作目录" in result.stderr
    assert not records.exists()


def test_missing_key_does_not_create_run_or_send_request(tmp_path: Path):
    """验证执行模式缺少密钥时提前退出，不创建运行目录。"""
    records = tmp_path / "records"
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)
    result = invoke("run", "--task", "测试任务", "--workspace", tmp_path, "--runs-dir", records, env=env)
    assert result.returncode == 2
    assert "DEEPSEEK_API_KEY" in result.stderr
    assert not records.exists()


def test_config_error_does_not_echo_sensitive_value(tmp_path: Path):
    """验证非法配置的错误输出不回显敏感输入，且不会留下运行记录。"""
    config = tmp_path / "invalid.toml"
    secret = "dummy-secret-in-invalid-config"
    config.write_text(f'api_key = "{secret}"\n', encoding="utf-8")
    records = tmp_path / "records"
    result = invoke(
        "run", "--prepare-only", "--task", "测试", "--workspace", tmp_path,
        "--config", config, "--runs-dir", records,
    )
    assert result.returncode == 2
    assert secret not in result.stdout + result.stderr
    assert not records.exists()


def test_cli_override_preserves_other_config_values(tmp_path: Path):
    """验证 CLI 只覆盖指定限制，配置文件中的其他限制仍保留。"""
    config = tmp_path / "harness.toml"
    config.write_text('[limits]\nmax_model_turns = 4\nmax_tool_calls = 9\n', encoding="utf-8")
    records = tmp_path / "records"
    prepared = invoke(
        "run", "--prepare-only", "--task", "测试", "--workspace", tmp_path, "--config", config,
        "--max-model-turns", 7, "--runs-dir", records, "--json",
    )
    assert prepared.returncode == 0, prepared.stderr
    run_id = json.loads(prepared.stdout)["run_id"]
    shown = invoke("show", run_id, "--runs-dir", records, "--json")
    assert shown.returncode == 0, shown.stderr
    limits = json.loads(shown.stdout)["config"]["limits"]
    assert limits["max_model_turns"] == 7
    assert limits["max_tool_calls"] == 9


def test_show_rejects_path_traversal(tmp_path: Path):
    """验证历史查看拒绝非 UUID 路径输入，并输出受控错误而非堆栈。"""
    result = invoke("show", "../outside", "--runs-dir", tmp_path)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr


def test_cli_execution_and_history_preserve_unverified_answer(tmp_path: Path, monkeypatch, capsys):
    """通过离线模型完成 CLI 文件读取闭环，验证回答持久化且未验收退出码为 3。"""
    import httpx
    from agent_harness import cli
    from agent_harness.runner import run_task as actual_run

    secret = "dummy-cli-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
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
        return httpx.Response(200, json={"model": "mock-deepseek", "choices": [{"message": message, "finish_reason": reason}]})

    async def offline_run(manifest, root, api_key, **kwargs):
        """保留真实 Runner 行为，仅注入 MockTransport 以避免访问模型网络。"""
        return await actual_run(manifest, root, api_key, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cli, "run_task", offline_run)
    records = tmp_path / "records"
    code = cli.main(["run", "--task", "统计 app.log", "--workspace", str(workspace), "--runs-dir", str(records), "--json"])
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
    assert json.loads(shown.stdout)["final_answer"] == result["final_answer"]


def test_unknown_tool_fails_before_creating_run(tmp_path: Path):
    """验证配置包含未识别工具名时，在创建运行记录前报错。"""
    config = tmp_path / "unsupported.toml"
    config.write_text('tools = ["read_file", "unknown_tool"]\n', encoding="utf-8")
    env = os.environ.copy()
    env["DEEPSEEK_API_KEY"] = "dummy-unused"
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
    env["DEEPSEEK_API_KEY"] = "dummy-unused"
    records = tmp_path / "records"
    result = invoke("run", "--task", "测试", "--workspace", tmp_path, "--verifier", verifier, "--runs-dir", records, env=env)
    assert result.returncode == 2
    assert "验收入口" in result.stderr
    assert not records.exists()



def test_prepare_can_register_expected_output_without_creating_it(tmp_path: Path):
    """CLI 可登记预期产物但不生成文件，也不在准备阶段调用真实模型。"""
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)
    records = tmp_path / "records"
    prepared = invoke("run", "--prepare-only", "--task", "生成报告", "--workspace", tmp_path, "--runs-dir", records, "--expected-output", "report.json", "--json", env=env)
    assert prepared.returncode == 0, prepared.stderr
    run_id = json.loads(prepared.stdout)["run_id"]
    shown = invoke("show", run_id, "--runs-dir", records, "--json", env=env)
    assert json.loads(shown.stdout)["task"]["expected_outputs"] == ["report.json"]
    assert not (tmp_path / "report.json").exists()


def test_bad_expected_output_is_rejected_before_preparation(tmp_path: Path):
    """准备入口也检查预期输出边界，拒绝父路径且不创建运行目录。"""
    records = tmp_path / "records"
    result = invoke("run", "--prepare-only", "--task", "报告", "--workspace", tmp_path, "--runs-dir", records, "--expected-output", "../outside.json")
    assert result.returncode == 2
    assert "预期输出" in result.stderr
    assert not records.exists()


@pytest.mark.parametrize("correct,expected_code,expected_status", [(True, 0, "passed"), (False, 1, "failed")])
def test_cli_verifier_and_show_preserve_independent_details(tmp_path: Path, monkeypatch, capsys, correct: bool, expected_code: int, expected_status: str):
    """correct 选择报告内容，核对CLI的 expected_code/status、真实验收输出与历史；返回 None。"""
    import httpx
    from agent_harness import cli
    from agent_harness.runner import run_task as actual_run

    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-cli-verifier-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = {"counts": {"timeout": 3, "database": 2, "auth": 1}, "total_errors": 6 if correct else 7}
    (workspace / "report.json").write_text(json.dumps(report), encoding="utf-8")
    checker = Path(__file__).resolve().parents[1] / "examples/verifiers/verify_logs.py"
    verifier = tmp_path / "verifier.toml"
    verifier.write_text("argv = " + json.dumps([sys.executable, str(checker)]) + "\ntimeout_seconds = 5.0\n", encoding="utf-8")
    requests = []

    def handler(request):
        """request 是离线HTTP请求；返回最终回答并记次数，检查验收不重新请求模型。"""
        requests.append(request)
        return httpx.Response(200, json={"model": "offline-model", "choices": [{
            "message": {"role": "assistant", "content": "报告已就绪。"}, "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})

    async def offline_run(manifest, root, api_key, **kwargs):
        """用 MockTransport 替换网络，保留 manifest/root/key 与信号参数，返回真实运行结果。"""
        return await actual_run(manifest, root, api_key, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cli, "run_task", offline_run)
    records = tmp_path / "records"
    code = cli.main([
        "run", "--task", "检查已有报告", "--workspace", str(workspace), "--runs-dir", str(records),
        "--verifier", str(verifier), "--expected-output", "report.json", "--json",
    ])
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert code == expected_code
    assert result["execution_status"] == "completed"
    assert result["validation_status"] == result["verification"]["status"] == expected_status
    assert len(result["verification"]["artifacts"]) == 2
    assert len(result["artifacts"]) == 3
    assert len(requests) == result["http_attempts"] == 1
    assert "dummy-cli-verifier-key" not in output.out + output.err
    shown = invoke("show", result["run_id"], "--runs-dir", records, "--json")
    assert shown.returncode == 0
    assert json.loads(shown.stdout)["verification"] == result["verification"]
    readable = invoke("show", result["run_id"], "--runs-dir", records)
    assert readable.returncode == 0
    assert "验收原因：" in readable.stdout
    assert result["verification"]["reason"] in readable.stdout


def test_prepare_with_verifier_does_not_execute_it(tmp_path: Path):
    """准备模式可登记宿主验收入口，但不创建marker或请求密钥；返回 None。"""
    verifier = tmp_path / "verifier.toml"
    source = "from pathlib import Path;Path('marker').write_text('bad')"
    verifier.write_text("argv = " + json.dumps([sys.executable, "-c", source]) + "\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.pop("DEEPSEEK_API_KEY", None)
    result = invoke(
        "run", "--prepare-only", "--task", "准备", "--workspace", tmp_path,
        "--verifier", verifier, "--runs-dir", tmp_path / "records", "--json", env=environment,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["execution_status"] == "created"
    assert data["verification"] is None
    assert not (tmp_path / "marker").exists()


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
