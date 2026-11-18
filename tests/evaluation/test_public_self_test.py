"""覆盖冻结公开自测的候选证明、执行器入口与失败回执。"""

import asyncio
import hashlib
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

from agent_harness.evaluation.public_self_test import (
    PYTEST_SELF_TEST_ARGV, PYTEST_SELF_TEST_NODE, archive_pytest_candidate, pytest_self_test_result,
)
from agent_harness.evaluation.repository import RepositoryEnvironment, RepositoryExecutor
from tests.helpers.models import configured_run


@pytest.mark.parametrize(("process", "status", "counts", "expected"), [
    ({"exit_code": 0}, "passed", [1, 1, 0], ("passed", None)),
    ({"exit_code": 0}, "passed", [0, 0, 0], ("error", "self_test_environment")),
    ({"exit_code": 1}, "failed", [1, 1, 0], ("failed", "self_test_assertion_failed")),
    ({"exit_code": 5}, "no_tests", [0, 0, 0], ("no_tests", "self_test_no_tests")),
    ({"exit_code": 0}, "all_skipped", [1, 0, 1], ("all_skipped", "self_test_all_skipped")),
    ({"exit_code": 0, "timed_out": True}, "passed", [1, 1, 0], ("timed_out", "self_test_timeout")),
    ({"exit_code": 0, "output_limit_exceeded": True}, "passed", [1, 1, 0], ("error", "self_test_output_limit")),
])
def test_pytest_outcomes_require_matching_exit_and_executed_tests(process, status, counts, expected):
    """构造 status/counts 的受控报告供 process 核验；断言通过证据、执行失败及零执行各自明确。"""
    reasons = {"passed": None, "failed": "self_test_assertion_failed", "no_tests": "self_test_no_tests",
               "all_skipped": "self_test_all_skipped"}
    marker = "HARNESS_PUBLIC_PYTEST " + json.dumps({
        "status": status, "reason": reasons[status], "tests_collected": counts[0],
        "tests_run": counts[1], "tests_skipped": counts[2],
    })
    fact = pytest_self_test_result(process, "ordinary pytest log\n" + marker + "\n")
    assert (fact["status"], fact["reason"]) == expected


def test_truncated_candidate_run_is_not_shared_environment_failure():
    """候选导入阶段提前退出时保留候选错误，普通成功样式的 stdout 不能证明自测通过。"""
    process = {"exit_code": 0}
    fake_output = "1 passed in 0.05s"
    assert pytest_self_test_result(process, fake_output)["reason"] == "self_test_environment"
    assert pytest_self_test_result(process, "HARNESS_PUBLIC_PYTEST_CANDIDATE\n" + fake_output)["reason"] == (
        "self_test_candidate_test_error"
    )


def test_candidate_archives_ignore_git_but_detect_source_changes(tmp_path):
    """在 tmp_path 复用候选归档目标，不以 Git 目录变化代替源码字节身份。"""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").mkdir()
    (work / ".git" / "config").write_text("ignored", encoding="utf-8")
    source = work / "answer.py"
    source.write_bytes(b"x=1\n")
    target = tmp_path / "c.tar"
    before = archive_pytest_candidate(work, target)
    (work / ".git" / "config").write_text("still ignored", encoding="utf-8")
    assert archive_pytest_candidate(work, target) == before
    with tarfile.open(target) as archive:
        assert all(".git" not in Path(member.name).parts for member in archive)
    source.write_bytes(b"x=2\n")
    assert archive_pytest_candidate(work, target) != before


def test_repository_public_self_test_mounts_only_frozen_test_and_candidate(tmp_path, monkeypatch):
    """模拟容器检查只读挂载，验证 tmp_path 下基线与版本文件变化会阻止启动自测。"""
    record = {"instance_id": "pytest-dev__pytest-5262", "repo": "pytest-dev/pytest"}
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "answer.py").write_bytes(b"x=1\n")
    environment = RepositoryEnvironment(record, tmp_path / "env", configured_run(), tmp_path / "helper",
                                        public_self_test=True)
    environment.directory.mkdir()
    environment.archive.write_bytes(b"frozen baseline")
    environment.public_version_file.write_bytes(b"frozen version")
    environment._baseline_source_sha256 = hashlib.sha256(environment.archive.read_bytes()).hexdigest()
    environment.public_self_test_metadata = {"selector": PYTEST_SELF_TEST_NODE, "version": "version",
                                            "version_sha256": hashlib.sha256(b"frozen version").hexdigest()}
    live = []

    async def container(command, mounts, seconds, *, deadline=None):
        """核对 command、mounts 和正向 seconds 后返回真正结构化的模拟容器回执。"""
        assert command[-3] == PYTEST_SELF_TEST_NODE and deadline is not None and 0 < seconds <= 30
        assert [entry.rsplit("target=", 1)[-1] for entry in mounts[1::2]] == [
            "/candidate.tar,readonly", "/baseline-source.tar,readonly", "/pytest-version.py,readonly",
        ]
        assert all("record.json" not in item and "spec.json" not in item and "/evidence" not in item for item in mounts)
        live.append(command)
        return {"exit_code": 0}

    def stdout(process):
        """为模拟 process 返回单个受控公开测试报告；不依赖宿主临时文件。"""
        assert process["exit_code"] == 0
        return 'HARNESS_PUBLIC_PYTEST {"status":"passed","reason":null,"tests_collected":1,"tests_run":1,"tests_skipped":0}'

    monkeypatch.setattr(environment, "container", container)
    monkeypatch.setattr(environment, "full_stdout", stdout)
    assert asyncio.run(environment.verify_public_self_test(workspace))["status"] == "passed"
    assert sorted(p.name for p in workspace.iterdir()) == ["answer.py"]
    environment.public_version_file.write_bytes(b"tampered metadata")
    assert asyncio.run(environment.verify_public_self_test(workspace))["reason"] == "self_test_environment"
    assert len(live) == 1


def test_repository_executor_self_test_emits_artifacts_and_invalidates_stale_result(tmp_path):
    """通过实际 run_command 工具接口使用固定入口，检查双流复制及候选变化失效。"""
    work = tmp_path / "candidate"
    work.mkdir()
    source = work / "answer.py"
    source.write_bytes(b"x=1\n")
    config = configured_run(tools=["run_command"])
    environment = SimpleNamespace(directory=tmp_path / "env", public_self_test_metadata={"selector": PYTEST_SELF_TEST_NODE})
    environment.directory.mkdir()
    logs = environment.directory / "processes" / "commands" / "one"
    logs.mkdir(parents=True)
    (logs / "stdout.bin").write_bytes(b"trusted result")
    events = []

    async def run_public_self_test(workspace, seconds):
        """为受测 workspace 生成真实归档，再返回经过独立 fixture 校验的通过回执。"""
        assert 0 < seconds <= 30
        digest = archive_pytest_candidate(workspace, tmp_path / "verified.tar")
        return ({"candidate_sha256": digest, "tests_run": 1, "tests_collected": 1,
                 "exit_code": 0, "status": "passed", "reason": None},
                {"stdout_ref": "artifacts/commands/one/stdout.bin", "cleanup_status": "complete"})

    def allow(name):
        """固定工具权限，只接纳 name 为 run_command 的操作。"""
        assert name == "run_command"
        return "allow"

    def emit(event, payload):
        """保存 event 与 payload 的发出顺序，供同一次自测标识核对。"""
        events.append((event, payload))

    environment._run_public_self_test = run_public_self_test
    executor = RepositoryExecutor(environment, work, tmp_path / "r", config)
    executor.permissions = SimpleNamespace(decision=allow)
    run_root = tmp_path / "r" / "run"
    run_root.mkdir(parents=True)
    executor.bind_run(run_root)
    executor.bind_public_self_test_events(emit)
    receipt = asyncio.run(executor.execute("run_command", json.dumps({"argv": PYTEST_SELF_TEST_ARGV})))
    assert receipt["ok"]
    fact = receipt["data"]["public_self_test"]
    assert fact["source"] == "model" and fact["selector"] == PYTEST_SELF_TEST_NODE
    assert fact == executor.public_self_test_facts() and len(events) == 2
    assert events[0][0] == "public_self_test_started" and events[1][0] == "public_self_test_finished"
    assert events[0][1]["self_test_id"] == fact["self_test_id"]
    assert (executor.run_dir / receipt["artifact_refs"][0]["path"]).read_bytes() == b"trusted result"
    source.write_bytes(b"x=2\n")
    assert executor.public_self_test_facts(emit_invalidation=False) is None and len(events) == 2
    assert executor.public_self_test_facts() is None
    event, invalidated = events[-1]
    assert event == "public_self_test_invalidated" and invalidated["self_test_id"] == fact["self_test_id"]
    assert invalidated["reason"] == "candidate_changed"
