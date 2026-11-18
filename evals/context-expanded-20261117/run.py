"""冻结长上下文题单并执行本机零模型预检、真实续跑和报告汇总。"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
from uuid import uuid4

import httpx

from agent_harness.config import prepare_task
from agent_harness.evaluation.budget import BatchBudget, RESERVATION_PICOS, USD_PICOS, validate_billing_config
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.evaluation.models import Experiment
from agent_harness.model_selection import valid_model_key
from agent_harness.runtime.runner import AgentStopped, run_task
from agent_harness.schemas import RunManifest
from agent_harness.storage.runs import RunStore

from core_cases import CASES as CORE
from inputs import HERE, OUTPUT, ROOT, LAYOUT, freeze_case, matrix, module_path, stage_sources
from template_cases import CASES as TEMPLATES

# 同一进程进出旧运行器，只复用冻结配置、记录和真实压缩入口。
sys.path.insert(1, str(ROOT / "evals/context-checkpoint-20261117"))
from continue_batch import EvalTools, OfflineFinish, SCORER_IMAGE, configuration, digest, history_parent, inventory
from continue_batch import unified_patch
from continuation_report import _scoreable, _usage_complete
import continuation_report
from freeze import POLICY
from replay import _input_content, replay_case, replay_tools
from run_replays import summarize as summarize_replay

PROTOCOL_FILES = {"inputs.py", "check_case.py", "core_cases.py", "template_cases.py", "run.py"}
# 用于确认预检和续跑用相同的本专项实现。
SHARDS = ("first", "second")  # 每分片有独立主机请求账本，每题两臂在同一分片。
ARMS = ("A", "B")  # A 关闭压缩，B 启用混合压缩。


def read_json(path: Path) -> dict:
    """以 UTF-8 从 path 获取已持久化的证据字典，格式错误直接阻断。"""
    return json.loads(path.read_bytes())


def grade(case: dict, workspace: Path, folder: Path, batch: Path) -> dict:
    """在固定的无网络评分镜像验证候选；保存真实进程状态，返回 passed/failed/error。"""
    command = [
        "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "512m", "--cpus", "1",
        "--pids-limit", "64", "--tmpfs", "/tmp:rw,nosuid,noexec,size=32m", "--workdir", "/tmp",
        "-v", f"{workspace.resolve()}:/candidate:ro", "-v", f"{(batch / 'grader').resolve()}:/grader:ro",
        "-v", f"{(batch / 'scorer-deps').resolve()}:/deps:ro", "--entrypoint", "python", SCORER_IMAGE,
        "-I", "-B", "/grader/check_case.py", case["id"], "/candidate", "/deps",
    ]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(command, capture_output=True, timeout=45)
        data = result.stdout.decode("utf-8", errors="replace")
        try:
            final = json.loads(data.splitlines()[-1])
            status = {0: "passed", 1: "failed"}.get(result.returncode, "error")
            if final.get("passed") is not (status == "passed"):
                status = "error"
        except (ValueError, IndexError, AttributeError):
            status = "error"
        answer = {"status": status, "exit_code": result.returncode, "stdout": data,
                  "stderr": result.stderr.decode("utf-8", errors="replace")}
    except (OSError, subprocess.TimeoutExpired) as error:
        answer = {"status": "error", "reason": type(error).__name__}
    atomic_json(folder / "score.json", answer)
    return answer


def patch_reference(case: dict, workspace: Path) -> None:
    """在独立的预检工作区 workspace 写入唯一文本替换，保留原始仓库。"""
    path = workspace / module_path(case)
    text = path.read_text(encoding="utf-8")
    for old, new in [(case["edit_old"], case["edit_new"]), *case.get("patches", ())]:
        if text.count(old) != 1:
            raise ValueError(f"参考变更锚点不唯一：{case['id']}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8", newline="\n")


def check_case(case: dict, batch: Path) -> dict:
    """对同一题核验源码基线失败和参考补丁通过，返回各自真实容器回执。"""
    base = batch / "bases" / case["project"]
    directory = batch / "preflight" / case["id"]
    reference = directory / "reference" / "workspace"
    shutil.copytree(base, reference)
    patch_reference(case, reference)
    return {"id": case["id"], "baseline": grade(case, base, directory / "baseline", batch),
            "reference": grade(case, reference, directory / "reference", batch)}


def fingerprints() -> dict[str, str]:
    """记录当前评测入口与共享既有配置、源码和回放实现的字节指纹。"""
    files = [HERE / name for name in sorted(PROTOCOL_FILES)]
    previous = ROOT / "evals/context-checkpoint-20261117"
    files.extend(previous / name for name in ("continue_batch.py", "continuation_report.py", "freeze.py", "replay.py", "run_replays.py"))
    files.extend(ROOT / "src/agent_harness" / name for name in (
        "context/builder.py", "context/compression.py", "runtime/runner.py",
        "runtime/model_gateway.py", "evaluation/budget.py", "tools/files.py",
    ))
    return {str(path.relative_to(ROOT)).replace("\\", "/"): digest(path.read_bytes()) for path in files}


def ordered_cases(groups: dict[str, list[dict]]) -> list[tuple[dict, int, int]]:
    """交错输出 groups 的仓库和位置，每个仓库两组交替，返回 case/档位索引/分片索引。"""
    return [(cases[index], index, (index + offset) % 2) for index in range(6)
            for offset, cases in enumerate(groups.values())]


async def preflight() -> None:
    """冻结题面和协议，核验 36 道题基线及参考，再回放关键回执并发布准入证据。"""
    batch = OUTPUT / "batches" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    batch.mkdir(parents=True)
    print("本机预检目录：", batch, flush=True)
    groups = matrix([*CORE, *TEMPLATES])
    order = ordered_cases(groups)
    sources = stage_sources(batch)
    private = batch / "grader"
    private.mkdir()
    atomic_json(private / "cases-private.json", {case["id"]: case for case, _, _ in order})
    shutil.copyfile(HERE / "check_case.py", private / "check_case.py")
    protocol = {
        "scope": "36 独立新拟维护需求、6 个固定开源发行或源码版本", "policy": POLICY.model_dump(mode="json"),
        "source_protocol_sha256": digest((batch / "sources.json").read_bytes()), "grader": SCORER_IMAGE,
        "grader_files": inventory(private), "implementation": fingerprints(),
        "cases": [{"id": case["id"], "repository": case["project"], "title": case["title"],
                   "module": case["module"], "symbol": case["symbol"], "prompt_sha256": digest(case["prompt"].encode()),
                   "band": LAYOUT[index][0], "target_kib": LAYOUT[index][1], "page_size": LAYOUT[index][2],
                   "shard": SHARDS[shard], "arms": "AB" if (index // 2 + shard) % 2 == 0 else "BA"}
                  for case, index, shard in order],
    }
    assert len(sources) == 6
    atomic_json(batch / "protocol.json", protocol)
    report = {"state": "preflight_running", "batch": str(batch), "expected": 36, "grades": [],
              "replays": [], "model_http_attempts": 0, "protocol_sha256": digest((batch / "protocol.json").read_bytes())}
    atomic_json(batch / "preflight.json", report)
    limit = asyncio.Semaphore(4)

    async def check_one(case: dict) -> dict:
        """在最多四个同时工作的评分流中测试一题完整的预检分支。"""
        async with limit:
            row = await asyncio.to_thread(check_case, case, batch)
            report["grades"].append(row)
            atomic_json(batch / "preflight.json", report)
            print(f"预检 {case['id']} {row['baseline']['status']}/{row['reference']['status']}", flush=True)
            return row

    valid_grades = await asyncio.gather(*(check_one(case) for case, _, _ in order), return_exceptions=True)
    issues = [str(row) if isinstance(row, BaseException) else row["id"]
              for row in valid_grades
              if isinstance(row, BaseException)
              or row["baseline"]["status"] != "failed" or row["reference"]["status"] != "passed"]
    if issues:
        report.update(state="preflight_failed", issues=issues)
        atomic_json(batch / "preflight.json", report)
        raise SystemExit(f"离线基线或参考评分未通过：{issues}")

    paths = batch / "checkpoints"
    paths.mkdir()
    log_by_id = {row["id"]: row["baseline"] for row in valid_grades}
    tools = replay_tools(POLICY)
    checkpoints = {}
    for case, index, _ in order:
        baseline = batch / "bases" / case["project"]
        checkpoint = freeze_case(case, baseline, index, tools, log_by_id[case["id"]])
        checkpoints[case["id"]] = checkpoint["checkpoint_sha256"]
        atomic_json(paths / (case["id"] + ".json"), checkpoint)
        replay = await replay_case(checkpoint, batch / "replay-runs", POLICY)
        replay.update(title=case["title"], band=checkpoint["band"], source=case["project"])
        report["replays"].append(replay)
        atomic_json(batch / "preflight.json", report)
        print(f"回放 {case['id']} {replay['status']} {replay['raw_input_bytes']} -> {replay['projected_input_bytes']}",
              flush=True)
    summary = summarize_replay(report["replays"])
    report["replay_summary"] = summary
    archive_valid = summary["archive_checks"] == summary["archive_matches"]
    if not (summary["measured_count"] == summary["evidence_resolved"] == 36 and not summary["http_attempts"] and archive_valid):
        report.update(state="preflight_failed", issues=["回放有效配对、归档证据或零请求检查"])
        atomic_json(batch / "preflight.json", report)
        raise SystemExit("冻结检查点未满足预检约束")
    smoke = []
    for identifier in [protocol["cases"][position]["id"] for position in (0, 12, 24)]:
        case = read_json(paths / (identifier + ".json"))
        for arm in ARMS:
            row = await live_trial(case, arm, batch / "runner-smoke", batch, "offline", None,
                                   lambda _run, _slot: None, offline=True)
            smoke.append(row)
    report["runner_smoke"] = smoke
    if any(row["http_attempts"] != 0 or row["patch_passed"] is not False for row in smoke):
        report.update(state="preflight_failed", issues=["实际 Runner 零请求检查"])
        atomic_json(batch / "preflight.json", report)
        raise SystemExit("Runner 预检发现请求或缺少独立基线评分")
    report.update(state="preflight_passed", checkpoint_sha256=checkpoints)
    atomic_json(batch / "preflight.json", report)
    atomic_json(OUTPUT / "preflight-latest.json", {"batch": str(batch), "protocol_sha256": report["protocol_sha256"],
                                                  "state": report["state"]})
    print("零模型预检完成 36/36，固定回放 36/36。", flush=True)


def approved_batch(batch: Path) -> tuple[dict, dict[str, dict]]:
    """核对 batch、评分材料与源码仍和已过预检的冻结指纹一致。"""
    report = read_json(batch / "preflight.json")
    protocol = read_json(batch / "protocol.json")
    if (report["state"] != "preflight_passed"
            or digest((batch / "protocol.json").read_bytes()) != report["protocol_sha256"]
            or protocol["implementation"] != fingerprints()
            or protocol["grader_files"] != inventory(batch / "grader")):
        raise ValueError("已冻结的预检、评分器或执行源码变化")
    source = read_json(batch / "sources.json")
    if (digest((batch / "sources.json").read_bytes()) != protocol["source_protocol_sha256"]
            or source["scorer_dependencies"] != inventory(batch / "scorer-deps")
            or any(row["files"] != inventory(batch / "bases" / name) for name, row in source["repositories"].items())):
        raise ValueError("已冻结的源码或评分依赖变化")
    checkpoints = {}
    for item in protocol["cases"]:
        case = read_json(batch / "checkpoints" / (item["id"] + ".json"))
        hashed = digest(_input_content(case["messages"], case["tools"]))
        if hashed != report["checkpoint_sha256"][case["id"]] or hashed != case["checkpoint_sha256"]:
            raise ValueError(f"冻结输入发生变化：{item['id']}")
        checkpoints[case["id"]] = case
    return protocol, checkpoints


class Ledger(BatchBudget):
    """单分片只允许一个在途请求；保留缺失用量的历史预留以便从同一题单恢复。"""

    def authorize(self, run_id: str, turn: int, purpose: str) -> None:
        """在请求 run_id 的第 turn 轮发起前同步记录预留和 purpose。"""
        reason = self.blocking_reason()
        if reason:
            self.state["stop_reason"] = reason
            self.save()
            raise AgentStopped("limit_exceeded", reason)
        if self.state["requests"] and self.state["requests"][-1]["status"] == "reserved":
            raise ValueError("仍有在途请求，不能预留下一笔")
        self.state["calls_admitted"] += 1
        self.state["reserved_usd_picos"] += RESERVATION_PICOS
        self.state["requests"].append({"run_id": run_id, "turn": turn, "purpose": purpose,
                                      "status": "reserved", "reserved_usd_picos": RESERVATION_PICOS})
        self.save()


def isolated_guard(report: dict, save, recovering: bool) -> Ledger:
    """恢复 report 的历史账本；只有明确要求 recovering 且技术中断才释放运行阻塞。"""
    stored = report.get("budget")
    max_turns = 18 * 2 * (20 + POLICY.max_summary_calls)
    # 运行步数才是本批新增推理范围，价格上限覆盖所有单次合法预留。
    caps = Experiment(max_cost_cents=max_turns * 35, max_model_calls=max_turns,
                      max_known_tokens=max_turns * 1_000_000)
    guard = Ledger(caps, report, save)
    if stored is not None:
        guard.state = report["budget"] = stored
    if recovering:
        reason = report.get("stop_reason") or stored.get("stop_reason") if stored else None
        if reason not in {"unknown_usage", "model_rate_limited", "evaluation_infrastructure", None}:
            raise ValueError(f"该终止原因需先核对协议：{reason}")
        if guard.state["requests"] and guard.state["requests"][-1]["status"] == "reserved":
            guard.state["requests"][-1].update(status="unknown", error_code="recovered_interrupted_request")
        guard.state["stop_reason"] = None
    return guard


async def live_trial(case: dict, arm: str, shard: Path, batch: Path, key: str, guard: Ledger | None, register,
                     *, offline: bool = False) -> dict:
    """从冻结检查点启动一臂；offline 用固定回应，始终返回真实 Runner 和独立评分。"""
    base = batch / "bases" / case["category"]
    part = shard / "slots" / case["id"] / arm
    slot = part / f"attempt-{len(list(part.glob('attempt-*'))) + 1}"
    workspace = slot / "workspace"
    shutil.copytree(base, workspace)
    runs = shard / "runs"
    config = configuration(case, POLICY, arm)
    validate_billing_config(config)
    parent, source = history_parent(case, config, workspace, runs)
    task = prepare_task(case["messages"][-1]["content"], workspace, python_executable=sys.executable)
    manifest = RunManifest(run_id=uuid4(), session_id=parent.session_id, parent_run_id=parent.run_id,
                           turn_index=2, task=task, config=config, history_source=source)
    register(str(manifest.run_id), str(slot))
    transport = None if offline else httpx.AsyncHTTPTransport(
        verify=ssl.create_default_context(), trust_env=False, retries=0)
    result = (await run_task(manifest, runs, key, executor=EvalTools(workspace, runs, config),
                             model_request_guard=guard, transport=transport,
                             model_client=OfflineFinish() if offline else None)).result
    events = RunStore.read(runs, str(manifest.run_id)).events
    starts = [event.payload for event in events if event.type == "run_started"]
    if len(starts) != 1 or starts[0]["messages"][starts[0]["conversation_start"]:] != case["messages"][1:]:
        raise ValueError("真实 Runner 未继承冻结历史")
    checks = [e.payload for e in events if e.type == "context_checked" and e.payload.get("purpose") == "execution"]
    changed = await asyncio.to_thread(unified_patch, base, workspace, slot / "candidate.patch")
    resolved = await asyncio.to_thread(grade, case, workspace, slot, batch)
    passed = resolved["status"] == "passed" if resolved["status"] in {"passed", "failed"} else None
    delivered = (passed and bool(changed) and result.execution_status == "completed"
                 and result.cleanup_status in {"complete", "not_needed"} and bool(result.final_answer)) if passed is not None else None
    return {"case_id": case["id"], "title": case["title"], "category": case["category"], "band": case["band"],
            "arm": arm, "run_id": str(manifest.run_id), "run_directory": str(runs / str(manifest.run_id)),
            "checkpoint_sha256": case["checkpoint_sha256"], "execution_status": result.execution_status,
            "stop_reason": result.stop_reason, "cleanup_status": result.cleanup_status,
            "grade_status": resolved["status"], "patch_passed": passed, "delivery": delivered, "changed_files": changed,
            "initial_check": checks[0] if checks else None,
            "max_execution_input_bytes": max((item["input_bytes"] for item in checks), default=None),
            "model_turns": result.model_turns, "http_attempts": result.http_attempts, "tool_calls": result.tool_calls,
            "usage": result.usage.model_dump(), "usage_complete": result.usage_complete,
            "cost": guard.run_cost(str(manifest.run_id)) if guard else None}


async def paid(batch: Path, name: str, key: str, resume: bool) -> None:
    """串行执行 name 分片中的同题两臂；将有效试次和技术中断原子保存。"""
    if not valid_model_key(key):
        raise ValueError("缺少官方模型凭据")
    protocol, checkpoints = approved_batch(batch)
    schedule = [(item["id"], arm) for item in protocol["cases"] if item["shard"] == name for arm in item["arms"]]
    shard = batch / "paid" / name
    shard.mkdir(parents=True, exist_ok=True)
    report_path = shard / "batch.json"
    report = read_json(report_path) if report_path.is_file() else {
        "state": "running", "trials": [], "diagnostics": [], "active": None, "stop_reason": None,
        "schedule": [{"case_id": identifier, "arm": arm} for identifier, arm in schedule],
        "protocol_sha256": digest((batch / "protocol.json").read_bytes()),
    }
    if report["state"] == "complete":
        print(f"已完成分片 {name}", flush=True)
        return
    if report_path.is_file() and not resume:
        raise ValueError("分片已有记录，核对进程及检查点后使用 --resume")
    if report["protocol_sha256"] != digest((batch / "protocol.json").read_bytes()):
        raise ValueError("付费分片与预检协议不一致")

    def save() -> None:
        """把同题状态、已完成回执和每笔请求账本保存至分片 JSON。"""
        atomic_json(report_path, report)

    guard = isolated_guard(report, save, resume)
    if report["active"] is not None:
        report["diagnostics"].append({"interrupted": report["active"]})
    report.update(state="running", stop_reason=None, active=None)
    save()
    finished = {(row["case_id"], row["arm"]) for row in report["trials"]}
    for identifier, arm in schedule:
        if (identifier, arm) in finished:
            continue
        if reason := guard.blocking_reason():
            report.update(state="stopped", stop_reason=reason)
            save()
            return

        def register(run_id: str, slot: str) -> None:
            """请求前记下当前运行编号和候选位置，便于失败时查证不重复成功组。"""
            report["active"] = {"case_id": identifier, "arm": arm, "run_id": run_id, "slot": slot}
            save()

        try:
            row = await live_trial(checkpoints[identifier], arm, shard, batch, key, guard, register)
        except Exception as error:
            report.update(state="stopped", stop_reason=guard.stop_reason or "evaluation_infrastructure",
                          technical_error=type(error).__name__)
            save()
            raise
        okay = (_scoreable(row) and _usage_complete(row)
                and row["cleanup_status"] in {"complete", "not_needed"}
                and guard.stop_reason is None)
        print(f"{name} {identifier} {arm} {row['stop_reason']} {row['grade_status']} "
              f"tokens={row['usage']['total_tokens']}", flush=True)
        report["active"] = None
        if not okay:
            report["diagnostics"].append(row)
            report.update(state="stopped", stop_reason=guard.stop_reason or "evaluation_infrastructure")
            save()
            return
        report["trials"].append(row)
        save()
    report["state"] = "complete"
    save()
    print(f"分片 {name} 完成：{len(report['trials'])}/{len(schedule)}。", flush=True)


def collect(batch: Path) -> dict:
    """合并两分片中的独立有效记录与预检证据，保存总体、压力档和仓库汇总。"""
    pre = read_json(batch / "preflight.json")
    shards = {name: read_json(batch / "paid" / name / "batch.json") for name in SHARDS}
    rows = [row for shard in shards.values() for row in shard["trials"]]
    continuation_report.PLANNED_CASES = 36
    total = continuation_report.summarize(rows)
    by_band = {band: {arm: continuation_report._arm_metrics(
        [r for r in rows if r["band"] == band and r["arm"] == arm]) for arm in ARMS}
        for band in ("low", "near", "over")}
    by_repo = {repo: {arm: continuation_report._arm_metrics(
        [r for r in rows if r["category"] == repo and r["arm"] == arm]) for arm in ARMS}
        for repo in sorted({r["category"] for r in rows})}
    used = {name: {"calls": val["budget"]["calls_admitted"], "known_tokens": val["budget"]["known_tokens"],
                   "known_usd_picos": val["budget"]["known_usd_picos"],
                   "reserved_picos": val["budget"]["reserved_usd_picos"]} for name, val in shards.items()}
    report = {"state": "complete" if len(rows) == 72 and total["comparison_complete"] else "incomplete",
              "stages": {name: val["state"] for name, val in shards.items()},
              "preflight": pre["state"], "zero_model_http": pre["model_http_attempts"],
              "replay": pre["replay_summary"], "live": total, "by_band": by_band, "by_repository": by_repo,
              "used": used, "diagnostics": {name: val["diagnostics"] for name, val in shards.items()},
              "trials": rows}
    atomic_json(batch / "results.json", report)
    replay = report["replay"]
    lines = [
        "# 长上下文配对扩样", "",
        "题目来自 6 个固定开源源码范围的新拟维护任务，每仓低压、临界、超限各两题。", "",
        f"- 预检有效题 {len(pre['grades'])}/36，固定检查点回放 {replay['measured_count']}/36，"
        f"零模型阶段外部请求 {report['zero_model_http']}。",
        f"- 回放平均输入 {replay['before_bytes_mean'] / 1024:.2f} → {replay['after_bytes_mean'] / 1024:.2f} KiB，"
        f"总字节降幅 {replay['total_reduction_pct']:.2f}%，"
        f"关键证据可核验 {replay['evidence_resolved']}/36。",
        f"- 真实续跑有效 {len(rows)}/72 次、{total['valid_pairs']}/36 个完整同题配对。", "",
        "| 组 | 有效试次 | 输入超限 | 补丁通过 | 完整交付 | 总 token |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for arm, counts in total["paired"].items():
        lines.append(f"| {arm} | {counts['scoreable']} | {counts['input_limit_stops']} | {counts['patch_passes']} | "
                     f"{counts['deliveries']} | {counts['usage']['total_tokens']} |")
    lines += ["", "| 题目 | 仓库 | 压力档 | 组 | 宿主终止原因 | 补丁通过 | 完整交付 | token | Run |",
              "|---|---|---|---|---|---:|---:|---:|---|"]
    for row in rows:
        lines.append(f"| {row['title']} | {row['category']} | {row['band']} | {row['arm']} | {row['stop_reason']} | "
                     f"{row['patch_passed']} | {row['delivery']} | {row['usage']['total_tokens']} | {row['run_id']} |")
    (batch / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


async def main() -> None:
    """解析本机预检、正式计划或汇总入口，密钥只允许从匿名管道接收。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--key-from-stdin", action="store_true")
    parser.add_argument("--shard", choices=SHARDS)
    parser.add_argument("--batch", type=Path)
    args = parser.parse_args()
    if args.real or args.collect:
        batch = args.batch.resolve() if args.batch else Path(read_json(OUTPUT / "preflight-latest.json")["batch"]).resolve()
        if not batch.is_relative_to(OUTPUT.resolve() / "batches"):
            raise ValueError("批次必须位于本专项结果根目录")
        if args.collect:
            result = collect(batch)
            print(f"{result['state']} {len(result['trials'])}/72，结果目录 {batch}", flush=True)
            return
        if not args.key_from_stdin or args.shard is None:
            raise ValueError("--real 需要受保护的 stdin 和已登记分片")
        shard = batch / "paid" / args.shard
        shard.mkdir(parents=True, exist_ok=True)
        import msvcrt

        # 锁由系统进程持有，掉电或异常退出后释放；只恢复同一批已经预检的槽。
        with (shard / "active.lock").open("a+b") as lease:
            lease.seek(0)
            msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                await paid(batch, args.shard, sys.stdin.readline(2048).strip(), args.resume)
            finally:
                lease.seek(0)
                msvcrt.locking(lease.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        await preflight()


if __name__ == "__main__":
    asyncio.run(main())

