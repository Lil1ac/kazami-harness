"""用真实本地进程验证宿主验收协议、输出预算和环境隔离，不访问模型。"""

import asyncio
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from agent_harness.processes import ProcessRunner
from agent_harness.schemas import RunLimits, VerifierSpec
from agent_harness.verifier import Verifier, _map_result


def execute(tmp_path: Path, source: str, **limits):
    """在隔离目录执行 source，返回验收详情；limits 覆盖预算，不访问网络。"""
    verifier = Verifier(RunLimits(**limits), tmp_path)
    spec = VerifierSpec(argv=[sys.executable, "-c", source], cwd=tmp_path)
    result = asyncio.run(verifier.run(spec, tmp_path, uuid4()))
    assert verifier.last_result == result
    return result


@pytest.mark.parametrize("code,status,reason", [
    (0, "passed", "checks_passed"),
    (1, "failed", "assertion_failed"),
    (2, "error", "verifier_exit_error"),
    (7, "error", "verifier_exit_error"),
])
def test_normal_exit_protocol_keeps_both_streams(tmp_path: Path, code: int, status: str, reason: str):
    """code 为真实退出码；验证 status/reason 协议及双流文件，返回 None。"""
    result = execute(tmp_path, f"import sys;print('report');print('warning',file=sys.stderr);sys.exit({code})")
    assert (result.status, result.reason, result.exit_code) == (status, reason, code)
    assert result.cleanup_status == "not_needed"
    assert result.stdout.strip() == "report"
    assert result.stderr.strip() == "warning"
    assert result.elapsed_ms >= 0
    assert {item.kind for item in result.artifacts} == {"stdout", "stderr"}
    assert all((tmp_path / item.path).is_file() for item in result.artifacts)


def test_preview_truncation_alone_does_not_fail_verification(tmp_path: Path):
    """缩小字符预览后仍允许正常退出通过，原始输出完整保存；返回 None。"""
    result = execute(tmp_path, "print('a'*200)", tool_output_preview_chars=13)
    assert result.status == "passed"
    assert result.stdout == "a" * 13
    assert result.truncated is True
    assert result.output_limit_exceeded is False
    stdout = next(item for item in result.artifacts if item.kind == "stdout")
    assert (tmp_path / stdout.path).read_text().strip() == "a" * 200


def test_raw_output_cap_never_passes(tmp_path: Path):
    """超过合计字节预算时终止真实进程并报告验收异常；返回 None。"""
    result = execute(tmp_path, "import os,time;os.write(1,b'x'*4096);time.sleep(20)", max_process_capture_bytes=257)
    assert (result.status, result.reason) == ("error", "verifier_output_limit")
    assert result.output_limit_exceeded is True
    assert result.cleanup_status == "complete"
    assert sum((tmp_path / item.path).stat().st_size for item in result.artifacts) == 257


def test_spawn_failure_is_verification_error(tmp_path: Path):
    """模拟预检后入口被删除，返回启动异常并保留已创建的双流引用；返回 None。"""
    verifier = Verifier(RunLimits(), tmp_path)
    spec = VerifierSpec(argv=[str(tmp_path / "deleted.exe")], cwd=tmp_path)
    result = asyncio.run(verifier.run(spec, tmp_path, uuid4()))
    assert (result.status, result.reason) == ("error", "verifier_start_failed")
    assert result.exit_code is None
    assert len(result.artifacts) == 2


@pytest.mark.parametrize("facts,reason", [
    ({"cancelled": True}, "verifier_cancelled"),
    ({"timed_out": True}, "verifier_timeout"),
    ({"output_limit_exceeded": True}, "verifier_output_limit"),
    ({"cleanup_status": "unknown"}, "verifier_cleanup_failed"),
    ({"cleanup_status": "incomplete"}, "verifier_cleanup_failed"),
    ({"error_code": "unexpected_code"}, "verifier_exit_error"),
])
def test_exit_zero_cannot_override_abnormal_facts(facts: dict, reason: str):
    """facts 为异常事实覆盖，验证退出0也不能覆盖 reason 指向的问题；返回 None。"""
    data = {"exit_code": 0, "cleanup_status": "not_needed", "error_code": None, **facts}
    result = _map_result(data, 9)
    assert (result.status, result.reason) == ("error", reason)


def test_verifier_receives_only_fixed_host_context(tmp_path: Path, monkeypatch):
    """验证固定宿主变量传入准确且不继承密钥，也不改写父环境；返回 None。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-secret-must-not-inherit")
    monkeypatch.setenv("HARNESS_WORKSPACE", "stale-parent-workspace")
    monkeypatch.setenv("HARNESS_RUN_ID", "stale-parent-run")
    identifier = uuid4()
    verifier = Verifier(RunLimits(), tmp_path)
    spec = VerifierSpec(argv=[sys.executable, "-c", "import os,json;print(json.dumps(dict(os.environ)))"], cwd=tmp_path)
    result = asyncio.run(verifier.run(spec, tmp_path, identifier))
    environment = json.loads(result.stdout)
    assert environment["HARNESS_WORKSPACE"] == str(tmp_path)
    assert environment["HARNESS_RUN_ID"] == str(identifier)
    assert "dummy-secret-must-not-inherit" not in result.stdout
    assert "DEEPSEEK_API_KEY" not in environment
    assert os.environ["HARNESS_WORKSPACE"] == "stale-parent-workspace"
    assert os.environ["HARNESS_RUN_ID"] == "stale-parent-run"


@pytest.mark.parametrize("extra", [
    {"DEEPSEEK_API_KEY": "not-allowed"}, {"PYTHONPATH": "not-allowed"},
    {"HARNESS_RUN_ID": 1}, {"HARNESS_WORKSPACE": "bad\x00path"},
])
def test_host_context_rejects_arbitrary_injection_before_capture(tmp_path: Path, extra: dict):
    """extra 为非法宿主覆盖，验证创建输出或启动进程前拒绝；返回 None。"""
    process = ProcessRunner(RunLimits(), tmp_path / "artifacts")
    with pytest.raises(ValueError):
        asyncio.run(process.run([sys.executable, "-c", "print('not reached')"], tmp_path, extra_env=extra))
    assert not (tmp_path / "artifacts").exists()



def test_project_verifier_imports_worktree_code_without_parent_injection(tmp_path: Path, monkeypatch):
    """让父环境指向源代码，验证显式搜索路径仍加载任务副本，且不继承密钥；返回 None。"""
    source = tmp_path / "source" / "src"
    source.mkdir(parents=True)
    (source / "project_marker.py").write_text("VALUE = 'source'", encoding="utf-8")
    worktree = tmp_path / "worktree"
    imports = worktree / "src"
    imports.mkdir(parents=True)
    (imports / "project_marker.py").write_text("VALUE = 'worktree'", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(source))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "parent-secret")
    program = "import os,json,project_marker;print(json.dumps({'value':project_marker.VALUE,'file':project_marker.__file__,'cwd':os.getcwd(),'key_present':'DEEPSEEK_API_KEY' in os.environ}))"
    verifier = Verifier(RunLimits(), tmp_path / "run")
    spec = VerifierSpec(argv=[sys.executable, "-c", program], cwd=worktree, python_paths=[imports])
    result = asyncio.run(verifier.run(spec, worktree, uuid4()))
    assert result.status == "passed"
    facts = json.loads(result.stdout)
    assert facts["value"] == "worktree"
    assert Path(facts["file"]).resolve().is_relative_to(worktree.resolve())
    assert Path(facts["cwd"]).resolve() == worktree.resolve()
    assert facts["key_present"] is False
    assert os.environ["PYTHONPATH"] == str(source)


@pytest.mark.parametrize("case", ["outside", "relative", "file", "separator"])
def test_process_rejects_invalid_python_paths_before_creating_capture(tmp_path: Path, case: str):
    """case 指定越界、相对、文件或搜索分隔符路径，要求未创建输出或启动进程便拒绝；返回 None。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    file = worktree / "module.py"
    file.write_text("", encoding="utf-8")
    invalid = {"outside": tmp_path, "relative": Path("src"), "file": file, "separator": worktree / ("inside" + os.pathsep + "injected")}[case]
    process = ProcessRunner(RunLimits(), tmp_path / "artifacts")
    with pytest.raises(ValueError):
        asyncio.run(process.run([sys.executable, "-c", "print('not reached')"], worktree, python_paths=[invalid]))
    assert not (tmp_path / "artifacts").exists()
