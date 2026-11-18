"""唯一的串行恢复入口：只读计划、持锁应用、证据复用及可持久暂停。"""

import argparse
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import os
import ssl
import shutil
from pathlib import Path
from uuid import uuid4

import httpx
from filelock import FileLock

from agent_harness.evaluation.campaign import Campaign
from agent_harness.evaluation.campaign_budget import CampaignBudget, atomic_json
from agent_harness.evaluation.engine import read_report, save_report
from agent_harness.evaluation.preflight import make_certificate, verify_certificate
from agent_harness.evaluation.protocols import digest, file_hash, environment_contract, ENVIRONMENT_SOURCES
from agent_harness.evaluation.report import summarize


def read(path: Path) -> dict:
    """读取 path 的 UTF-8 JSON 对象；失败向上抛出，不修改原件。"""
    return json.loads(path.read_text(encoding="utf-8"))


def changes(before: dict, after: dict) -> dict:
    """返回两个源码清单间的精确变更映射，包含新增和删除文件；None 表示该侧不存在。"""
    return {key: {"before": before.get(key), "after": after.get(key)}
            for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)}


def check_ledger(root: Path, value: dict) -> None:
    """在临时内存实例中复核 value 全部金额与未知预留，不写权威账本；错误时拒绝恢复。"""
    # 构造器的持久化通过专用只读子类关闭；继续使用同一套金额和例外验证规则。
    class ReadOnlyBudget(CampaignBudget):
        """恢复计划专用审计器，只载入权威账本，不保存派生停止状态。"""

        def save(self) -> None:
            """禁止审计过程写文件；返回 None。"""

    budget = ReadOnlyBudget(root / "ledger.json", value["protocol_sha256"])
    if budget.has_pending_request():
        raise ValueError("存在新未知用量，必须另行确认，恢复计划不释放预留")
    if budget.stop_reason not in {None, "campaign_paused", "budget_checkpoint_error"}:
        raise ValueError("账本停止原因需要单独处理：" + budget.stop_reason)


def reconcile_report(report: dict, ledger: dict, work_root: Path) -> dict:
    """生成 report 的恢复视图；保留所有有效终态，把不完整尝试转入诊断并更新未完成槽位。

    ledger 用于发现发单后检查点中断；work_root 用于发现创建了工作区但未发单的尝试。
    不写文件，不重试有效失败；新 run_id 仅赋给已经用过但没有有效终态的槽位。
    """
    result = deepcopy(report)
    valid, diagnostic = [], list(result.get("diagnostic_trials", []))
    for row in result["trials"]:
        (valid if row.get("comparison_valid") else diagnostic).append(row)
    if result.get("active_trial"):
        active = result["active_trial"]
        if active["run_id"] not in {row["run_id"] for row in valid + diagnostic}:
            diagnostic.append({**active, "interrupted": True, "comparison_valid": False})
    ids = [row["run_id"] for row in valid]
    if len(ids) != len(set(ids)):
        raise ValueError("已完成结果身份重复")
    used = {row["run_id"] for row in ledger["requests"]} | {row["run_id"] for row in diagnostic}
    for slot in result["schedule"]:
        if slot["run_id"] in ids:
            continue
        if slot["run_id"] in used or (work_root / slot["run_id"]).exists():
            slot["replaces_run_id"] = slot["run_id"]
            slot["run_id"] = str(uuid4())
    result.update(trials=valid, diagnostic_trials=diagnostic, active_trial=None, stop_reason=None)
    result["state"] = "completed" if len(valid) == len(result["schedule"]) else "incomplete"
    return result


def plan_recovery(campaign: Campaign, review: dict, *, historical: Path | None = None) -> dict:
    """只读生成恢复计划；review 精确绑定源码变更、验收及等价性结论，historical 指一次性旧证据目录。

    配置、题单、预算或未审查代码变化均拒绝。返回待应用的完整事务，不修改结果或账本。
    historical=None 使用已登记的新证书；不保留隐式旧格式兼容或自动全量预检分支。
    """
    campaign.validate()
    root = campaign.root
    before = read(root / "protocol.json")
    state, ledger = read(root / "campaign.json"), read(root / "ledger.json")
    current = json.loads(json.dumps(campaign.build_protocol()))
    delta = changes(before["runtime"], current["runtime"])
    if delta != review.get("changes") or digest(current) != review.get("protocol_sha256"):
        raise ValueError("源码变化未逐项审查，不自动重建预检")
    if any(before.get(key) != current.get(key) for key in current if key not in {"runtime", "identities"}):
        raise ValueError("模型、题单、资源或预算变化，超出恢复授权")
    if (not review.get("environment_equivalent") or not review.get("execution_equivalent")
            or not review.get("approval") or not review.get("offline_proof_sha256")):
        raise ValueError("恢复审查缺少环境、行为等价性或验收依据")
    if state["protocol_sha256"] != digest(before) or ledger["protocol_sha256"] != digest(before):
        raise ValueError("权威状态的协议绑定不一致")
    check_ledger(root, ledger)
    expected = {name: file_hash(root / name) for name in ("protocol.json", "campaign.json", "ledger.json")}
    writes, copies = {}, {}
    receipts = {}
    environment_delta = changes({key: before["runtime"][key] for key in ENVIRONMENT_SOURCES},
                                {key: current["runtime"][key] for key in ENVIRONMENT_SOURCES})
    rebind = review.get("environment_rebind")
    if rebind and (rebind.get("changes") != environment_delta or not rebind.get("approval_sha256")):
        raise ValueError("环境证书更新未绑定逐项变化和批准凭据")
    if historical is not None:
        if state.get("recovery_history"):
            raise ValueError("一次性历史采纳已经结束；以后只验证已登记证书")
        original = read(historical / "protocol.json")
        source = read(historical / "campaign.json")
        if (source["protocol_sha256"] != digest(original)
                or any(original[key] != before[key] for key in before if key not in {"runtime", "identities"})
                or changes(original["runtime"], before["runtime"]) != review.get("historical_changes")):
            raise ValueError("历史证据与已审查的前序协议不符")
    else:
        source = state
    for identifier in state["formal_ids"]:
        key = "formal:" + identifier
        receipt = source["preflight"].get(key)
        if not receipt or not receipt["passed"]:
            raise ValueError("该题缺少完整证据，需要定向预检：" + identifier)
        folder = campaign.runs_root / ".evaluations" / receipt["report_id"]
        cert_path = folder / "preflight-certificate.json"
        if historical is not None:
            raw = read(folder / "protocol.json")
            if raw["runtime_hashes"] != original["runtime"]:
                raise ValueError("历史单题运行代码与审查来源不一致")
            certificate = make_certificate(folder, adoption={"review_sha256": digest(review),
                "original_protocol_sha256": digest(original), "kind": "one_time_audited_adoption"})
        else:
            certificate = read(cert_path)
            if digest(certificate) != receipt["certificate_sha256"]:
                raise ValueError("已登记证书摘要变化")
        record = read(campaign.suite_root / "records" / (identifier + ".json"))
        if rebind:
            # 先用原冻结代码核对旧证书与全部原件，再登记审查链；不能直接把旧证明标成新执行。
            verify_certificate(campaign.runs_root, certificate, [{"id": identifier, "record": record}],
                               campaign.config.model_dump(mode="json"), sources=before["runtime"])
            prior_digest = digest(certificate)
            certificate = deepcopy(certificate)
            certificate["contracts"] = {identifier: digest(environment_contract(record, current["config"]))}
            certificate.setdefault("binding_history", []).append({"previous_certificate_sha256": prior_digest,
                "review_sha256": digest(review), "approval_sha256": rebind["approval_sha256"],
                "kind": "reviewed_environment_equivalence"})
        verify_certificate(campaign.runs_root, certificate, [{"id": identifier, "record": record}],
                           campaign.config.model_dump(mode="json"))
        relative = cert_path.relative_to(root).as_posix()
        expected[relative] = file_hash(cert_path) if cert_path.exists() else None
        writes[relative] = certificate
        receipts[key] = {"report_id": receipt["report_id"], "passed": True, "reason": None,
                         "status": "reused", "certificate_sha256": digest(certificate)}
    state["preflight"].update(receipts)
    state.setdefault("recovery_history", []).append({"at": datetime.now(UTC).isoformat(),
        "previous_active": state.get("active"), "review_sha256": digest(review),
        "previous_protocol_sha256": state["protocol_sha256"]})
    state.update(active=None, stop_reason=None, protocol_sha256=digest(current))
    for phase, entry in state["phases"].items():
        relative = "runs/.evaluations/" + entry["report_id"] + "/report.json"
        report = read(root / relative)
        expected[relative] = file_hash(root / relative)
        for correction in review.get("grading_recoveries", []):
            if correction["phase"] == phase:
                from agent_harness.evaluation.grading_recovery import adopt_grading
                adopt_grading(root, report, correction["receipt_id"], digest(current), ledger, expected, copies)
        result = reconcile_report(report, ledger, campaign.work_root)
        entry["completed_cases"] = [case["id"] for case in result["cases"]
            if all(slot["run_id"] in {row["run_id"] for row in result["trials"]}
                   for slot in result["schedule"] if slot["case_id"] == case["id"])]
        result["recovery"] = {"status": "ready", "fresh": 0, "reused": len(receipts), "recheck": 0,
                              "review_sha256": digest(review), "protocol_sha256": digest(current),
                              "evidence": [{"case_id": key.split(":", 1)[1], **value} for key, value in receipts.items()]}
        result["summary"] = summarize(result)
        writes[relative] = result
    # 仅更新审计绑定及已处理的停止原因；逐请求明细、金额、上限及批准预留逐字值保留。
    ledger["protocol_sha256"] = digest(current)
    ledger["stop_reason"] = None
    writes.update({"protocol.json": current, "ledger.json": ledger, "campaign.json": state})
    return {"version": 1, "root": str(root), "expected": expected, "writes": writes, "copies": copies,
            "review": review, "reused": len(receipts), "external_model_requests": 0}


def apply_plan(root: Path, plan: dict) -> str:
    """持 campaign.lock 时应用已审查 plan，返回事务摘要；重复应用幂等，中断后只补写同一事务。

    先封存全部旧值和计划，再写入；任何无关外部变更均拒绝。函数不获取锁，调用者必须持锁。
    """
    if str(root.resolve()) != plan["root"]:
        raise ValueError("恢复计划目标不匹配")
    if not (set(plan["writes"]) | set(plan.get("copies", {}))).issubset(plan["expected"]):
        raise ValueError("恢复写入缺少原状态前置条件")
    identifier = digest(plan)
    directory = root / "recovery" / identifier
    if (directory / "complete.json").exists():
        return identifier
    prepared = (directory / "prepared.json").exists()
    for relative, expected in plan["expected"].items():
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()) or path.resolve() != path.absolute():
            raise ValueError("恢复事务路径越界")
        actual = file_hash(path) if path.exists() else None
        # 应用中断允许已完成的写入，不允许其他未知内容。
        already = prepared and path.exists() and (
            relative in plan["writes"] and read(path) == plan["writes"][relative]
            or relative in plan.get("copies", {}) and actual == plan["copies"][relative]["sha256"])
        if actual != expected and not already:
            raise ValueError("恢复计划过期：" + relative)
    if not prepared:
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "plan.json", plan)
        for relative in {*plan["writes"], *plan.get("copies", {})}:
            path = root / relative
            if path.exists():
                backup = directory / "before" / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(path.read_bytes())
        atomic_json(directory / "prepared.json", {"plan_sha256": identifier})
    # 先落产物再发布报告；中断恢复只补写缺失内容，不重新采集或评分。
    for relative, artifact in plan.get("copies", {}).items():
        path, source = root / relative, root / artifact["source"]
        if not source.resolve().is_relative_to(root.resolve()) or file_hash(source) != artifact["sha256"]:
            raise ValueError("恢复产物来源改变或越界")
        if path.exists() and file_hash(path) == artifact["sha256"]:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_suffix(path.suffix + ".pending")
        shutil.copyfile(source, pending)
        pending.replace(path)
    for relative, value in plan["writes"].items():
        path = root / relative
        if path.exists() and read(path) == value:
            continue
        atomic_json(path, value)
    atomic_json(directory / "complete.json", {"plan_sha256": identifier})
    # 暂停标记先归档再清除；清除本身不发单，必须经过独立 resume 门禁。
    pause = root / "pause.json"
    if pause.exists():
        atomic_json(directory / "pause.json", read(pause))
        pause.unlink()
    return identifier


async def connection_probe(config, api_key: str, *, transport=None) -> dict:
    """以冻结端点和 TLS 策略 GET 模型列表，返回无敏感正文的回执；不发送推理，不自动重试。

    api_key 仅放认证头；transport 为离线测试注入，None 使用与模型相同的直连网络。
    """
    endpoint = config.custom_providers[config.provider].base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=20, transport=transport,
                                    verify=ssl.create_default_context()) as client:
            response = await client.get(endpoint, headers={"Authorization": "Bearer " + api_key})
    except httpx.RequestError as error:
        raise ValueError("认证探测传输失败：" + type(error).__name__) from None
    if response.status_code != 200:
        raise ValueError("认证探测失败，HTTP " + str(response.status_code))
    return {"at": datetime.now(UTC).isoformat(), "method": "GET", "path": "/models",
            "status": response.status_code, "inference_requests": 0}


def main() -> int:
    """命令行 plan/apply/resume/pause；所有变更持单写锁，pause 只写独立标记，不启动执行器。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["plan", "apply", "resume", "pause"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--suite-root", type=Path, default=Path("evals/swe-three-arm"))
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--swe-python", type=Path, default=Path(".runtime/swe-env/Scripts/python.exe"))
    parser.add_argument("--review", type=Path)
    parser.add_argument("--historical", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--offline-proof", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "pause":
        atomic_json(root / "pause.json", {"at": datetime.now(UTC).isoformat(), "reason": "user_requested"})
        return 0
    campaign = Campaign(root, args.suite_root, args.work_root, args.swe_python)
    with FileLock(str(root / "campaign.lock"), timeout=0):
        if args.action == "plan":
            atomic_json(args.plan, plan_recovery(campaign, read(args.review), historical=args.historical))
            return 0
        if args.action == "apply":
            print(apply_plan(root, read(args.plan)), flush=True)
            return 0
        campaign.initialize()
        proof = read(args.offline_proof)
        if (proof.get("runtime_sha256") != digest(campaign.protocol["runtime"])
                or proof.get("external_model_requests") != 0
                or not all(proof.get(key) for key in ("backend_passed", "frontend_passed", "docker_passed", "web_passed"))):
            raise ValueError("当前源码未通过离线验收")
        campaign.check_pause()
        if campaign.state.get("completed"):
            return 0
        try:
            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
            if not api_key:
                raise ValueError("官方模型凭据不可用")
            atomic_json(root / "connection-probe.json", asyncio.run(connection_probe(campaign.config, api_key)))
            asyncio.run(campaign.run(api_key))
        except Exception as error:
            campaign.state["stop_reason"] = str(error)
            campaign.save()
            print(json.dumps({"event": "campaign_stopped", "reason": str(error)}, ensure_ascii=False), flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
