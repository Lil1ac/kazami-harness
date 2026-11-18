"""汇总十二题项目记忆对照，保留已判定、未判定及有效配对的实际分母。"""

from collections import Counter
from decimal import Decimal


ARMS = ("A", "B")  # A 关闭项目记忆，B 启用已确认记忆和历史检索。
KINDS = {"reuse": 6, "stale": 4, "control": 2}  # 按冻结题型保留计划分母。
TERMINAL = {"completed", "limit_exceeded", "timed_out", "waiting_input", "failed", "cancelled", "interrupted"}
INFRASTRUCTURE = {
    "internal_error", "unknown_usage", "cleanup_unconfirmed", "model_transport", "model_authentication",
    "model_rate_limited", "model_bad_request", "model_tls_verification", "evaluation_model_changed",
    "evaluation_rate_mismatch", "service_shutdown", "batch_model_limit", "batch_cost_limit",
    "batch_known_token_threshold", "evaluation_infrastructure", "scorer_error",
}  # 外部服务与宿主异常保留原始记录和费用，不进入同题效果比较。
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")  # 供应商报告的输入、输出及总量。


def _index(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """按题号和 A/B 组索引 rows；两臂题型不一致、重复试次或超计划数量时拒绝。"""
    result: dict[str, dict[str, dict]] = {}
    for row in rows:
        key, arm, kind = row["case_id"], row["arm"], row["kind"]
        entries = result.setdefault(key, {})
        if arm not in ARMS or kind not in KINDS or arm in entries:
            raise ValueError(f"试次组别、题型无效或试次重复：{key} {arm} {kind}")
        if entries and next(iter(entries.values()))["kind"] != kind:
            raise ValueError(f"同一题两臂的题型不一致：{key}")
        entries[arm] = row
    counts = Counter(next(iter(entries.values()))["kind"] for entries in result.values())
    if any(counts[kind] > count for kind, count in KINDS.items()):
        raise ValueError("记忆评测试次超过冻结题量")
    return result


def _is_count(value: object) -> bool:
    """判断 value 是否为可计量的非负整数；布尔和缺失值不参与统计。"""
    return type(value) is int and value >= 0


def _usage_complete(row: dict) -> bool:
    """核实 row 用量回执完整，且输入与输出之和等于总 token。"""
    usage = row.get("usage") or {}
    return (row.get("usage_complete") is True and all(_is_count(usage.get(field)) for field in TOKEN_FIELDS)
            and usage["prompt_tokens"] + usage["completion_tokens"] == usage["total_tokens"])


def _scored(row: dict) -> bool:
    """确认 row 有可解释终态和独立评分，返回能否汇总正确性与交付。"""
    return (row.get("execution_status") in TERMINAL - {"cancelled", "interrupted"}
            and row.get("stop_reason") not in INFRASTRUCTURE and row.get("comparison_valid", True) is True
            and type(row.get("patch_passed")) is bool and type(row.get("delivery")) is bool)


def _boolean(rows: list[dict], field: str) -> dict[str, int]:
    """汇总 rows 的 field 真假值，返回真值次数和已判定分母。"""
    known = [row[field] for row in rows if type(row.get(field)) is bool]
    return {"count": sum(known), "scored": len(known)}


def _totals(rows: list[dict], fields: tuple[str, ...]) -> dict[str, int | None]:
    """累计 rows 的 fields；无样本或该列有缺项时留空，真实零次保留为 0。"""
    totals = {}
    for field in fields:
        values = [row.get(field) for row in rows]
        totals[field] = sum(values) if values and all(_is_count(value) for value in values) else None
    return totals


def _arm_metrics(rows: list[dict]) -> dict:
    """计算一臂 rows 的独立正确性、交付、资源和事件数据；用量缺失时精确汇总留空。"""
    scored = [row for row in rows if _scored(row)]
    complete = bool(rows) and all(_usage_complete(row) for row in rows)
    usage = {key: sum(row["usage"][key] for row in rows) for key in TOKEN_FIELDS} if complete else dict.fromkeys(TOKEN_FIELDS)
    return {
        "recorded": len(rows), "finished": sum(row.get("execution_status") in TERMINAL for row in rows),
        "patch": _boolean(scored, "patch_passed"), "delivery": _boolean(scored, "delivery"),
        "old_retrieved": _boolean(scored, "old_retrieved"), "old_exposed": _boolean(scored, "old_exposed"),
        "misuse": _boolean(scored, "misuse"),
        "usage_complete": complete, "usage": usage,
        **_totals(rows, ("tool_calls", "search_calls", "http_attempts")),
    }


def _outcomes(rows: list[dict]) -> dict[str, int]:
    """分别统计误用判定和完整交付。未交付的试次也可能没有可判定产物。"""
    misuse = sum(row.get("misuse") is True for row in rows)
    safe = sum(row.get("misuse") is False and row.get("delivery") is True for row in rows)
    undelivered = sum(row.get("delivery") is False for row in rows)
    return {"misuse": misuse, "safe_delivery": safe, "undelivered": undelivered,
            "unassessed": sum(row.get("misuse") is None for row in rows)}


def _reads(rows: list[dict]) -> dict:
    """汇总有共同可计量口径的 rows，返回重复读取、字节和所有工具开销。"""
    return {"cases": len(rows), **_totals(rows, ("repeated_reads", "repeated_bytes", "search_calls", "tool_calls"))}


def summarize(rows: list[dict]) -> dict:
    """从 rows 构建固定十二题覆盖状态、同题效果及各题型指标，结果可直接写入 JSON。"""
    by_case = _index(rows)
    finished = [pair for pair in by_case.values()
                if all(arm in pair and pair[arm].get("execution_status") in TERMINAL for arm in ARMS)]
    valid = [pair for pair in finished if all(_scored(row) and _usage_complete(row) for row in pair.values())]
    grouped = {}
    for kind, planned in KINDS.items():
        pairs = [pair for pair in valid if pair["A"]["kind"] == kind]
        details = {arm: _arm_metrics([pair[arm] for pair in pairs]) for arm in ARMS}
        for arm in ARMS:
            details[arm]["outcomes"] = _outcomes([pair[arm] for pair in pairs])
        grouped[kind] = {"planned": planned, "valid_pairs": len(pairs), "arms": details}

    reuse = [pair for pair in valid if pair["A"]["kind"] == "reuse" and all(
        _is_count(row.get(field)) for row in pair.values() for field in ("repeated_reads", "repeated_bytes")
    )]
    delivered = [pair for pair in reuse if all(row["delivery"] for row in pair.values())]
    grouped["reuse"]["reads"] = {arm: _reads([pair[arm] for pair in reuse]) for arm in ARMS}
    grouped["reuse"]["both_delivered_reads"] = {arm: _reads([pair[arm] for pair in delivered]) for arm in ARMS}
    return {
        "planned_cases": sum(KINDS.values()), "planned_trials": sum(KINDS.values()) * len(ARMS),
        "recorded_cases": len(by_case), "recorded_trials": len(rows),
        "finished_trials": sum(row.get("execution_status") in TERMINAL for row in rows),
        "finished_pairs": len(finished), "valid_pairs": len(valid),
        "complete": len(finished) == sum(KINDS.values()), "comparison_complete": len(valid) == sum(KINDS.values()),
        "arms": {arm: _arm_metrics([row for row in rows if row["arm"] == arm]) for arm in ARMS},
        "paired": {arm: _arm_metrics([pair[arm] for pair in valid]) for arm in ARMS},
        "kinds": grouped,
    }


def _display(value: object) -> str:
    """生成表格单行文本，value 缺失时显示空位，转义管道符。"""
    if value is None:
        return "—"
    if _is_count(value):
        return f"{value:,}"
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _fraction(value: dict) -> str:
    """用实际已判定数渲染 value 的分子分母，零分母时保留空位。"""
    return f"{value['count']}/{value['scored']}" if value["scored"] else "—"


def _outcome(value: object) -> str:
    """把 value 显示为是、否或未判定。"""
    return "是" if value is True else "否" if value is False else "—"


def _price(value: object) -> str:
    """将账本的美元万亿分之一单位 value 显示为美元，未知时保留空位。"""
    return f"${Decimal(value).scaleb(-12):f}" if _is_count(value) else "—"


def _trial_text(row: dict | None) -> list[str]:
    """渲染逐题终态、正确/完整、重读次数、误用和 Run 编号；缺失行显示空位。"""
    if row is None:
        return ["—"] * 5
    status = _display(row.get("execution_status"))
    if row.get("stop_reason"):
        status += f" / {_display(row['stop_reason'])}"
    return [status, f"{_outcome(row.get('patch_passed'))} / {_outcome(row.get('delivery'))}",
            _display(row.get("repeated_reads")), _outcome(row.get("misuse")), _display(row.get("run_id"))]


def render(batch: dict) -> str:
    """根据 batch 的试次、冻结题单、运行状态及账本生成 Markdown 对照报告。"""
    rows = batch.get("trials", [])
    summary = summarize(rows)
    out = ["# AgentHarness 跨会话项目记忆对照", "",
           "A 关闭项目记忆，B 启用已确认记忆和历史检索。两组从相同项目文件和预置学习材料开始，使用独立的新会话。", "",
           f"计划 {summary['planned_cases']} 题、{summary['planned_trials']} 次，"
           f"已记录 {summary['recorded_trials']}/{summary['planned_trials']} 次，"
           f"有终态 {summary['finished_trials']}/{summary['planned_trials']} 次，"
           f"形成 {summary['valid_pairs']}/{summary['planned_cases']} 个有效配对。"]
    if batch.get("state"):
        out += ["", f"批次状态 `{_display(batch['state'])}`。"]
    if batch.get("stop_reason"):
        out += ["", f"停批原因 `{_display(batch['stop_reason'])}`。"]
    if not summary["comparison_complete"]:
        out += ["", "以下按已完成且两组均得到独立评分和完整用量的同题配对汇总，缺项留空。"]

    out += ["", "## 全部题目的成对结果", "",
            "| 组别 | 产物正确 | 完整交付 | 工具步数 | HTTP 尝试 | 输入 / 输出 / 总 Token |",
            "|---|---:|---:|---:|---:|---|"]
    for arm, values in summary["paired"].items():
        tokens = " / ".join(_display(values["usage"][key]) for key in TOKEN_FIELDS)
        out.append(f"| {arm} | {_fraction(values['patch'])} | {_fraction(values['delivery'])} | "
                   f"{_display(values['tool_calls'])} | {_display(values['http_attempts'])} | {tokens} |")
    budget = batch.get("budget") or {}
    if budget:
        out += ["", f"批次逐请求账本累计接纳 {_display(budget.get('calls_admitted'))} 次，"
                f"已知估算费用 {_price(budget.get('known_usd_picos'))}，"
                f"本执行批次的未结算预留为 {_price(budget.get('reserved_usd_picos'))}。"]
    recovery = batch.get("recovery")
    if recovery:
        held = recovery["held_cents"] / 100
        original = recovery["original_limit_cents"] / 100
        out += ["", f"首次尝试在 TLS 校验阶段终止，恢复时保留原账本的 ${held:.2f} 预留，"
                f"计入 ${original:.2f} 总上限。"]

    out += ["", "## 六道复用题", "",
            "重复读取按学习阶段已有的同一文件版本和覆盖字节计算，记忆检索和回读计入工具步数。", "",
            "| 组别 | 样本范围 | 有效对数 | 重复读取 | 重复字节 | 搜索调用 | 工具步数 |",
            "|---|---|---:|---:|---:|---:|---:|"]
    reuse = summary["kinds"]["reuse"]
    for label, key in (("已配对且可计量", "reads"), ("两臂均完整交付", "both_delivered_reads")):
        for arm, values in reuse[key].items():
            fields = ("cases", "repeated_reads", "repeated_bytes", "search_calls", "tool_calls")
            out.append(f"| {arm} | {label} | " + " | ".join(_display(values[field]) for field in fields) + " |")

    for kind, heading in (("stale", "四道失效题"), ("control", "两道干扰负对照")):
        section = summary["kinds"][kind]
        out += ["", f"## {heading}", "", f"有效配对 {section['valid_pairs']}/{section['planned']}。", "",
                "| 组别 | 旧值检索 | 旧值进入输入 | 产物误用 / 可判定 | 安全交付 | 未交付 | 未判定 |",
                "|---|---:|---:|---:|---:|---:|---:|"]
        for arm, values in section["arms"].items():
            outcomes = values["outcomes"]
            counts = (_display(outcomes[field] if section["valid_pairs"] else None)
                      for field in ("safe_delivery", "undelivered", "unassessed"))
            out.append(f"| {arm} | {_fraction(values['old_retrieved'])} | {_fraction(values['old_exposed'])} | "
                       f"{_fraction(values['misuse'])} | "
                       + " | ".join(counts) + " |")

    by_case = _index(rows)
    titles = {entry["case_id"]: entry.get("title") for entry in batch.get("schedule", [])}
    for row in rows:
        titles.setdefault(row["case_id"], row.get("title"))
    out += ["", "## 逐题 Run", "",
            "| 题目 | 组别 | 终态 | 正确 / 完整 | 重复读取 | 产物误用 | Run |",
            "|---|---|---|---|---:|---|---|"]
    for case_id, title in titles.items():
        for arm in ARMS:
            cells = [_display(title or case_id), arm, *_trial_text(by_case.get(case_id, {}).get(arm))]
            out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"
