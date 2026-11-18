"""用两轮独立会话对照评估全新任务中的稳定规则、事实与记忆失效。"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime
import json
from pathlib import Path
import sys

from agent_harness.evaluation.budget import BatchBudget, RESERVATION_PICOS, USD_PICOS, validate_billing_config
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.evaluation.models import Experiment
from agent_harness.memory.types import MemoryContent
from agent_harness.model_selection import valid_model_key
from agent_harness.storage.runs import RunStore


HERE = Path(__file__).resolve().parent  # 追加评测的脚本和独立题目。
OUTPUT = HERE.parent.parent / "output" / HERE.name  # 追加批次的本机证据目录。
REPETITIONS = (1, 2)  # 每轮各有十二个同题 A/B 配对。
MAX_CALLS = 48 * 8  # 按批准的试次数和每试次模型次数计算请求上界。

# 复用首轮已经验收的学习、真实 Runner、用量、评分和回执采集。同目录中的题单独立加载。
sys.path.insert(0, str(HERE.parent / "memory-switch-20261117"))
import run as memory_run
sys.path.pop(0)


class RequestLedger(BatchBudget):
    """沿用逐笔预留和用量核账，批次接纳只依据获批的次数及回执状态。"""

    def __init__(self, report: dict, save: Callable[[], None]) -> None:
        """初始化正式账本。构造所需的内部金额取所有请求的最大预留，公开美元限额为空。"""
        reserve_cents = RESERVATION_PICOS // (USD_PICOS // 100)
        super().__init__(Experiment(max_cost_cents=MAX_CALLS * reserve_cents, max_model_calls=MAX_CALLS,
                                   max_known_tokens=MAX_CALLS * 1_000_000), report, save)
        self.state["limit_usd_picos"] = None

    def blocking_reason(self) -> str | None:
        """返回模型请求数达到上界或回执不可结算等已记录阻塞，正常时返回 None。"""
        if self.stop_reason:
            return self.stop_reason
        if self.state["calls_admitted"] >= self.state["max_calls"]:
            return "batch_model_limit"
        return None


def load_cases() -> list[dict]:
    """加载十二道新题并验证题型、学习路径、新任务和答案产物均具备冻结信息。"""
    from fact_cases import CASES as FACTS
    from rule_cases import CASES as RULES
    from safety_cases import CASES as SAFETY

    sources = {"rule": RULES, "fact": FACTS, "safety": SAFETY}
    if any(len(group) != 4 for group in sources.values()):
        raise ValueError("题单必须覆盖 4 道规则、4 道事实、4 道失效题")
    cases = [dict(group[index], scenario=scenario) for index in range(4)
             for scenario, group in sources.items()]
    if len({case["id"] for case in cases}) != 12:
        raise ValueError("发现重复题号")
    for case in cases:
        category = "stale" if case["scenario"] == "safety" else "reuse"
        if case["kind"] != category or case["target"] not in case["files"] | case.get("updates", {}):
            raise ValueError(f"题目类别或评分目标不合法：{case['id']}")
        if category == "stale" and (not case.get("stale_values") or not case.get("misuse")):
            raise ValueError(f"安全题缺少冻结旧值与行为探针：{case['id']}")
        foreign = {note["episode"] for note in case["notes"] if note.get("foreign")}
        for index, episode in enumerate(case["episodes"]):
            files = case.get("foreign_files", {}) if index in foreign else case["files"]
            if (not episode["read"] or case["target"] in episode["read"]
                    or not set(episode["read"]) <= files.keys()):
                raise ValueError(f"学习回执不对应独立证据：{case['id']}")
        for note in case["notes"]:
            MemoryContent.model_validate(note["content"])
            if not 0 <= note["episode"] < len(case["episodes"]):
                raise ValueError(f"笔记来源越界：{case['id']}")
        if case["scenario"] == "fact":
            for note in case["notes"]:
                evidence = set(note["content"].get("evidence", []))
                if (note["content"]["type"] != "fact" or not evidence
                        or evidence - set(case["episodes"][note["episode"]]["read"])
                        or evidence & case.get("updates", {}).keys()):
                    raise ValueError(f"稳定事实与真实学习回执不一致：{case['id']}")
    return cases


def schedule_for(cases: list[dict], repetition: int) -> list[tuple[dict, str]]:
    """让同题两臂相邻，两轮反转题序和 A/B 先后，以降低顺序偏差。"""
    sequence = cases if repetition == 1 else list(reversed(cases))
    positions = {case["id"]: index for index, case in enumerate(cases)}
    return [(case, arm) for case in sequence
            for arm in ("AB" if (positions[case["id"]] + repetition) % 2 else "BA")]


def scored(row: dict) -> bool:
    """仅当独立评分、完整用量及终态都可判断时纳入同题统计。"""
    return (isinstance(row.get("patch_passed"), bool) and isinstance(row.get("delivery"), bool)
            and row.get("usage_complete") is True and row.get("execution_status") not in {"running", "created"})


def metrics(rows: list[dict]) -> dict:
    """汇总已选择的同条件试次，用显式分母区分判定和缺失。"""
    count = len(rows)
    aggregate = {field: sum(row[field] for row in rows) for field in
                 ("repeated_reads", "repeated_bytes", "search_calls", "tool_calls", "memory_calls", "http_attempts")}
    aggregate["usage"] = {key: sum(row["usage"][key] for row in rows)
                          for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    return {**aggregate, "n": count, "mean_tools": aggregate["tool_calls"] / count if count else None,
            "correct": sum(row["patch_passed"] is True for row in rows),
            "complete": sum(row["delivery"] is True for row in rows),
            "old_retrieved": sum(row["old_retrieved"] for row in rows),
            "old_exposed": sum(row["old_exposed"] for row in rows),
            "misuse": sum(row["misuse"] is True for row in rows),
            "assessable": sum(row["misuse"] is not None for row in rows),
            "safe_delivery": sum(row["delivery"] is True and row["misuse"] is False for row in rows),
            "undelivered": sum(row["delivery"] is False for row in rows)}


def round_summary(rows: list[dict]) -> dict:
    """按实际有效的同题配对汇总指定轮次，保留不完整批次的分母。"""
    by_case = defaultdict(dict)
    for row in rows:
        group = by_case[row["case_id"]]
        if row["arm"] in group:
            raise ValueError("同一轮出现重复试次")
        group[row["arm"]] = row
    comparable = [pair for pair in by_case.values() if set(pair) == {"A", "B"} and all(map(scored, pair.values()))]
    buckets = {
        "all": comparable,
        "reuse": [pair for pair in comparable if pair["A"]["kind"] == "reuse"],
        "both_delivered_reuse": [pair for pair in comparable if pair["A"]["kind"] == "reuse" and all(row["delivery"] for row in pair.values())],
        "rule": [pair for pair in comparable if pair["A"]["scenario"] == "rule"],
        "fact": [pair for pair in comparable if pair["A"]["scenario"] == "fact"],
        "safety": [pair for pair in comparable if pair["A"]["scenario"] == "safety"],
    }
    return {"valid_pairs": len(comparable), "trials": len(rows),
            **{name: {arm: metrics([pair[arm] for pair in pairs]) for arm in ("A", "B")}
               for name, pairs in buckets.items()}}


def draw_report(report: dict) -> str:
    """将两轮真实配对、费用和逐题正确性写成可复查的 Markdown。"""
    rows = report["trials"]
    out = ["# AgentHarness 长期记忆追加评测", "",
           "A 关闭项目记忆，B 开启已确认记忆、主题索引与历史读取。两组均在独立新会话完成全新任务。",
           f"批次状态 `{report['state']}`，收到 {len(rows)}/48 条正式记录。", ""]
    if report["stop_reason"]:
        out += [f"停批原因 `{report['stop_reason']}`。", ""]
    budget = report.get("budget")
    if budget:
        out += [f"真实请求 {budget['calls_admitted']}，已知用量 {budget['known_tokens']:,} token，"
                f"按峰时费率估算 ${budget['known_usd_picos'] / USD_PICOS:.6f}，"
                f"未结算预留 ${budget['reserved_usd_picos'] / USD_PICOS:.2f}。", ""]
    out += ["## 分轮结果", "", "| 轮次 | 样本 | 组别 | 配对题数 | 重读次数 | 重读字节 | 平均工具步数 | 正确 | 完整 |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for repeat, details in report["rounds"].items():
        for bucket, label in (("reuse", "8 道复用题"), ("both_delivered_reuse", "两组均完整的复用题"),
                              ("rule", "稳定约定"), ("fact", "稳定事实"), ("all", "全部题")):
            for arm, m in details[bucket].items():
                if not m["n"]:
                    continue
                out.append(f"| {repeat} | {label} | {arm} | {m['n']} | {m['repeated_reads']} | {m['repeated_bytes']} | "
                           f"{m['mean_tools']:.2f} | {m['correct']}/{m['n']} | {m['complete']}/{m['n']} |")
    out += ["", "## 四道失效与干扰题", "", "| 轮次 | 组别 | 配对题数 | 旧值进入输入 | 误用 / 可判定 | 安全交付 | 未完整交付 |",
            "|---|---|---:|---:|---:|---:|---:|"]
    for repeat, details in report["rounds"].items():
        for arm, m in details["safety"].items():
            if m["n"]:
                out.append(f"| {repeat} | {arm} | {m['n']} | {m['old_exposed']} | {m['misuse']}/{m['assessable']} | "
                           f"{m['safe_delivery']}/{m['n']} | {m['undelivered']} |")
    out += ["", "## 逐题结果", "", "| 轮次 | 题目 | 组别 | 重读次数 | 产物正确 | 完整交付 | 旧值误用 | 终态 | Run |",
            "|---|---|---|---:|---|---|---|---|---|"]
    marks = {True: "是", False: "否", None: "—"}
    for row in rows:
        out.append(f"| {row['repetition']} | {row['title']} | {row['arm']} | {row['repeated_reads']} | "
                   f"{marks[row['patch_passed']]} | {marks[row['delivery']]} | {marks[row['misuse']]} | "
                   f"{row['stop_reason'] or row['execution_status']} | {row['run_id']} |")
    return "\n".join(out) + "\n"


def sent_note_measurement(row: dict, runs: Path) -> int:
    """从正式发单轮次核对真实自动注入，返回曾进入请求的确认条目数量。"""
    history = RunStore.read(runs, row["run_id"])
    actual_turns = {event.payload.get("model_turn") for event in history.events if event.type == "model_http_attempt"}
    identified = set()
    for event in history.events:
        if event.type != "memory_selected":
            continue
        for use in event.payload["memory"]["uses"]:
            if use["model_turn"] in actual_turns:
                identified.update(item["id"] for item in use["selected"])
    return len(identified)


async def main() -> None:
    """先跑无外部模型预检，再以同一题单独立执行两轮正式对照并保留账本。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="启动已批准的四十八个正式槽位。")
    parser.add_argument("--key-from-stdin", action="store_true", help="通过匿名管道接收官方 API 凭据。")
    flags = parser.parse_args()
    key = sys.stdin.readline(2048).strip() if flags.key_from_stdin else "offline"
    if flags.real and (not flags.key_from_stdin or not valid_model_key(key)):
        raise SystemExit("正式评测缺少官方模型凭据")
    cases = load_cases()
    fingerprint = memory_run.fingerprint(cases)
    if flags.real:
        preflight = json.loads((OUTPUT / "preflight-latest.json").read_bytes())
        if preflight["state"] != "passed" or preflight["case_fingerprint"] != fingerprint:
            raise SystemExit("同一冻结题单尚未通过预检")
        for arm in ("A", "B"):
            validate_billing_config(memory_run.config(arm))
    batch = OUTPUT / ("paid" if flags.real else "preflight") / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    batch.mkdir(parents=True)
    schedule = {repeat: schedule_for(cases, repeat) for repeat in REPETITIONS}
    rows: list[dict] = []
    report = {"state": "preparing", "stop_reason": None, "active": None, "trials": rows,
              "case_fingerprint": fingerprint, "scorer_image": memory_run.SCORER_IMAGE,
              "schedule": [{"repetition": repeat, "case_id": case["id"], "arm": arm}
                           for repeat, slots in schedule.items() for case, arm in slots]}

    def save() -> None:
        """在发单前和用量回执后原子保存各轮、费用和当前槽位。"""
        report["rounds"] = {str(repeat): round_summary([row for row in rows if row["repetition"] == repeat])
                            for repeat in REPETITIONS}
        atomic_json(batch / "batch.json", report)

    atomic_json(batch / "frozen-cases.json", {"case_fingerprint": fingerprint, "cases": cases})
    save()
    print(f"Batch: {batch}", flush=True)
    expected = {}
    for repeat in REPETITIONS if flags.real else (1,):
        round_path = batch / f"round-{repeat}"
        seeds = await memory_run.prepare(cases, schedule[repeat], round_path)
        for seed in seeds:
            old = expected.setdefault(seed["case_id"], seed["fixture_sha256"])
            if old != seed["fixture_sha256"]:
                raise ValueError(f"两轮学习材料发生变化：{seed['case_id']}")
        atomic_json(round_path / "seeds.json", {"preparations": seeds})
    if not flags.real:
        scores = [await asyncio.to_thread(memory_run.scoring_preflight, case, batch) for case in cases]
        first, second = schedule[1][:2]
        controls = [await memory_run.trial(case, arm, batch / "round-1", key=key, guard=None, real=False)
                    for case, arm in (first, second)]
        if any(row["http_attempts"] or row["patch_passed"] is not False for row in controls):
            raise ValueError("零模型答题入口未通过")
        report.update(state="passed", scores=scores, controls=controls, seeds=24)
        save()
        atomic_json(OUTPUT / "preflight-latest.json", {"state": "passed", "case_fingerprint": fingerprint,
                                                      "batch": str(batch), "external_http": 0})
        print("Preflight passed 12/12; external requests 0.", flush=True)
        return

    report.update(state="running", preflight=preflight["batch"])
    guard = RequestLedger(report, save)
    save()
    for repeat, slots in schedule.items():
        round_path = batch / f"round-{repeat}"
        for case, arm in slots:
            if reason := guard.blocking_reason():
                report.update(state="stopped", stop_reason=reason)
                break
            report["active"] = {"repetition": repeat, "case_id": case["id"], "arm": arm}
            save()
            try:
                row = await memory_run.trial(case, arm, round_path, key=key, guard=guard, real=True)
                row.update(repetition=repeat, scenario=case["scenario"],
                           injected_notes=sent_note_measurement(row, round_path / "runs"))
            except Exception as error:
                report.update(state="stopped", stop_reason=type(error).__name__)
                save()
                raise
            rows.append(row)
            report["active"] = None
            print(f"Round {repeat} {case['id']} {arm} {row['stop_reason']} "
                  f"patch={row['patch_passed']} read={row['repeated_reads']} misuse={row['misuse']}", flush=True)
            if guard.stop_reason or row["patch_passed"] is None or row["cleanup_status"] not in {"complete", "not_needed"}:
                report.update(state="stopped", stop_reason=guard.stop_reason or "evaluation_infrastructure")
                break
            save()
        if report["state"] == "stopped":
            break
    if report["state"] == "running":
        report["state"] = "complete"
    save()
    (batch / "report.md").write_text(draw_report(report), encoding="utf-8")
    print(f"{report['state']} {len(rows)}/48", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
