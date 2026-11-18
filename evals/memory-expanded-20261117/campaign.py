"""执行冻结题单的两轮交叉对照，以同题为统计单位保存全部正式证据。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
from random import Random
from statistics import mean
from unittest.mock import patch

from agent_harness.evaluation.budget import validate_billing_config
from agent_harness.evaluation.campaign_budget import atomic_json

from evidence import ConcurrentLedger, TraceTransport, measured_trial, prepare_seed
from run_repository import arm_metrics, config, memory_run, render, schedule_for, scored, summary_for


WORKERS = 2  # 同时最多两道题，每题严格按冻结先后依次提交两臂。
TRANSPORT_ERRORS = {"model_transport", "model_tls_verification", "model_rate_limited"}  # 可独立核对的服务连接异常。


def task_comparison(trials: list[dict], rounds: tuple[int, ...]) -> dict:
    """按同题各轮两臂完整配对聚合 trials，返回工具、交付和题级重采样区间。"""
    by_case = defaultdict(dict)
    for row in trials:
        by_case[row["case_id"]][(row["round"], row["arm"])] = row
    slots = {(round_no, arm) for round_no in rounds for arm in "AB"}
    complete = [replies for replies in by_case.values() if slots <= replies.keys()
                and all(scored(replies[key]) for key in slots)]
    groups = {arm: arm_metrics([replies[round_no, arm] for replies in complete for round_no in rounds]) for arm in "AB"}
    effects = {}
    for name in ("search_calls", "repeated_reads", "tool_calls", "patch_passed", "delivery"):
        deltas = [mean(replies[r, "B"][name] - replies[r, "A"][name] for r in rounds) for replies in complete]
        if not deltas:
            continue
        rng = Random(261117)
        bootstrap = sorted(mean(rng.choices(deltas, k=len(deltas))) for _ in range(2000)) if len(deltas) > 1 else []
        effects[name] = {"paired_mean_delta": mean(deltas), "lower": bootstrap[50] if bootstrap else None,
                         "upper": bootstrap[1949] if bootstrap else None,
                         "positive_tasks": sum(delta > 0 for delta in deltas),
                         "negative_tasks": sum(delta < 0 for delta in deltas),
                         "tied_tasks": deltas.count(0)}
    return {"independent_tasks": len(complete), "A": groups["A"], "B": groups["B"], "effects": effects}


def _preflight(out: Path, protocol: str) -> dict:
    """核对 out 的零模型证书与 protocol 以及完整批次原件，返回已验证收据。"""
    receipt = json.loads((out / "preflight-latest.json").read_bytes())
    batch = Path(receipt["batch"]).resolve(strict=True)
    if not batch.is_relative_to((out / "preflight").resolve()):
        raise ValueError("预检目录偏离本专项")
    record = json.loads((batch / "batch.json").read_bytes())
    if (receipt["state"], receipt["protocol"], receipt["cases"], receipt["external_http"]) != ("passed", protocol, 36, 0):
        raise ValueError("正式发单前需要同协议的零模型预检")
    if (record["state"] != "passed" or record["protocol"] != protocol or record["external_http"]
            or len(record["scores"]) != 36 or len(record["seeds"]) != 36 or record["errors"]):
        raise ValueError("预检原件无法核对")
    return receipt


def _prepare_report(out: Path, cases: list[dict], manifest: dict, previous: Path | None,
                    recover_transport: bool, receipt: dict) -> tuple[Path, dict]:
    """创建新批次或恢复 previous 同协议批次；返回目录和含实际原账本的报告。"""
    from run_expanded import write_manifest

    if previous is None:
        batch = out / "paid" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        batch.mkdir(parents=True)
        write_manifest(batch, cases, manifest)
        return batch, {
            "protocol": manifest["protocol"], "repositories": manifest["repositories"],
            "schedule": manifest["schedule"], "scorer_image": manifest["scorer_image"],
            "state": "preparing", "stop_reason": None, "trials": [], "active": {}, "attempts": {},
            "fixtures": {}, "preflight": receipt["batch"], "recoveries": [],
        }
    batch = previous.resolve(strict=True)
    if not batch.is_relative_to((out / "paid").resolve()):
        raise ValueError("恢复路径不是本专项正式批次")
    report = json.loads((batch / "batch.json").read_bytes())
    if report["protocol"] != manifest["protocol"] or report["schedule"] != manifest["schedule"]:
        raise ValueError("恢复时的题单、顺序或程序不同于预注册协议")
    if report["state"] == "complete":
        return batch, report
    reason = report.get("stop_reason") or report["budget"].get("stop_reason")
    requests = report["budget"]["requests"]
    reserved = [row for row in requests if row["status"] == "reserved"]
    if reason or reserved:
        # 鉴权、模型版本或费率异常不能混入本批。传输恢复保留费用不明的原请求。
        unknown = [row for row in requests if row["status"] == "unknown"]
        allowed = (reason is None or reason in TRANSPORT_ERRORS or
                   (reason == "unknown_usage" and unknown and unknown[-1].get("error_code") in TRANSPORT_ERRORS))
        if not recover_transport or not allowed:
            raise ValueError("请先核对原请求异常与费用保留后再继续可恢复的传输槽位")
        for row in reserved:
            row.update(status="unknown", error_code="interrupted_after_reservation")
        report["recoveries"].append({"stop_reason": reason, "unknown_held": len(unknown) + len(reserved)})
        report["budget"]["stop_reason"] = None
    report.update(state="preparing", stop_reason=None, active={})
    return batch, report


async def run_campaign(cases: list[dict], manifest: dict, key: str, previous: Path | None, recover_transport: bool) -> bool:
    """按冻结 cases/manifest 启动真实官方对照；使用匿名 key 并在退出前持久化每次预留。"""
    from run_expanded import MAX_CALLS, OUTPUT, ROUNDS, TRIALS

    receipt = _preflight(OUTPUT, manifest["protocol"])
    for arm in "AB":
        validate_billing_config(config(arm))
    batch, report = _prepare_report(OUTPUT, cases, manifest, previous, recover_transport, receipt)
    if report["state"] == "complete":
        return True
    print(f"正式批次目录 {batch}", flush=True)

    def save() -> None:
        """将预算、活跃槽和原始试次以同一份原子回执写入本批。"""
        atomic_json(batch / "batch.json", report)

    ledger = ConcurrentLedger(report, save, MAX_CALLS)
    finished = {(row["round"], row["case_id"], row["arm"]) for row in report["trials"]}
    if len(finished) != len(report["trials"]):
        raise ValueError("正式有效试次不允许重复")
    expected_fixtures = json.loads((Path(receipt["batch"]) / "batch.json").read_bytes())["seeds"]
    short_paths = {(row["round"], row["case_id"], row["arm"]): row["slot_root"] for row in manifest["schedule"]}
    report.update(state="running", stop_reason=None)
    save()
    atomic_json(OUTPUT / "paid-latest.json", {"batch": str(batch), "protocol": manifest["protocol"]})

    async def pair(round_no: int, case: dict, arms: tuple[str, str]) -> None:
        """在 round_no 依次执行 case 的固定 arms，保留发单和失效的证据。"""
        for arm in arms:
            identifier = case["id"]
            slot_key = (round_no, identifier, arm)
            if slot_key in finished or report["stop_reason"] or ledger.stop_reason:
                continue
            slug = f"{round_no}/{identifier}/{arm}"
            attempts = report["attempts"].setdefault(slug, [])
            attempt = batch / short_paths[slot_key] / f"{len(attempts) + 1:02d}"
            report["active"][slug] = {"round": round_no, "case_id": identifier, "arm": arm,
                                      "attempt": attempt.relative_to(batch).as_posix()}
            original = report["active"][slug]
            attempts.append(original)
            save()
            try:
                seed = await prepare_seed(case, arm, attempt)
                if seed["fixture_sha256"] != expected_fixtures[identifier]["fixture_sha256"]:
                    raise ValueError("正式答题环境与预检的学习历史或源码指纹不一致")
                report["fixtures"][identifier] = seed["fixture_sha256"]
                if ledger.stop_reason or report["stop_reason"]:
                    original["result"] = "prepared_without_request"
                    return
                row = await measured_trial(case, arm, attempt, key, ledger)
                row.update(round=round_no, attempt=original["attempt"])
                original["outcome"] = row
                invalid = (not scored(row) or row["cleanup_status"] not in {"complete", "not_needed"})
                if invalid:
                    report["stop_reason"] = ledger.stop_reason or "evaluation_infrastructure"
                else:
                    report["trials"].append(row)
                    finished.add(slot_key)
                print(f"轮{round_no} {identifier} {arm} {row['stop_reason']} 补丁={row['patch_passed']} "
                      f"交付={row['delivery']} 有效 {len(finished)}/{TRIALS}", flush=True)
            except Exception as error:
                original["error"] = type(error).__name__
                report["stop_reason"] = ledger.stop_reason or "evaluation_infrastructure"
                print(f"正式槽位异常 {slug} {error!r}", flush=True)
            finally:
                report["active"].pop(slug, None)
                save()

    with patch.object(memory_run, "config", config), patch.object(memory_run, "EvidenceTransport", TraceTransport):
        for round_no in ROUNDS:
            schedule = schedule_for(cases, round_no)
            work = [(schedule[i][0], (schedule[i][1], schedule[i + 1][1])) for i in range(0, len(schedule), 2)]
            for offset in range(0, len(work), WORKERS):
                if report["stop_reason"] or ledger.stop_reason:
                    break
                await asyncio.gather(*(pair(round_no, case, arms) for case, arms in work[offset:offset + WORKERS]))
            if report["stop_reason"] or ledger.stop_reason:
                break

    order = {(row["round"], row["case_id"], row["arm"]): n for n, row in enumerate(manifest["schedule"])}
    report["trials"].sort(key=lambda row: order[row["round"], row["case_id"], row["arm"]])
    report["rounds"] = {str(r): summary_for([row for row in report["trials"] if row["round"] == r]) for r in ROUNDS}
    report["project_effects"] = {
        project: task_comparison([row for row in report["trials"] if row["project"] == project], ROUNDS)
        for project in manifest["repositories"]
    }
    report["overall"] = task_comparison(report["trials"], ROUNDS)
    complete = len(finished) == TRIALS and not ledger.stop_reason and not report["stop_reason"]
    report.update(state="complete" if complete else "stopped",
                  stop_reason=None if complete else report["stop_reason"] or ledger.stop_reason or "missing_trials")
    save()
    markdown = render(report).replace("/48 次", f"/{TRIALS} 次", 1)
    markdown += "\n## 两轮同题汇总\n\n"
    markdown += f"实际完整独立题 {report['overall']['independent_tasks']}/{len(cases)}。\n\n"
    for arm in "AB":
        row = report["overall"][arm]
        markdown += f"- {arm} 共 {row['n']} 个槽，搜索 {row['search_calls']}，重读 {row['repeated_reads']}，"
        markdown += f"平均工具 {row['mean_tools']}，补丁 {row['patch']}/{row['n']}，完整交付 {row['full']}/{row['n']}。\n"
    (batch / "report.md").write_text(markdown, encoding="utf-8")
    print(f"正式批次 {report['state']}，有效 {len(finished)}/{TRIALS}", flush=True)
    return complete
