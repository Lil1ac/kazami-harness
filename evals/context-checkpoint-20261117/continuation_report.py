"""汇总固定十二题的真实续跑回执，区分计划覆盖、评分与用量。"""

from __future__ import annotations

from decimal import Decimal


PLANNED_CASES = 12  # 每臂各执行一次，部分批次仍使用固定题单分母。
ARMS = ("A", "B")  # A 关闭压缩，B 启用混合压缩。
INPUT_LIMITS = ("max_model_input_bytes", "max_model_input_tokens")  # 宿主输入门禁的两个结束原因。
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")  # 供应商报告的精确用量字段。
FINISHED = {"completed", "limit_exceeded", "timed_out", "failed", "cancelled", "interrupted"}  # 已有可记录的终态。
SCORED = {"passed", "failed"}  # 独立断言的有效结果，评分环境异常另计。
INFRASTRUCTURE = {  # 外部服务或宿主失败，仍保留已用资源及逐次回执。
    "internal_error", "unknown_usage", "cleanup_unconfirmed", "model_transport",
    "model_authentication", "model_rate_limited", "model_bad_request", "model_tls_verification",
    "evaluation_model_changed", "evaluation_rate_mismatch", "service_shutdown",
    "batch_model_limit", "batch_cost_limit", "batch_known_token_threshold",
}


def _index(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """按题目和组别索引 rows，重复试次或超出冻结题量时抛出 ValueError。"""
    by_case: dict[str, dict[str, dict]] = {}
    for row in rows:
        case_id = row["case_id"]
        arm = row["arm"]
        entries = by_case.setdefault(case_id, {})
        if arm not in ARMS or arm in entries:
            raise ValueError(f"续跑回执的组别无效或重复：{case_id} {arm}")
        entries[arm] = row
    if len(by_case) > PLANNED_CASES:
        raise ValueError("续跑回执超过冻结的十二道题")
    return by_case


def _scoreable(row: dict) -> bool:
    """判断 row 是否有可解释的终态和独立评分，返回是否可汇总任务结果。"""
    return (
        row.get("execution_status") in FINISHED - {"cancelled", "interrupted"}
        and row.get("stop_reason") not in INFRASTRUCTURE
        and row.get("grade_status") in SCORED
        and row.get("comparison_valid", True) is True
    )


def _is_usage(value: object) -> bool:
    """确认 value 是非负整数计量值，布尔值与缺失值均不属于用量。"""
    return type(value) is int and value >= 0


def _add_known(values: list) -> int | None:
    """累加 values 中已知计量值，列表无已知值时返回 None。"""
    available = [value for value in values if _is_usage(value)]
    return sum(available) if available else None


def _tokens(row: dict) -> dict:
    """返回 row 的计量字典，缺省用量返回空映射。"""
    return row.get("usage") or {}


def _usage_complete(row: dict) -> bool:
    """判断 row 的三个精确用量字段都已结算并且和式一致。"""
    usage = _tokens(row)
    return (
        row.get("usage_complete") is True
        and all(_is_usage(usage.get(key)) for key in TOKEN_FIELDS)
        and usage["prompt_tokens"] + usage["completion_tokens"] == usage["total_tokens"]
    )


def _arm_metrics(rows: list[dict]) -> dict:
    """计算 rows 对应组别的结果覆盖、终态、实际资源开销和缺失情况。"""
    finished = [row for row in rows if row.get("execution_status") in FINISHED]
    scoreable = [row for row in finished if _scoreable(row)]
    patch = [row["patch_passed"] for row in scoreable if type(row.get("patch_passed")) is bool]
    deliveries = [row["delivery"] for row in scoreable if type(row.get("delivery")) is bool]
    known_tokens = {key: _add_known([_tokens(row).get(key) for row in rows]) for key in TOKEN_FIELDS}
    usage_complete = bool(rows) and all(_usage_complete(row) for row in rows)
    costs = [row.get("cost") or {} for row in rows]
    known_cost = _add_known([cost.get("known_usd_picos") for cost in costs])
    cost_complete = bool(costs) and all(
        cost.get("complete") is True and _is_usage(cost.get("known_usd_picos")) for cost in costs
    )
    http = [row.get("http_attempts") for row in rows]
    limit_stops = {reason: sum(row.get("stop_reason") == reason for row in finished) for reason in INPUT_LIMITS}
    return {
        "recorded": len(rows),
        "finished": len(finished),
        "scoreable": len(scoreable),
        "grade_errors": sum(row.get("grade_status") == "error" for row in rows),
        "input_limits": limit_stops,
        "input_limit_stops": sum(limit_stops.values()),
        "patch_passes": sum(patch),
        "patch_scored": len(patch),
        "deliveries": sum(deliveries),
        "delivery_scored": len(deliveries),
        "http_attempts": _add_known(http) if all(_is_usage(value) for value in http) else None,
        "usage_complete": usage_complete,
        "usage": known_tokens if usage_complete else dict.fromkeys(TOKEN_FIELDS),
        "known_usage": known_tokens,
        "cost_complete": cost_complete,
        "cost_usd_picos": known_cost if cost_complete else None,
        "known_cost_usd_picos": known_cost,
    }


def summarize(rows: list[dict]) -> dict:
    """汇总已记录 rows，返回按全体记录和同题有效配对计算的两套组别数据。"""
    by_case = _index(rows)
    finished_pairs = [
        entries for entries in by_case.values()
        if all(arm in entries and entries[arm].get("execution_status") in FINISHED for arm in ARMS)
    ]
    valid_pairs = [
        entries for entries in finished_pairs if all(
            _scoreable(row) and _usage_complete(row)
            and type(row.get("patch_passed")) is bool and type(row.get("delivery")) is bool
            for row in entries.values()
        )
    ]
    return {
        "planned_cases": PLANNED_CASES,
        "planned_trials": PLANNED_CASES * len(ARMS),
        "recorded_cases": len(by_case),
        "recorded_trials": len(rows),
        "finished_trials": sum(row.get("execution_status") in FINISHED for row in rows),
        "finished_pairs": len(finished_pairs),
        "valid_pairs": len(valid_pairs),
        "complete": len(finished_pairs) == PLANNED_CASES,
        "comparison_complete": len(valid_pairs) == PLANNED_CASES,
        "overall": _arm_metrics(rows),
        "arms": {arm: _arm_metrics([row for row in rows if row["arm"] == arm]) for arm in ARMS},
        "paired": {arm: _arm_metrics([entries[arm] for entries in valid_pairs]) for arm in ARMS},
    }


def _display(value: object) -> str:
    """将 value 转为单行 Markdown 表格文本，None 表示无数据。"""
    return "—" if value is None else str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _fraction(passed: int, scored: int) -> str:
    """展示 passed 相对已统计 scored 的分子分母，零样本显示缺失。"""
    return f"{passed}/{scored}" if scored else "—"


def _outcome(value: object) -> str:
    """把独立断言 value 直接映射为通过、未通过或无评分。"""
    return "通过" if value is True else "未通过" if value is False else "—"


def _price(picos: int | None) -> str:
    """将美元万亿分之一单位的 picos 转为十进制字符串，缺失显示破折号。"""
    return f"${Decimal(picos).scaleb(-12):f}" if picos is not None else "—"


def _token_display(exact: dict, known: dict) -> str:
    """依次显示 exact 的输入、输出、总量，缺失时标注 known 的下限。"""
    parts = []
    for key in TOKEN_FIELDS:
        value = exact.get(key)
        observed = known.get(key)
        if value is None and observed is not None:
            parts.append(f"未知（已知 {observed:,}）")
        else:
            parts.append(f"{value:,}" if _is_usage(value) else "—")
    return " / ".join(parts)


def _trial_display(row: dict | None) -> tuple[str, str, str, str]:
    """格式化 row 的终态、独立评分、Token 和运行编号，缺行显示未记录。"""
    if row is None:
        return "未记录", "—", "—", "—"
    status = _display(row.get("execution_status"))
    if row.get("stop_reason") is not None:
        status += f" / {_display(row['stop_reason'])}"
    if row.get("grade_status") == "error":
        score = "评分异常"
    elif not _scoreable(row):
        score = "—"
    else:
        score = f"{_outcome(row.get('patch_passed'))} / {_outcome(row.get('delivery'))}"
    known = _tokens(row)
    exact = known if _usage_complete(row) else {}
    return status, score, _token_display(exact, known), _display(row.get("run_id"))


def render(rows: list[dict], summary: dict, state: dict) -> str:
    """用 rows 和 summary 渲染 Markdown，state 可附带冻结题单、批次状态及停批原因。"""
    by_case = _index(rows)
    if summary["recorded_trials"] != len(rows):
        raise ValueError("续跑回执和汇总记录数不一致")
    planned = state.get("cases") or state.get("schedule") or []
    titles = {entry.get("id", entry.get("case_id")): entry.get("title") for entry in planned}
    for row in rows:
        if not titles.get(row["case_id"]):
            titles[row["case_id"]] = row["title"]

    out = [
        "# AgentHarness 固定检查点真实续跑",
        "",
        "A 关闭压缩，B 启用混合压缩。两组继承同题冻结历史，由离线容器独立评分。"
        "共用项目内文件工具，B 额外启用归档回读。",
        "",
        f"计划 {summary['planned_cases']} 题、{summary['planned_trials']} 次。"
        f"已收录 {summary['recorded_trials']}/{summary['planned_trials']} 次，"
        f"有终态 {summary['finished_trials']}/{summary['planned_trials']} 次，"
        f"双臂均有终态 {summary['finished_pairs']}/{summary['planned_cases']} 题，"
        f"形成 {summary['valid_pairs']}/{summary['planned_cases']} 个有效配对。",
    ]
    status = state.get("state") or state.get("status")
    if status:
        out.extend(["", f"批次状态为 `{_display(status)}`。"])
    if state.get("stop_reason"):
        out.extend(["", f"停批原因是 `{_display(state['stop_reason'])}`。"])
    if summary["comparison_complete"]:
        blocks = [("12 个有效配对汇总", summary["paired"])]
    else:
        out.extend(["", "当前仅报告已取得的回执和可比较的配对。缺记录与缺评分会保留为空。"])
        blocks = [("各组已记录试次", summary["arms"]), ("仅统计同题有效配对", summary["paired"])]

    for heading, arms in blocks:
        out.extend([
            "", f"## {heading}", "",
            "| 组别 | 有终态 | 字节超限 | Token 超限 | 补丁通过 | 完整交付 | HTTP 次数 | 输入 / 输出 / 总 Token | 估算费用 |",
            "|---|---:|---:|---:|---:|---:|---:|---|---:|",
        ])
        for arm, metrics in arms.items():
            cost = _price(metrics["cost_usd_picos"])
            if metrics["cost_usd_picos"] is None and metrics["known_cost_usd_picos"] is not None:
                cost = f"未知（已知 {_price(metrics['known_cost_usd_picos'])}）"
            finished = metrics["finished"]
            limits = metrics["input_limits"]
            out.append(
                f"| {arm} | {finished} | {_fraction(limits[INPUT_LIMITS[0]], finished)} | "
                f"{_fraction(limits[INPUT_LIMITS[1]], finished)} | "
                f"{_fraction(metrics['patch_passes'], metrics['patch_scored'])} | "
                f"{_fraction(metrics['deliveries'], metrics['delivery_scored'])} | {_display(metrics['http_attempts'])} | "
                f"{_token_display(metrics['usage'], metrics['known_usage'])} | {cost} |"
            )

    out.extend([
        "", "## 逐题 A/B 回执", "",
        "| 题目 | A 终态 | A 补丁 / 交付 | A 输入 / 输出 / 总 Token | A Run | B 终态 | B 补丁 / 交付 | B 输入 / 输出 / 总 Token | B Run |",
        "|---|---|---|---|---|---|---|---|---|",
    ])
    for case_id, title in titles.items():
        out.append("| " + " | ".join([
            _display(title or case_id),
            *_trial_display(by_case.get(case_id, {}).get("A")),
            *_trial_display(by_case.get(case_id, {}).get("B")),
        ]) + " |")

    out.extend([
        "",
        "同题有效配对要求两侧均有可解释终态、独立评分和完整用量。补丁通过和完整交付分别按各自有结果的次数计算。"
        "费用沿用请求账本的已知金额。未发起模型请求的运行可以有完整的零用量。",
        "",
    ])
    return "\n".join(out)
