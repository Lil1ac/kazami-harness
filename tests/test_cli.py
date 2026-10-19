"""通过真实 CLI 进程验证首个切片，不依赖密钥或网络。"""

import json
import os
import subprocess
import sys
from pathlib import Path


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


def test_unimplemented_verifier_fails_before_creating_run(tmp_path: Path):
    """验证执行模式拒绝尚未接入的独立验收入口，不产生假启动记录。"""
    import json as json_module

    verifier = tmp_path / "verifier.toml"
    verifier.write_text("argv = [" + json_module.dumps(sys.executable) + ', "-c", "pass"]\n', encoding="utf-8")
    env = os.environ.copy()
    env["DEEPSEEK_API_KEY"] = "dummy-unused"
    records = tmp_path / "records"
    result = invoke("run", "--task", "测试", "--workspace", tmp_path, "--verifier", verifier, "--runs-dir", records, env=env)
    assert result.returncode == 2
    assert "独立验收" in result.stderr
    assert not records.exists()
