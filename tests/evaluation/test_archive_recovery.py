"""Linux 链接归档与同一次执行补评分的离线回归；不调用模型或 Docker。"""

from copy import deepcopy
import io
import asyncio
import json
import os
from pathlib import Path
import struct
import tarfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_harness.evaluation.archive import CandidateArchive, LX_SYMLINK_TAG, decode_lx_link
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.evaluation.grading_recovery import adopt_grading
from agent_harness.evaluation.protocols import digest, file_hash
from agent_harness.evaluation.recovery import apply_plan, reconcile_report


@pytest.mark.parametrize("raw", [b"", struct.pack("<IHHI", 0, 8, 0, 2) + b"file",
                                 struct.pack("<IHHI", LX_SYMLINK_TAG, 4, 0, 2),
                                 struct.pack("<IHHI", LX_SYMLINK_TAG, 8, 0, 3) + b"file"])
def test_bad_link_metadata_fails_closed(raw):
    """未知或截断 raw 不得当作普通文件继续读取，必须抛 ValueError。"""
    with pytest.raises(ValueError):
        decode_lx_link(raw)


def test_plain_archive_is_byte_identical(tmp_path):
    """普通文件与目录必须生成和标准库逐字节一致的归档，含忽略文件也不自行过滤。"""
    (tmp_path / "build").mkdir()
    (tmp_path / "build/data").write_bytes(b"kept in archive")
    (tmp_path / ".gitignore").write_bytes(b"build\n")
    values = []
    for cls in (tarfile.TarFile, CandidateArchive):
        content = io.BytesIO()
        with cls.open(fileobj=content, mode="w", dereference=False) as archive:
            archive.add(tmp_path, arcname=".")
        values.append(content.getvalue())
    assert values[0] == values[1]


@pytest.mark.skipif(os.name != "nt", reason="LX重解析点是Windows边界")
def test_lx_link_never_reads_target_or_file_content(tmp_path, monkeypatch):
    """模拟 Windows 将 LX 链接识别为普通文件，实际调用归档入口，确认只写链接元数据。"""
    import builtins
    import agent_harness.evaluation.archive as module
    path = tmp_path / "link"
    path.write_bytes(b"must not be read")
    original_stat, original_open = os.lstat, builtins.open

    def metadata(name, *args, **kwargs):
        """仅为目标 name 注入 LX 标记，其余 lstat 参数原样转发并返回元数据。"""
        value = original_stat(name, *args, **kwargs)
        if Path(name) == path:
            fields = {key: getattr(value, key) for key in dir(value) if key.startswith("st_")}
            return SimpleNamespace(**{**fields, "st_reparse_tag": LX_SYMLINK_TAG})
        return value

    def guarded_open(name, *args, **kwargs):
        """若归档错误读取链接普通内容立即失败；其他文件按原 open 处理。"""
        if name == path or name == str(path):
            raise AssertionError("LX链接不能作为普通文件读取")
        return original_open(name, *args, **kwargs)

    monkeypatch.setattr(module.os, "lstat", metadata)
    monkeypatch.setattr(module, "read_lx_link", lambda name: "../missing-target")
    monkeypatch.setattr(builtins, "open", guarded_open)
    content = io.BytesIO()
    with CandidateArchive.open(fileobj=content, mode="w", dereference=False) as archive:
        archive.add(path, arcname="link")
    content.seek(0)
    with tarfile.open(fileobj=content) as archive:
        member = archive.getmember("link")
        assert member.issym() and member.linkname == "../missing-target" and member.size == 0


def grading_fixture(root: Path, passed: bool) -> tuple[dict, dict, str]:
    """在 root 构造完整原执行与独立补评分，passed 决定官方终态；返回父报告、账本和回执编号。"""
    identifier, run, original, parent = (str(uuid4()) for _ in range(4))
    row = {"run_id": run, "case_id": "fixture-1", "strategy": "A", "repeat": 0,
           "comparison_valid": False, "execution_status": "completed", "usage_complete": True,
           "success": False, "model_calls": 1, "known_tokens": 17, "cost": {"complete": True, "known_usd_picos": 42},
           "grade": {"status": "error", "reason": "artifact_unreadable"}, "grading_seconds": .1, "duration_seconds": 9}
    environment = {"image_id": "immutable-image", "source_sha256": "frozen-source"}
    source = root / "runs/.evaluations" / original
    atomic_json(source / "report.json", {"trials": [row], "environments": {"fixture-1": environment}})
    row = {**row, "source_report_id": original}
    folder = root / "grading-recoveries" / identifier
    folder.mkdir(parents=True)
    (folder / "candidate.patch").write_bytes(b"unchanged-model-patch")
    (folder / "test_output.txt").write_bytes(b"official test output")
    official = {"resolved": passed, "tests_status": {"FAIL_TO_PASS": {"success" if passed else "failure": ["bug"]}}}
    atomic_json(folder / "official.json", official)
    atomic_json(folder / "receipt.json", {"run_id": run, "case_id": "fixture-1", "source_report_id": original,
        "original_row_sha256": digest(row), "source_report_sha256": file_hash(source / "report.json"),
        "protocol_sha256": "approved", "external_model_requests": 0, "environment": environment,
        "artifacts": {name: file_hash(folder / name) for name in ("candidate.patch", "official.json", "test_output.txt")},
        "grade": {"status": "passed" if passed else "failed", "official": official}, "grading_seconds": 1.5})
    report = {"id": parent, "trials": [row], "schedule": [{"run_id": run}], "active_trial": None}
    ledger = {"requests": [{"run_id": run, "turn": 1, "status": "settled", "known_usd_picos": 42}]}
    return report, ledger, identifier


@pytest.mark.parametrize("passed", [True, False])
def test_grade_adoption_preserves_execution_and_skips_rerun(tmp_path, passed):
    """补评分通过或失败都成为有效终态；原请求、费用与运行身份不变，调度不重跑此槽位。"""
    report, ledger, identifier = grading_fixture(tmp_path, passed)
    original = deepcopy(report["trials"][0])
    ledger_before = deepcopy(ledger)
    expected, copies = {}, {}
    adopt_grading(tmp_path, report, identifier, "approved", ledger, expected, copies)
    result = reconcile_report(report, ledger, tmp_path / "work")
    restored = result["trials"][0]
    assert restored["comparison_valid"] and restored["success"] == passed
    assert result["schedule"][0]["run_id"] == original["run_id"] and not result["diagnostic_trials"]
    assert ledger == ledger_before
    for key in ("run_id", "model_calls", "known_tokens", "cost", "execution_status"):
        assert restored[key] == original[key]
    assert len(copies) == 3 and restored["grading_recovery"]["external_model_requests"] == 0


def test_grade_artifact_tamper_rejects_adoption(tmp_path):
    """补丁在补评分后发生变化时，不得采纳成绩或标记原执行有效。"""
    report, ledger, identifier = grading_fixture(tmp_path, True)
    (tmp_path / "grading-recoveries" / identifier / "candidate.patch").write_bytes(b"changed")
    with pytest.raises(ValueError, match="摘要不一致"):
        adopt_grading(tmp_path, report, identifier, "approved", ledger, {}, {})
    assert not report["trials"][0]["comparison_valid"]


def test_copied_artifacts_and_report_resume_after_crash(tmp_path, monkeypatch):
    """产物复制后、报告发布前失败，重用同一事务只补报告，原件和金额保持不变。"""
    import agent_harness.evaluation.recovery as module
    source = tmp_path / "original.patch"
    source.write_bytes(b"model patch")
    atomic_json(tmp_path / "report.json", {"state": "old"})
    plan = {"root": str(tmp_path.resolve()), "expected": {"original.patch": file_hash(source),
            "copied.patch": None, "report.json": file_hash(tmp_path / "report.json")},
            "writes": {"report.json": {"state": "restored"}},
            "copies": {"copied.patch": {"source": "original.patch", "sha256": file_hash(source)}}}
    save = module.atomic_json

    def fail_report(path, value):
        """仅发布目标报告时模拟失败；其他审计写入保持真实落盘行为。"""
        if path == tmp_path / "report.json":
            raise OSError("publication interrupted")
        save(path, value)

    monkeypatch.setattr(module, "atomic_json", fail_report)
    with pytest.raises(OSError):
        apply_plan(tmp_path, plan)
    monkeypatch.setattr(module, "atomic_json", save)
    apply_plan(tmp_path, plan)
    assert (tmp_path / "copied.patch").read_bytes() == source.read_bytes()
    assert json.loads((tmp_path / "report.json").read_text()) == {"state": "restored"}


def test_campaign_partial_case_only_admits_remaining_groups(tmp_path, monkeypatch):
    """真实调度器恢复已有 A 成绩的一题，仅准入 B/C；再次调用同阶段也不重复请求。"""
    import agent_harness.evaluation.campaign as module
    from agent_harness.evaluation.campaign import Campaign
    from agent_harness.evaluation.engine import save_report
    from tests.evaluation.test_three_arm import usage

    campaign = Campaign(tmp_path / "round", Path("evals/swe-three-arm"), tmp_path / "work", Path(__file__))
    monkeypatch.setattr(campaign, "docker", lambda args: "")
    monkeypatch.setattr(campaign, "cleanup", lambda report: None)
    campaign.initialize()
    identifier = "matplotlib__matplotlib-26113"
    report = campaign.aggregate("formal", [identifier], campaign.experiment("formal"))
    a = next(slot for slot in report["schedule"] if slot["strategy"] == "A")
    campaign.budget.bind("formal", report, lambda: None)

    def finished(slot):
        """对 slot 结算一条真实守卫请求并构造有效终态；返回评分行，无网络调用。"""
        campaign.budget.authorize(slot["run_id"], 1, "execution")
        campaign.budget.settle(slot["run_id"], 1, usage(), "deepseek-flash")
        return {**slot, "success": True, "comparison_valid": True, "usage_complete": True,
                "model_calls": 1, "http_attempts": 1, "known_tokens": 120, "needs_input": False,
                "execution_status": "completed", "stop_reason": None, "grade": {"status": "passed"},
                "duration_seconds": 1, "cost": campaign.budget.run_cost(slot["run_id"])}

    report["trials"].append(finished(a))
    save_report(campaign.runs_root / ".evaluations" / report["id"], report)
    campaign.state["preflight"]["formal:" + identifier] = {"passed": True, "report_id": str(uuid4()), "certificate_sha256": "fixture"}
    observed = []

    async def evaluate(suite_path, config, experiment, runs_root, work_root, api_key, **options):
        """仅替代模型/容器边界，核对真实调度器传入剩余槽位并生成可合并报告。"""
        slots = options["frozen_schedule"]
        assert {slot["strategy"] for slot in slots} == {"B", "C"}
        assert options["completed_schedule"] == [a]
        observed.extend(slot["strategy"] for slot in slots)
        child = {**report, "id": str(uuid4()), "schedule": slots, "trials": [finished(slot) for slot in slots],
                 "state": "completed", "stop_reason": None, "active_trial": None}
        return child

    monkeypatch.setattr(module, "evaluate", evaluate)
    result = asyncio.run(campaign.run_phase("formal", [identifier], "offline"))
    assert len(result["trials"]) == 3 and set(observed) == {"B", "C"}
    assert campaign.budget.state["calls_admitted"] == 3
    asyncio.run(campaign.run_phase("formal", [identifier], "offline"))
    assert campaign.budget.state["calls_admitted"] == 3 and len(observed) == 2
