"""从完整计划和逐次回执计算指标；缺失试次不能被静默排除。"""

import math
import random
from statistics import mean
from itertools import combinations
from collections import Counter


def quantile(values: list[float], probability: float) -> float | None:
    """返回 values 的最近秩分位数；probability 为0..1，空样本返回None。"""
    return sorted(values)[max(0, math.ceil(probability * len(values)) - 1)] if values else None


def metric_pass(row: dict, metric: str) -> bool:
    """返回 row 的交付或独立修复结果；metric=resolved 时只使用外部评分。"""
    return row.get("grade", {}).get("status") == "passed" if metric == "resolved" else row["success"]


def summarize(report: dict) -> dict:
    """汇总冻结 report 的计划和回执，返回策略与任务级配对区间；不访问模型或当前源码。"""
    groups = []
    paired = []
    for strategy in [group["id"] for group in report["experiment"]["groups"]]:
        started = [row for row in report["trials"] if row["strategy"] == strategy]
        rows = [row for row in started if not row.get("interrupted")]
        expected = [row for row in report["schedule"] if row["strategy"] == strategy]
        successes = sum(row["success"] for row in rows)
        tokens = sum(row["known_tokens"] for row in started)
        complete = len(rows) == len(expected)
        usage_complete = bool(started) and all(row["usage_complete"] for row in started)
        durations = [row["duration_seconds"] for row in rows]
        triple = []
        if report["experiment"]["repeats"] >= 3:
            for case in report["cases"]:
                first = [row for row in rows if row["case_id"] == case["id"] and row["repeat"] < 3]
                if len(first) == 3:
                    triple.append(all(row["success"] for row in first))
        group = {"strategy": strategy, "planned": len(expected), "finished": len(rows), "successes": successes,
                       "patch_passes": sum(row.get("grade", {}).get("status") == "passed" for row in rows),
                       "scored": sum(row.get("grade", {}).get("status") in {"passed", "failed"} for row in rows),
                       "success_rate": successes / len(rows) if rows else None, "complete": complete,
                       "pass3": mean(triple) if len(triple) == len(report["cases"]) and triple else None,
                       "known_tokens": tokens, "usage_complete": usage_complete,
                       "tokens_per_success": tokens / successes if successes and usage_complete else None,
                       "duration_p50": quantile(durations, .5), "duration_p95": quantile(durations, .95),
                       "intervention_rate": mean(row["needs_input"] for row in rows) if rows else None,
                       "model_calls": sum(row["model_calls"] for row in started)}
        if report["experiment"].get("kind") == "memory":
            traces = [row["memory"] for row in rows if "memory" in row]
            drafts = report.get("consolidations", []) if strategy == "memory_full" else []
            consolidation_tokens = sum(item["usage"].get("total_tokens", 0) for item in drafts)
            total_complete = usage_complete and all(item["usage_complete"] for item in drafts)
            group.update(memory={"required_trials": sum(item["all_required_exposed"] is not None for item in traces),
                "all_evidence_exposed": sum(item["all_required_exposed"] is True for item in traces),
                "forbidden_hits": sum(len(item["forbidden_hits"]) for item in traces),
                "searches": sum(item["searches"] for item in traces),
                "consolidation_tokens": consolidation_tokens, "total_tokens": tokens + consolidation_tokens,
                "total_usage_complete": total_complete, "consolidation_calls": sum(item["model_calls"] for item in drafts),
                "review_seconds": sum(item.get("review_seconds", 0) for item in drafts),
                "total_tokens_per_success": (tokens + consolidation_tokens) / successes if successes and total_complete else None})
        costs = [row.get("cost") for row in started]
        cost_complete = bool(costs) and all(cost and cost["complete"] for cost in costs)
        known_cost = sum(cost["known_usd_picos"] for cost in costs if cost)
        patch_passes = group["patch_passes"]
        definitions = {item["id"]: item for item in report["experiment"]["groups"]}
        group.update(executor=definitions[strategy]["executor"], context_policy=definitions[strategy]["context_policy"],
            patch_pass_rate=patch_passes / len(rows) if rows else None,
            known_usd_picos=known_cost, cost_complete=cost_complete,
            cost_per_delivery_usd_picos=known_cost / successes if cost_complete and successes else None,
            cost_per_resolved_usd_picos=known_cost / patch_passes if cost_complete and patch_passes else None,
            failures=dict(Counter(row.get("failure_type") or row.get("stop_reason") or "none" for row in rows if not row["success"])),
            summary_calls=sum(row.get("summary_calls", 0) for row in rows),
            compression_trials=sum(bool(row.get("compression_applied")) for row in rows),
            input_bytes_peak=max((row.get("input_bytes_peak", 0) for row in rows), default=0),
            context_limit_failures=sum(row.get("stop_reason") == "max_model_input_bytes" for row in rows))
        for timer in ("agent_seconds", "preparation_seconds", "grading_seconds", "model_seconds", "tool_seconds"):
            measured = [row[timer] for row in rows if row.get(timer) is not None]
            group[timer + "_p50"] = quantile(measured, .5)
            group[timer + "_p95"] = quantile(measured, .95)
        group["strata"] = {}
        for axis in ("project", "category"):
            strata = []
            for name in sorted({row[axis] for row in rows if axis in row}):
                members = [row for row in rows if row.get(axis) == name]
                strata.append({"name": name, "trials": len(members), "deliveries": sum(row["success"] for row in members),
                    "resolved": sum(row.get("grade", {}).get("status") == "passed" for row in members)})
            group["strata"][axis] = strata
        requests = [item for item in report.get("budget", {}).get("requests", [])
                    if item["run_id"] in {row["run_id"] for row in started if "run_id" in row}]
        valid_usage = [item["usage"] for item in requests if item.get("usage") and item["status"] == "settled"]
        group["usage"] = {key: sum(item.get(key, 0) for item in valid_usage) for key in
            ("prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens", "total_tokens")}
        group["usage"]["reasoning_tokens"] = sum(item.get("completion_tokens_details", {}).get("reasoning_tokens", 0) for item in valid_usage)
        group["cache_hit_ratio"] = group["usage"]["prompt_cache_hit_tokens"] / group["usage"]["prompt_tokens"] if group["usage"]["prompt_tokens"] else None
        groups.append(group)
    context_pairs, patch_pairs = [], []
    comparable = [row for row in report["trials"] if not row.get("interrupted") and row.get("comparison_valid", True)]
    identifiers = [group["id"] for group in report["experiment"]["groups"]]
    for baseline, strategy in combinations(identifiers, 2):
        for metric in ("delivery", "resolved"):
            differences = []
            outcomes = {"wins": [], "losses": [], "ties": []}
            for case in report["cases"]:
                left = [row for row in comparable if row["case_id"] == case["id"] and row["strategy"] == baseline]
                right = [row for row in comparable if row["case_id"] == case["id"] and row["strategy"] == strategy]
                if len(left) == len(right) == report["experiment"]["repeats"]:
                    if {row["repeat"] for row in left} != {row["repeat"] for row in right}:
                        continue
                    difference = mean(metric_pass(row, metric) for row in right) - mean(metric_pass(row, metric) for row in left)
                    differences.append(difference)
                    outcomes["wins" if difference > 0 else "losses" if difference < 0 else "ties"].append(case["id"])
            if report["experiment"].get("kind") != "memory":
                target = context_pairs if metric == "delivery" else patch_pairs
                target.append({"baseline": baseline, "candidate": strategy, "metric": metric, **outcomes,
                               "valid_pairs": len(differences), "planned_pairs": len(report["cases"])})
            # 同题多次运行仍以题为采样单位；不把稳定性重复伪装成更多独立题。
            if len(differences) == len(report["cases"]) and len(differences) >= 2:
                rng = random.Random(report["experiment"]["seed"])
                samples = [mean(rng.choices(differences, k=len(differences))) for _ in range(2000)]
                paired.append({"baseline": baseline, "candidate": strategy, "metric": metric, "tasks": len(differences),
                               "difference": mean(differences), "ci95": [quantile(samples, .025), quantile(samples, .975)]})
    memory_pairs = []
    if report["experiment"].get("kind") == "memory":
        # 保留不完整配对的分母；逐题成败直接观察，供应商或评分环境异常不能参与能力归因。
        by_case = {(row["case_id"], row["strategy"]): row for row in report["trials"] if row.get("comparison_valid")}
        for left, right in (("memory_current", "memory_history"), ("memory_history", "memory_full"), ("memory_off", "memory_current")):
            won, lost, tied = [], [], []
            for case in report["cases"]:
                first, second = by_case.get((case["id"], left)), by_case.get((case["id"], right))
                if first is None or second is None:
                    continue
                target = tied if first["success"] == second["success"] else won if second["success"] else lost
                target.append(case["id"])
            memory_pairs.append({"baseline": left, "candidate": right, "wins": won, "losses": lost, "ties": tied,
                                 "valid_pairs": len(won) + len(lost) + len(tied), "planned_pairs": len(report["cases"])})
        paired = []  # 每题单次小样本只公布同题赢输平。
    return {"groups": groups, "paired": paired, **({"memory_pairs": memory_pairs} if memory_pairs else {}),
            **({"context_pairs": context_pairs} if context_pairs else {}),
            **({"patch_pairs": patch_pairs} if patch_pairs else {})}
