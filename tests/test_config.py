"""验证启动边界、配置优先级和错误信息的脱敏行为。"""

import json
import os
import sys
from pathlib import Path

import pytest

from agent_harness.config import ConfigurationError, load_config, prepare_task


def write_toml(path: Path, text: str) -> Path:
    """将给定文本写入临时 UTF-8 配置文件，并返回路径供配置测试使用。"""
    path.write_text(text, encoding="utf-8")
    return path


def test_config_merges_limits_without_mutating_overrides(tmp_path: Path) -> None:
    """验证配置优先级、限制逐字段合并和覆盖字典不被修改。"""
    config_path = write_toml(
        tmp_path / "config.toml",
        'requested_model = "configured-model"\ntools = ["read_file"]\n'
        "[limits]\nmax_model_turns = 8\nmax_tool_calls = 12\n",
    )
    overrides = {"requested_model": "cli-model", "limits": {"max_tool_calls": 5}}
    config = load_config(config_path, overrides)
    assert config.requested_model == "cli-model"
    assert config.tools == ["read_file"]
    assert config.limits.max_model_turns == 8
    assert config.limits.max_tool_calls == 5
    assert config.limits.command_timeout_seconds == 30.0
    assert overrides == {"requested_model": "cli-model", "limits": {"max_tool_calls": 5}}
    assert load_config().limits.max_model_turns == 20


def test_config_does_not_read_environment_or_dotenv(tmp_path: Path, monkeypatch) -> None:
    """验证加载配置不读取密钥值或 .env，仅保存固定环境变量名。"""
    secret = "credential-must-not-enter-config"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    write_toml(tmp_path / ".env", f"DEEPSEEK_API_KEY={secret}")
    config = load_config()
    assert config.api_key_env == "DEEPSEEK_API_KEY"
    assert secret not in config.model_dump_json()
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert load_config() == config


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": "TOP_SECRET"},
        {"api_key_env": "TOP_SECRET"},
        {"unknown": "TOP_SECRET"},
        {"provider": "TOP_SECRET"},
        {"requested_model": 7},
        {"requested_model": "   "},
        {"system_prompt": "\t"},
        {"tools": ["read_file", "read_file"]},
        {"tools": ["unknown_tool"]},
        {"tools": "read_file"},
        {"limits": {"max_model_turns": "20"}},
        {"limits": {"max_model_turns": True}},
        {"limits": {"max_model_turns": 0}},
        {"limits": {"run_timeout_seconds": "30"}},
        {"limits": {"run_timeout_seconds": float("inf")}},
        {"limits": {"extra_secret": "TOP_SECRET"}},
        {"limits": []},
    ],
)
def test_config_rejects_invalid_values_without_echoing_input(tmp_path: Path, overrides: dict) -> None:
    """参数化验证未知字段、错误类型及非法取值均被拒绝，错误不回显输入。"""
    with pytest.raises(ConfigurationError) as captured:
        load_config(overrides=overrides)
    message = str(captured.value)
    assert "无效" in message
    assert "TOP_SECRET" not in message
    assert "input_value" not in message
    assert not (tmp_path / ".runs").exists()


def test_toml_errors_do_not_echo_contents(tmp_path: Path) -> None:
    """验证 TOML 语法错误只返回受控提示，不泄露配置正文。"""
    path = write_toml(tmp_path / "bad.toml", 'api_key = "TOP_SECRET"\ninvalid TOP_SECRET')
    with pytest.raises(ConfigurationError, match="TOML") as captured:
        load_config(path)
    assert "TOP_SECRET" not in str(captured.value)


def test_missing_config_file_and_invalid_override_type(tmp_path: Path) -> None:
    """验证配置文件缺失和覆盖参数类型错误都转换为 ConfigurationError。"""
    with pytest.raises(ConfigurationError, match="无法读取"):
        load_config(tmp_path / "missing.toml")
    with pytest.raises(ConfigurationError, match="字典"):
        load_config(overrides=[])  # type: ignore[arg-type]


def test_prepare_task_resolves_workspace_without_api_key(tmp_path: Path, monkeypatch) -> None:
    """验证准备任务无需密钥，并将相对工作目录规范化为绝对路径。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "workspace").mkdir()
    task = prepare_task("处理数据", Path("workspace"))
    assert task.workspace == (tmp_path / "workspace").resolve()
    assert task.verifier is None
    assert not (tmp_path / ".runs").exists()


@pytest.mark.parametrize("prompt", ["", " \n\t", 42])
def test_invalid_task_prompt(tmp_path: Path, prompt) -> None:
    """验证空字符串、纯空白和错误类型的任务描述均被拒绝。"""
    with pytest.raises(ConfigurationError, match="任务参数"):
        prepare_task(prompt, tmp_path)


def test_workspace_must_be_existing_directory(tmp_path: Path) -> None:
    """验证不存在的路径和普通文件都不能作为任务工作目录。"""
    file_path = write_toml(tmp_path / "file", "data")
    for path in [tmp_path / "missing", file_path]:
        with pytest.raises(ConfigurationError, match="任务工作目录"):
            prepare_task("task", path)


def test_verifier_defaults_to_host_directory_and_timeout(tmp_path: Path) -> None:
    """验证验收工作目录默认取清单目录，超时使用调用方默认值，准备时不执行入口。"""
    host = tmp_path / "host"
    host.mkdir()
    verifier = write_toml(
        host / "verifier.toml",
        f"argv = {json.dumps([sys.executable, '-c', 'raise RuntimeError()'])}\n",
    )
    task = prepare_task("task", tmp_path, verifier, verifier_timeout=7.0)
    assert task.verifier is not None
    assert task.verifier.cwd == host.resolve()
    assert task.verifier.argv[0] == str(Path(sys.executable).resolve())
    assert task.verifier.timeout_seconds == 7.0


def test_relative_verifier_paths_use_manifest_parent(tmp_path: Path, monkeypatch) -> None:
    """验证验收相对目录和程序路径以清单目录为准，显式超时覆盖默认值。"""
    host = tmp_path / "host"
    host.mkdir()
    (host / "work").mkdir()
    (host / "bin").mkdir()
    entry = host / "bin" / ("check.exe" if os.name == "nt" else "check")
    entry.write_text("验收入口不能在准备阶段执行", encoding="utf-8")
    entry.chmod(0o755)
    verifier = write_toml(
        host / "verifier.toml",
        f'argv = ["bin/{entry.name}", "--check"]\ncwd = "work"\ntimeout_seconds = 4.0\n',
    )
    monkeypatch.chdir(tmp_path)
    task = prepare_task("task", tmp_path, verifier, verifier_timeout=9.0)
    assert task.verifier is not None
    assert task.verifier.cwd == (host / "work").resolve()
    assert task.verifier.argv == [str(entry.resolve()), "--check"]
    assert task.verifier.timeout_seconds == 4.0


def test_verifier_command_on_path_is_resolved(tmp_path: Path, monkeypatch) -> None:
    """验证裸命令名可从 PATH 找到，并被固定为绝对程序路径。"""
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    path = write_toml(tmp_path / "verifier.toml", f"argv = [{json.dumps(Path(sys.executable).name)}]")
    task = prepare_task("task", tmp_path, path)
    assert task.verifier is not None
    assert task.verifier.argv[0] == str(Path(sys.executable).resolve())


@pytest.mark.skipif(os.name != "nt", reason="Windows 脚本入口约束")
def test_windows_verifier_script_requires_an_interpreter(tmp_path: Path) -> None:
    """验证 Windows 不直接接受 Python 脚本作为验收入口，要求显式解释器。"""
    (tmp_path / "verify.py").write_text("raise SystemExit(0)", encoding="utf-8")
    path = write_toml(tmp_path / "verifier.toml", 'argv = ["./verify.py"]')
    with pytest.raises(ConfigurationError, match="解释器"):
        prepare_task("task", tmp_path, path)


@pytest.mark.parametrize(
    "body",
    [
        'argv = []',
        'argv = ["   "]',
        'argv = [17]',
        'argv = ["./missing-entry"]',
        'argv = ["unavailable-harness-verifier-command"]',
        'argv = ["."]',
        'argv = ["./missing-entry"]\ncwd = "missing"',
        'argv = ["./missing-entry"]\ncwd = 1',
        'argv = ["./missing-entry"]\ntimeout_seconds = "30"',
        'argv = ["./missing-entry"]\ntimeout_seconds = -1.0',
        'argv = ["./missing-entry"]\napi_key = "TOP_SECRET"',
        'argv = ["TOP_SECRET"',
    ],
)
def test_invalid_verifier_does_not_execute_or_leak(tmp_path: Path, body: str) -> None:
    """参数化验证非法验收清单提前失败，不执行命令、不创建记录且不泄露输入。"""
    path = write_toml(tmp_path / "verifier.toml", body)
    with pytest.raises(ConfigurationError) as captured:
        prepare_task("task", tmp_path, path)
    assert "TOP_SECRET" not in str(captured.value)
    assert not (tmp_path / ".runs").exists()



def test_verifier_python_paths_are_relative_to_explicit_cwd(tmp_path: Path) -> None:
    """宿主配置的 src 相对验收 cwd 解析，使独立副本的导入目录随工作区绑定。"""
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    verifier = write_toml(
        tmp_path / "verifier.toml",
        f"argv = {json.dumps([sys.executable, '-c', 'pass'])}\ncwd = \"worktree\"\npython_paths = [\"src\"]\n",
    )
    task = prepare_task("task", worktree, verifier)
    assert task.verifier.python_paths == [(worktree / "src").resolve()]
