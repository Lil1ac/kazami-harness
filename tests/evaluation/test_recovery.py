"""恢复协议的证据篡改、暂停结算、局部失效及事务中断验收；无外部模型或 Docker。"""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_harness.config import load_config
from agent_harness.evaluation.campaign_budget import CampaignBudget, atomic_json
from agent_harness.evaluation.preflight import make_certificate, verify_certificate
from agent_harness.evaluation.protocols import ENVIRONMENT_SOURCES, digest, environment_contract, file_hash
from agent_harness.evaluation.recovery import apply_plan, check_ledger, connection_probe, reconcile_report
from agent_harness.runtime.runner import AgentStopped
from tests.evaluation.test_three_arm import usage
from tests.evaluation.test_transport_hold import _two_approved_holds


def fixture(root: Path, case_id: str = "example-1") -> tuple[Path, dict, dict]:
    """在 root 生成一份有原件的零模型预检，返回目录、记录和配置；case_id 标识独立题目。"""
    config = load_config(Path("evals/swe-three-arm/deepseek.toml")).model_dump(mode="json")
    record = {"instance_id": case_id, "repo": "example/repo", "base_commit": "abc",
              "patch": "gold", "test_patch": "test", "FAIL_TO_PASS": '["bug"]', "PASS_TO_PASS": '["regression"]'}
    folder = root / ".evaluations" / str(uuid4())
    baseline = {"status": "failed", "official": {"tests_status": {
        "FAIL_TO_PASS": {"failure": ["bug"]}, "PASS_TO_PASS": {"success": ["regression"]}}}}
    environment = {case_id: {"image_id": "sha256:fixed", "source_sha256": "source"}}
    source = Path("src/agent_harness").resolve()
    protocol = {"frozen": [{"id": case_id, "record": record}], "config": config,
                "runtime_hashes": {key: file_hash(source / key) for key in ENVIRONMENT_SOURCES},
                "experiment": {"groups": []}, "repository_environments": environment}
    report = {"id": folder.name, "state": "preflight_passed", "mode": "preflight", "trials": [],
              "environments": environment, "protocol_sha256": digest(protocol),
              "preflight": [{"case_id": case_id, "baseline": baseline, "reference": {"status": "passed"},
                             "workspace_verified": True, "baseline_valid": True}]}
    atomic_json(folder / "protocol.json", protocol)
    atomic_json(folder / "report.json", report)
    atomic_json(folder / "preflight" / case_id / "baseline" / "official.json", baseline)
    atomic_json(folder / "preflight" / case_id / "reference" / "official.json", {"resolved": True})
    return folder, record, config


def test_fifty_certificates_survive_accounting_revision_and_one_case_change(tmp_path):
    """50题证据复用不运行容器；计费源码变更不影响环境身份，一题补丁变化只拒绝该题。"""
    entries = []
    for index in range(50):
        folder, record, config = fixture(tmp_path, f"example-{index}")
        cert = make_certificate(folder)
        entries.append((cert, record, config))
    source = Path("src/agent_harness").resolve()
    before = {key: file_hash(source / key) for key in ENVIRONMENT_SOURCES}
    after = {**before, "evaluation/campaign_budget.py": "changed accounting"}
    cert, record, config = entries[0]
    assert environment_contract(record, config, sources=before) == environment_contract(record, config, sources=after)
    for cert, record, config in entries:
        verify_certificate(tmp_path, cert, [{"id": record["instance_id"], "record": record}], config)
    altered = {**entries[9][1], "test_patch": "changed test"}
    with pytest.raises(ValueError, match="该题环境契约变化"):
        verify_certificate(tmp_path, entries[9][0], [{"id": altered["instance_id"], "record": altered}], entries[9][2])


@pytest.mark.parametrize("target", ["report.json", "protocol.json", "preflight/example-1/reference/official.json"])
def test_tampered_original_rejected(tmp_path, target):
    """分别破坏报告、协议和评分原件；target 是相对路径，任何篡改均拒绝复用。"""
    folder, record, config = fixture(tmp_path)
    cert = make_certificate(folder)
    with (folder / target).open("a", encoding="utf-8") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="摘要变化"):
        verify_certificate(tmp_path, cert, [{"id": record["instance_id"], "record": record}], config)


def test_old_certificate_review_does_not_bypass_original_integrity(tmp_path):
    """显式旧源码清单只用于审查；默认新代码拒绝旧证书，原件篡改也不能借审查参数通过。"""
    folder, record, config = fixture(tmp_path)
    protocol = json.loads((folder / "protocol.json").read_text(encoding="utf-8"))
    protocol["runtime_hashes"]["evaluation/repository.py"] = "previous-frozen-source"
    atomic_json(folder / "protocol.json", protocol)
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    report["protocol_sha256"] = digest(protocol)
    atomic_json(folder / "report.json", report)
    cert = make_certificate(folder)
    records = [{"id": record["instance_id"], "record": record}]
    with pytest.raises(ValueError, match="环境契约变化"):
        verify_certificate(tmp_path, cert, records, config)
    verify_certificate(tmp_path, cert, records, config, sources=protocol["runtime_hashes"])
    (folder / "preflight/example-1/reference/official.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="摘要变化"):
        verify_certificate(tmp_path, cert, records, config, sources=protocol["runtime_hashes"])


def test_pause_settles_inflight_then_refuses_new_admission(tmp_path):
    """暂停时允许已发请求结算，下一次发单前拒绝；已批准两笔未知预留仍完整保留。"""
    budget = _two_approved_holds(tmp_path)
    budget.bind("formal", {}, lambda: None)
    budget.authorize("inflight", 1, "execution")
    atomic_json(tmp_path / "pause.json", {"reason": "user_requested"})
    budget.settle("inflight", 1, usage(), "deepseek-flash")
    with pytest.raises(AgentStopped, match="campaign_paused"):
        budget.authorize("new", 1, "execution")
    assert budget.state["calls_admitted"] == 3
    assert budget.state["reserved_usd_picos"] == 700_000_000_000
    check_ledger(tmp_path, budget.state)
    assert budget.state["requests"][-1]["status"] == "settled"


def test_new_unknown_cannot_be_cleared_by_recovery(tmp_path):
    """已有两笔例外不扩大到新未知请求；只读恢复审计拒绝且不更改账本字节。"""
    budget = _two_approved_holds(tmp_path)
    budget.bind("formal", {}, lambda: None)
    budget.authorize("new", 1, "execution")
    budget.settle("new", 1, None, None, "model_transport")
    before = (tmp_path / "ledger.json").read_bytes()
    with pytest.raises(ValueError, match="新未知"):
        check_ledger(tmp_path, budget.state)
    assert (tmp_path / "ledger.json").read_bytes() == before


def test_valid_failure_kept_partial_attempt_gets_new_identity(tmp_path):
    """有效失败不复跑；崩溃请求保留诊断，只有未完成槽位换身份，重复恢复不再换。"""
    rows = [{"run_id": "failed", "comparison_valid": True, "success": False},
            {"run_id": "broken", "comparison_valid": False, "interrupted": True}]
    report = {"trials": rows, "schedule": [{"run_id": "failed"}, {"run_id": "broken"}, {"run_id": "unused"}],
              "active_trial": None}
    ledger = {"requests": [{"run_id": "broken"}]}
    result = reconcile_report(report, ledger, tmp_path)
    assert result["trials"] == rows[:1] and result["diagnostic_trials"] == rows[1:]
    assert result["schedule"][0]["run_id"] == "failed" and result["schedule"][2]["run_id"] == "unused"
    assert result["schedule"][1]["run_id"] != "broken"
    assert reconcile_report(result, ledger, tmp_path) == result


def test_recovery_transaction_survives_partial_write(tmp_path, monkeypatch):
    """第二次落盘失败后从同一计划续写，重复应用不修改文件，也不生成模型请求。"""
    from agent_harness.evaluation import recovery
    atomic_json(tmp_path / "one.json", {"value": 1})
    atomic_json(tmp_path / "two.json", {"value": 2})
    plan = {"root": str(tmp_path.resolve()), "expected": {name: file_hash(tmp_path / name) for name in ("one.json", "two.json")},
            "writes": {"one.json": {"value": 3}, "two.json": {"value": 4}}}
    real = recovery.atomic_json

    def fail_second(path, value):
        """仅第二个目标模拟磁盘失败；其余写入交真实原子函数，返回 None。"""
        if path == tmp_path / "two.json":
            raise OSError("simulated disk failure")
        real(path, value)

    monkeypatch.setattr(recovery, "atomic_json", fail_second)
    with pytest.raises(OSError):
        apply_plan(tmp_path, plan)
    monkeypatch.setattr(recovery, "atomic_json", real)
    identifier = apply_plan(tmp_path, plan)
    snapshots = {name: (tmp_path / name).read_bytes() for name in plan["writes"]}
    assert apply_plan(tmp_path, plan) == identifier
    assert all((tmp_path / name).read_bytes() == raw for name, raw in snapshots.items())


def test_probe_uses_get_and_has_no_retry_or_sensitive_diagnostics():
    """认证探测只允许 GET /models；403 不重试，异常信息不包含密钥或响应正文。"""
    config = load_config(Path("evals/swe-three-arm/deepseek.toml"))
    calls = []

    def respond(request):
        """记录请求方法和地址，返回拒绝认证；不记录认证头，返回模拟响应。"""
        calls.append((request.method, request.url.path))
        return httpx.Response(403, text="sensitive server response")

    with pytest.raises(ValueError, match="HTTP 403") as raised:
        asyncio.run(connection_probe(config, "offline-secret", transport=httpx.MockTransport(respond)))
    assert calls == [("GET", "/v1/models")]
    assert "offline-secret" not in str(raised.value) and "sensitive" not in str(raised.value)


def test_http_report_projects_pause_and_original_links(tmp_path):
    """真实 FastAPI 路由读取恢复报告并投影整轮暂停；GET 不改证据、不发模型请求。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from agent_harness.web.evaluations import install_evaluation_routes

    root = tmp_path / "runs"
    identifier = str(uuid4())
    original = str(uuid4())
    path = root / ".evaluations" / identifier / "report.json"
    atomic_json(path, {"id": identifier, "summary": {}, "state": "incomplete", "active_trial": None,
                      "recovery": {"status": "ready", "fresh": 0, "reused": 50, "recheck": 0,
                                   "evidence": [{"case_id": "example-1", "report_id": original}]}})
    atomic_json(tmp_path / "campaign.json", {"stop_reason": "campaign_paused", "protocol_sha256": "fixed",
                                            "phases": {"formal": {"report_id": identifier}}})
    atomic_json(tmp_path / "ledger.json", {"protocol_sha256": "fixed", "known_usd_picos": 2926459284000,
                                          "reserved_usd_picos": 700000000000})
    before = path.read_bytes()
    app = FastAPI()
    install_evaluation_routes(app, SimpleNamespace(runs_root=root))
    with TestClient(app) as client:
        response = client.get("/api/evaluations/" + identifier)
    assert response.status_code == 200
    body = response.json()
    assert body["recovery"]["status"] == "paused" and body["stop_reason"] == "campaign_paused"
    assert body["recovery"]["evidence"][0]["report_id"] == original
    assert body["budget"]["known_usd_picos"] == 2926459284000 and body["budget"]["reserved_usd_picos"] == 700000000000
    assert path.read_bytes() == before
