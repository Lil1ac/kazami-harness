"""直接通过真实验收适配器检查固定日志报告，覆盖有效和畸形产物。"""

import asyncio
import importlib.util
from pathlib import Path
from uuid import uuid4
import sys

import pytest

from agent_harness.schemas import RunLimits, VerifierSpec
from agent_harness.verifier import Verifier


CHECKER = Path(__file__).resolve().parents[1] / "examples/verifiers/verify_logs.py"  # 宿主维护的固定验收入口。
VALID = '{"counts":{"timeout":3,"database":2,"auth":1},"total_errors":6}'  # 九行样例中的六个ERROR。


@pytest.mark.parametrize("report,expected", [
    (VALID, "passed"),
    (None, "failed"),
    ("{broken", "failed"),
    ('[]', "failed"),
    (VALID.replace('"timeout":3', '"timeout":2'), "failed"),
    (VALID.replace('"auth":1', '"auth":true'), "failed"),
    (VALID.replace('"timeout":3', '"timeout":3.0'), "failed"),
    (VALID.replace('"total_errors":6', '"total_errors":7'), "failed"),
    (VALID.replace('"auth":1', '"other":1'), "failed"),
    (VALID.replace('"total_errors":6', '"total_errors":false'), "failed"),
    (VALID.replace('"total_errors":6', '"total_errors":0,"total_errors":6'), "failed"),
    (VALID.replace('"auth":1', '"auth":0,"auth":1'), "failed"),
])
def test_fixed_checker_decides_from_report(tmp_path: Path, report: str | None, expected: str):
    """report=None 表示文件缺失；否则写报告，核对真实宿主检查结果 expected；返回 None。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if report is not None:
        (workspace / "report.json").write_text(report, encoding="utf-8")
    verifier = Verifier(RunLimits(), tmp_path / "run")
    spec = VerifierSpec(argv=[sys.executable, str(CHECKER)], cwd=CHECKER.parent)
    result = asyncio.run(verifier.run(spec, workspace, uuid4()))
    assert result.status == expected
    assert result.exit_code == (0 if expected == "passed" else 1)
    assert '"status": "' + expected + '"' in result.stdout
    assert not result.stderr


def test_checker_internal_failure_uses_protocol_error_code(tmp_path: Path, monkeypatch, capsys):
    """注入内部实现故障，确认返回2且不回显异常正文；返回 None。"""
    spec = importlib.util.spec_from_file_location("host_log_checker", CHECKER)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    monkeypatch.setenv("HARNESS_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HARNESS_RUN_ID", str(uuid4()))

    def broken_check(workspace):
        """workspace 为任务路径；抛模拟实现异常，不执行产物断言。"""
        raise RuntimeError("private-checker-error")

    monkeypatch.setattr(checker, "check_report", broken_check)
    assert checker.main() == 2
    output = capsys.readouterr()
    assert "验收器内部异常" in output.err
    assert "private-checker-error" not in output.out + output.err


def test_checker_without_host_context_is_error(tmp_path: Path):
    """不传两项宿主环境时运行入口，确认配置问题返回error而非产物failed；返回 None。"""
    process = Verifier(RunLimits(), tmp_path)
    # 这里有意直接运行底层执行器，绕过自动补充上下文的 Verifier.run。
    result = asyncio.run(process._process.run([sys.executable, str(CHECKER)], CHECKER.parent))
    assert result["exit_code"] == 2
    assert "缺少宿主验收上下文" in result["stderr"]


def test_checker_rejects_oversized_or_nonfile_report(tmp_path: Path):
    """报告超过1MiB或为目录时均不判通过，且保留源文件；返回 None。"""
    spec = importlib.util.spec_from_file_location("bounded_log_checker", CHECKER)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    report = tmp_path / "report.json"
    report.write_bytes(b" " * (checker.MAX_REPORT_BYTES + 1))
    assert checker.check_report(tmp_path)
    assert report.stat().st_size == checker.MAX_REPORT_BYTES + 1
    report.unlink()
    report.mkdir()
    assert checker.check_report(tmp_path)
