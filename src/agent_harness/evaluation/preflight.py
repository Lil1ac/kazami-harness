"""仓库零模型验证及不可变证据证书；与模型、上下文策略和计费版本解耦。"""

import json
from pathlib import Path
from uuid import UUID, uuid4

from agent_harness.evaluation.protocols import digest, environment_contract, file_hash


def baseline_valid(record: dict, outcome: dict) -> bool:
    """核对原始缺陷 outcome 是否精确复现 record 的测试分组，返回布尔值。"""
    tests = outcome.get("official", {}).get("tests_status", {})
    expected = set(json.loads(record["FAIL_TO_PASS"]))
    return (outcome["status"] == "failed" and bool(expected)
            and set(tests.get("FAIL_TO_PASS", {}).get("failure", [])) == expected
            and not tests.get("PASS_TO_PASS", {}).get("failure", [])
            and set(tests.get("PASS_TO_PASS", {}).get("success", [])) == set(json.loads(record["PASS_TO_PASS"])))


async def verify_repository(environment, directory: Path, work_root: Path, *, previous: dict | None = None) -> dict:
    """执行环境的基线、参考补丁及工作区验证，返回原始评分；previous 非空只复用已验真的证据。

    directory 保存评分日志，work_root 创建唯一工作副本；异常向上层传播，不换题、不调用模型。
    """
    if previous is not None:
        outcomes = dict(previous)
        if not outcomes.get("workspace_verified"):
            raise ValueError("预检未包含实际工作区解包和Git初始化")
    else:
        outcomes = {}
        for variant, patch in (("baseline", ""), ("reference", environment.record["patch"])):
            outcomes[variant] = await environment.grade(patch, directory / variant)
        probe = work_root / ("check-" + uuid4().hex[:12])
        await environment.workspace(probe)
        await environment.start_session(probe)
        try:
            await environment.verify_workspace(probe)
            outcomes.update(workspace_verified=True, workspace_probe=str(probe.resolve()))
            if environment.public_self_test:
                outcomes["public_self_test"] = await environment.verify_public_self_test(probe)
        finally:
            await environment.close_session(probe)
    if environment.public_self_test:
        test = outcomes.get("public_self_test") or {}
        if test.get("status") != "passed" or not test.get("tests_run", 0):
            raise ValueError("固定工作副本的公开自测未通过")
    outcomes["baseline_valid"] = baseline_valid(environment.record, outcomes["baseline"])
    return outcomes


def evidence_files(folder: Path) -> dict:
    """返回 folder 内协议、报告和评分日志的字节摘要；不包含可重建归档，缺失评分证据则拒绝。"""
    paths = [folder / "protocol.json", folder / "report.json"]
    paths += sorted(path for path in (folder / "preflight").rglob("*") if path.is_file())
    if len(paths) == 2:
        raise ValueError("预检缺少评分原件")
    return {path.relative_to(folder).as_posix(): file_hash(path) for path in paths}


def validate_original(folder: Path) -> tuple[dict, dict]:
    """读取并验证 folder 零模型报告与冻结协议，返回两者；伪造通过状态或付费记录均拒绝。"""
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    protocol = json.loads((folder / "protocol.json").read_text(encoding="utf-8"))
    if (report["id"] != folder.name or report["state"] != "preflight_passed"
            or report["mode"] != "preflight" or report["trials"] or report.get("active_trial")
            or report.get("budget", {}).get("calls_admitted", 0)
            or digest(protocol) != report["protocol_sha256"]
            or protocol["repository_environments"] != report["environments"]):
        raise ValueError("预检原件不满足零模型完整性条件")
    records = {row["id"]: row["record"] for row in protocol["frozen"]}
    if set(records) != {row["case_id"] for row in report["preflight"]}:
        raise ValueError("预检题目与原件不一致")
    for row in report["preflight"]:
        if not (row.get("workspace_verified") and row["reference"]["status"] == "passed"
                and baseline_valid(records[row["case_id"]], row["baseline"])):
            raise ValueError("原始缺陷、参考补丁或工作区证据不合格")
    return report, protocol


def make_certificate(folder: Path, *, adoption: dict | None = None) -> dict:
    """为通过的 folder 创建待保存证书；adoption 为一次性审查凭据，None 要求原件已是当前环境实现。

    不修改历史报告，返回包含逐题环境契约和所有原件摘要的对象。旧实现须由审查工具明确确认等价。
    """
    report, protocol = validate_original(folder)
    self_test = any(group.get("context_policy") == "hybrid_budgeted_verified_handoff"
                    for group in protocol["experiment"]["groups"])
    contracts = {row["id"]: digest(environment_contract(row["record"], protocol["config"],
                  public_self_test=self_test, sources=None if adoption else protocol["runtime_hashes"]))
                 for row in protocol["frozen"]}
    return {"version": 1, "report_id": report["id"], "contracts": contracts,
            "files": evidence_files(folder), "environments": report["environments"],
            "adoption": adoption, "external_model_requests": 0}


def verify_certificate(runs_root: Path, certificate: dict, records: list[dict], config: dict,
                       *, public_self_test: bool = False, sources: dict | None = None) -> dict:
    """核对证书、逐题契约及历史原件并返回原报告；变化抛 ValueError，绝不自动重跑。

    sources 默认 None 核对当前源码；恢复审查可传入上一份冻结清单核对旧证书，不能用于放行新执行。
    public_self_test 默认关闭，与证书中的公开自测门槛一致。
    """
    folder = runs_root.resolve() / ".evaluations" / str(UUID(certificate["report_id"]))
    if evidence_files(folder) != certificate["files"]:
        raise ValueError("预检原件摘要变化")
    report, _ = validate_original(folder)
    expected = {row["id"]: digest(environment_contract(row["record"], config,
                    public_self_test=public_self_test, sources=sources)) for row in records}
    if certificate["contracts"] != expected or certificate["environments"] != report["environments"]:
        raise ValueError("该题环境契约变化，需要审查并定向预检")
    return report
