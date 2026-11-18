"""开发题及冻结对照的单写者调度；任何模型请求经同一本持久预算，已发起槽位不可重做。"""

import argparse
import asyncio
from contextlib import ExitStack
from importlib.metadata import distribution, version
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zipfile import ZIP_DEFLATED, ZipFile

from filelock import FileLock

from agent_harness.config import load_config
from agent_harness.evaluation.budget import USD_PICOS
from agent_harness.evaluation.campaign_budget import CampaignBudget, atomic_json, receipt_sha256
from agent_harness.evaluation.engine import evaluate
from agent_harness.evaluation.models import Experiment, preset_groups
from agent_harness.evaluation.protocols import digest, environment_contract
from agent_harness.evaluation.recovery import connection_probe
from agent_harness.evaluation.repository import RepositoryEnvironment
from agent_harness.evaluation.reproduction import digest_bytes, replay

ROOT = Path(__file__).resolve().parents[2]  # 运行根目录。
DATA = ROOT / ".runtime/hv2"  # 本轮独占数据目录，独立于历史付费账本。
RUNS = DATA / "r"  # 官方评分、补丁、轨迹和预检原件。
CONFIG = ROOT / "evals/swe-three-arm/deepseek.toml"  # 双方冻结同一生成参数。
GRADER = ROOT / ".runtime/swe-env/Scripts/python.exe"  # 固定官方评分器。
MANIFEST = Path(__file__).with_name("protocol.json")  # 在开发前已经冻结的题单。
HTTP_CACHE = Path(__file__).with_name("grader-http")  # 获批的评分环境补充，仅用于登记题目。


def read(path):
    """读取 path 的 UTF-8 JSON 并返回，文件缺失时直接报错。"""
    return json.loads(path.read_bytes())


def http_cache_for(case):
    """返回获批用于 case 独立评分的缓存目录；其余题返回 None，沿用原预检证书。"""
    amendment = read(Path(__file__).with_name("grading-addendum.json"))
    if case not in amendment["cases"]:
        return None
    from agent_harness.evaluation.grading_http import cache_receipt
    if (amendment["base_protocol_sha256"] != digest_bytes(MANIFEST.read_bytes())
            or amendment["cache"] != cache_receipt(HTTP_CACHE)):
        raise ValueError("评分缓存偏离正式请求前封存的补充协议")
    return HTTP_CACHE


def suite(case, phase):
    """返回 case 的宿主清单；phase 为开发阶段时从已冻结历史记录物化。"""
    folder = DATA / "cases" / case
    if phase == "calibration":
        record = read(ROOT / "evals/swe-three-arm/records" / (case + ".json"))
        atomic_json(folder / "record.json", record)
        atomic_json(folder / "suite.json", {"name": case, "version": "20261118-dev", "provenance": "SWE-bench Verified",
            "cases": [{"id": case, "category": record["difficulty"], "project": record["repo"], "split": "test",
                       "prompt": record["problem_statement"], "source": "record.json", "backend": "swebench"}]})
    return folder / "suite.json"


def freeze_runtime():
    """首次正式试次前写出源代码、配置和外部基线的字节摘要；续跑时要求文件集合完全一致。"""
    src = ROOT / "src/agent_harness"
    files = sorted(src.rglob("*.py")) + [CONFIG, Path(__file__), MANIFEST, MANIFEST.with_name("grading-addendum.json"),
                                       MANIFEST.with_name("freeze_http.py"), *sorted(HTTP_CACHE.iterdir())]
    hashes = {p.relative_to(ROOT).as_posix(): digest_bytes(p.read_bytes()) for p in files}
    package = distribution("mini-swe-agent")
    native = {str(p): digest_bytes(p.locate().read_bytes()) for p in (package.files or [])
              if p.parts[0] == "minisweagent" and p.suffix in {".py", ".yaml", ".jinja"}}
    receipt = {"files": hashes, "mini": version("mini-swe-agent"), "mini_files": native}
    target = DATA / "runtime-freeze.json"
    if receipt["mini"] != "2.4.6" or (target.exists() and read(target) != receipt):
        raise ValueError("正式冻结版本发生变化")
    if not target.exists():
        atomic_json(target, receipt)
    # 把与指纹对应的源文件一并交付，后续即使工作区继续开发仍可取回本次实现。
    destination = ROOT / "output/repository-comparison-20261118"
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "正式版本.zip"
    if not archive.exists():
        pending = archive.with_suffix(".pending")
        with ZipFile(pending, "w", compression=ZIP_DEFLATED) as package_zip:
            for path in files:
                name = path.relative_to(ROOT).as_posix()
                raw = path.read_bytes()
                if digest_bytes(raw) != hashes[name]:
                    raise ValueError("封存期间源文件发生变化：" + name)
                package_zip.writestr(name, raw)
            package_zip.writestr("runtime-freeze.json", target.read_bytes())
        pending.replace(archive)


async def independent(case, report, cfg):
    """若 case 是已声明的后台开发题，在首次送评的原补丁上独立复验真实编辑页。"""
    if case != "django__django-13512":
        return
    folder = RUNS / ".evaluations" / report["id"]
    for row in report["trials"]:
        patch_file = folder / (row["run_id"] + ".patch")
        if not patch_file.exists():
            continue
        patch = patch_file.read_bytes()
        assert digest_bytes(patch) == row["patch_sha256"]
        env = RepositoryEnvironment(read(DATA / "cases" / case / "record.json"), folder / "environments" / case,
                                    cfg, GRADER)
        env.image = report["environments"][case]["image_id"]
        probe = ROOT / "evals/repository-entry/admin_entry.py"
        fact = await replay(env, probe.read_bytes(), patch.decode(), DATA / "review" / row["run_id"], 120, ("admin", ""))
        atomic_json(folder / "entry-reviews" / (row["run_id"] + ".json"),
                    {**fact, "run_id": row["run_id"], "protocol_sha256": report["protocol_sha256"]})


async def run_slot(case, arm, folder, cfg, exp, key, budget, phase, fingerprint):
    """在独占 folder 运行 case 的 arm 一次；返回首份终态报告 ID，恢复时只读取原回执。"""
    slot = folder / arm
    slot.mkdir(exist_ok=True)
    run_id = str(uuid5(NAMESPACE_URL, f"{fingerprint}/{phase}/{case}/{arm}"))
    schedule = [{"case_id": case, "strategy": arm, "repeat": 0, "run_id": run_id}]
    marker, progress = slot / "started.json", slot / "progress.json"
    if marker.exists():
        if not progress.exists():
            raise ValueError("槽位已登记但未取得报告身份：" + run_id)
        report = read(RUNS / ".evaluations" / read(progress)["id"] / "report.json")
        if report["active_trial"] or report["state"] not in {"stopped", "completed"}:
            raise RuntimeError("中断槽位保留全部证据，需核对原始执行状态：" + run_id)
    else:
        atomic_json(marker, {"schedule": schedule})

        def checkpoint(report):
            """将 report 当前批次身份落盘，供程序重启时只读恢复原始试次。"""
            atomic_json(progress, {"id": report["id"], "state": report["state"],
                                   "active": (report.get("active_trial") or {}).get("run_id")})

        print("live", case, arm, flush=True)
        report = await evaluate(DATA / "cases" / case / "suite.json", cfg,
            exp.model_copy(update={"groups": preset_groups([arm]), "max_model_calls": 60, "max_cost_cents": 100}),
            RUNS, DATA / "w", key, swe_python=GRADER, required_preflight=read(folder / "preflight.json")["id"],
            campaign_budget=budget, campaign_phase=phase, frozen_schedule=schedule, progress=checkpoint,
            grader_http_cache=http_cache_for(case))
    receipt = {"id": report["id"], "state": report["state"], "reason": report["stop_reason"]}
    await independent(case, report, cfg)
    print("result", case, arm, [r["grade"]["status"] for r in report["trials"]], flush=True)
    # 已终结的传输或400错误永久保留$0.35，仅下一道独立槽位可以继续，绝不重发本题请求。
    pending = [r for r in budget.state["requests"] if r["status"] in {"reserved", "unknown"}
               and not any(h["run_id"] == r["run_id"] and h["turn"] == r["turn"] for h in budget.state.get("approved_holds", []))]
    if (len(pending) == 1 and pending[0]["run_id"] == run_id and pending[0]["status"] == "unknown"
            and pending[0].get("error_code") in {"model_bad_request", "model_transport"}
            and budget.stop_reason in {"unknown_usage", "model_bad_request"} and report["trials"]):
        row = pending[0]
        budget.state.setdefault("approved_holds", []).append({"run_id": run_id, "turn": row["turn"],
            "reserved_usd_picos": row["reserved_usd_picos"], "receipt_sha256": receipt_sha256(row),
            "approval_sha256": fingerprint})
        # 先保留原批次的停止原因，再用新账本实例校验预留；当前单写者不再绑定旧报告。
        atomic_json(budget.path, {**budget.state, "stop_reason": None})
    atomic_json(slot / "result.json", receipt)
    return receipt


async def run(mode, limit):
    """按 mode 做免生成预检或正式执行；limit 限制此次处理的题目数，续跑跳过终结槽位。"""
    manifest = read(MANIFEST)
    fingerprint = digest_bytes(MANIFEST.read_bytes())
    cfg = load_config(CONFIG)
    if digest_bytes(CONFIG.read_bytes()) != manifest["config_sha256"]:
        raise ValueError("模型配置与抽样冻结协议不符")
    phase = "calibration" if mode.endswith("dev") else "formal"
    live = mode.startswith("run")
    groups = [(case, ["C"]) for case in manifest["development"]] if phase == "calibration" else [
        (row["instance_id"], row["groups"]) for row in manifest["cases"]]
    if live and not (DATA / "offline" / "passed.json").exists():
        raise ValueError("离线验收尚未完成")
    if live and phase == "formal":
        freeze_runtime()
    budget_options = {"total_limit": 10 * USD_PICOS, "total_calls": 2640,
                      "phases": {"calibration": (10 * USD_PICOS, 240), "formal": (10 * USD_PICOS, 2400)}}
    key = ""
    if live:
        key = os.environ["DEEPSEEK_API_KEY"]
        atomic_json(DATA / "connectivity.json", await connection_probe(cfg, key))
    count = 0
    for case, arms in groups:
        folder = DATA / "slots" / case
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / "result.json"
        checks = folder / "preflight.json"
        if final.exists():
            continue
        if count >= limit:
            break
        suite_path = suite(case, phase)
        original = environment_contract(read(suite_path.with_name("record.json")), cfg.model_dump(mode="json"))
        cache = http_cache_for(case)
        if cache:
            from agent_harness.evaluation.grading_http import cache_receipt
            contract = digest({"original": original, "grading_http": cache_receipt(cache)})
        else:
            contract = digest(original)
        previous = read(checks) if checks.exists() else {}
        ready = previous.get("state") == "preflight_passed" and previous.get("contract") == contract
        if live and previous.get("reason") == "invalid_case" and previous.get("contract") == contract:
            print("unscored", case, "invalid_case", previous["id"], flush=True)
            continue
        if ready and not live:
            continue
        count += 1
        if phase == "formal":
            row = next(row for row in manifest["cases"] if row["instance_id"] == case)
            record = read(suite_path.with_name("record.json"))
            if row["record_sha256"] != digest_bytes(suite_path.with_name("record.json").read_bytes()) or (
                read(suite_path)["cases"][0]["prompt"] != record["problem_statement"]):
                raise ValueError("正式题记录或原始问题已改变")
        exp = Experiment(split="test", groups=preset_groups(arms), max_model_calls=60 * len(arms),
                         max_known_tokens=10**9, max_cost_cents=100 * len(arms), grader_timeout_seconds=600)
        if not ready:
            print("preflight", case, flush=True)
            check = await evaluate(suite_path, cfg, exp, RUNS, DATA / "w", "", swe_python=GRADER, preflight_only=True,
                                   grader_http_cache=cache)
            atomic_json(checks, {"id": check["id"], "state": check["state"], "reason": check.get("stop_reason"),
                                 "error": check.get("preflight_error"),
                                 "contract": contract})
            if check["state"] != "preflight_passed":
                print("preflight failed", case, check.get("preflight_error"), flush=True)
                continue
        if not live:
            continue
        receipts = {}
        for arm in arms:
            if (folder / arm / "result.json").exists():
                receipts[arm] = read(folder / arm / "result.json")
                continue
            budget = CampaignBudget(DATA / "ledger.json", fingerprint, **budget_options)
            budget.bind(phase, {}, lambda: None)  # 仅查询下一槽位的接纳能力，尚未绑定实际报告。
            reason = budget.blocking_reason()
            if reason:
                print("budget stop", reason, flush=True)
                return
            if phase == "formal":
                freeze_runtime()
            receipts[arm] = await run_slot(case, arm, folder, cfg, exp, key, budget, phase, fingerprint)
            if budget.stop_reason and not read(budget.path)["stop_reason"]:
                atomic_json(DATA / "connectivity.json", await connection_probe(cfg, key))
        atomic_json(final, receipts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["check-dev", "run-dev", "check-formal", "run-formal"])
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    # 零模型预检只写各自阶段的证书，开发与正式可独立准备；有费用的执行始终串行。
    with ExitStack() as locks:
        locks.enter_context(FileLock(str(DATA / (args.mode.split("-")[-1] + ".lock")), timeout=0))
        if args.mode.startswith("run"):
            locks.enter_context(FileLock(str(DATA / "campaign.lock"), timeout=0))
        asyncio.run(run(args.mode, args.limit))
