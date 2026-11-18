"""生成四配置长期记忆诊断报告，效果比较使用同题共有的有效回执。"""

from decimal import Decimal


# 四种配置均开启项目记忆。第一项为公共提示，第二项为已确认记忆的呈现方式。
VARIANTS = {
    1: "现有提示、现有呈现",
    2: "移除核对措辞、现有呈现",
    3: "现有提示、标明宿主核验",
    4: "移除核对措辞、标明宿主核验",
}
CONTRASTS = ((2, 1), (3, 1), (4, 2), (4, 3))  # （对照后配置，对照前配置）。
PLANNED_CASES = 4  # 两道稳定规则、两道稳定事实，每道在四种配置下各跑一次。
TERMINAL = {"completed", "limit_exceeded", "timed_out", "waiting_input", "failed", "cancelled", "interrupted"}  # 已写入 Runner 终态。
INFRASTRUCTURE = {
    "unknown_usage", "cleanup_unconfirmed", "internal_error", "model_transport", "model_authentication",
    "model_rate_limited", "model_bad_request", "model_tls_verification", "evaluation_model_changed",
    "evaluation_rate_mismatch", "service_shutdown", "batch_model_limit", "batch_cost_limit",
    "batch_known_token_threshold", "evaluation_infrastructure", "scorer_error",
}  # 即使写入账本，基础设施异常也不进入效果配对。
COUNTS = ("repeated_reads", "repeated_bytes", "tool_calls", "search_calls", "http_attempts")  # 同题可聚合的回执计数。
TOKENS = ("prompt_tokens", "completion_tokens", "total_tokens")  # 供应商用量的独立字段。


def _is_count(value: object) -> bool:
    """判断 value 是否是非负整数实测值；布尔值和缺项排除。"""
    return type(value) is int and value >= 0


def _index(rows: list[dict]) -> dict[str, dict[int, dict]]:
    """把 rows 索引到题号和配置；拒绝重复槽、非法配置或同题归类不一致。"""
    cases: dict[str, dict[int, dict]] = {}
    for row in rows:
        case_id, variant, scenario = row["case_id"], row["variant"], row["scenario"]
        slots = cases.setdefault(case_id, {})
        if type(variant) is not int or variant not in VARIANTS or scenario not in {"rule", "fact"} or variant in slots:
            raise ValueError(f"配置或题型无效，或同一槽位重复：{case_id} {variant} {scenario}")
        if slots and next(iter(slots.values()))["scenario"] != scenario:
            raise ValueError(f"同题的规则或事实归类不一致：{case_id}")
        slots[variant] = row
    if len(cases) > PLANNED_CASES:
        raise ValueError("记录数超出冻结的四道题")
    return cases


def _eligible(row: dict) -> bool:
    """核实 row 已完成有效输入、执行、评分及用量计量，返回是否可作同题对照。"""
    usage = row.get("usage") or {}
    return (
        row.get("execution_status") in TERMINAL - {"cancelled", "interrupted"}
        and row.get("stop_reason") not in INFRASTRUCTURE
        and row.get("injected_input_verified") is True
        and type(row.get("patch_passed")) is bool and type(row.get("delivery")) is bool
        and row.get("usage_complete") is True and all(_is_count(usage.get(key)) for key in TOKENS)
        and usage["prompt_tokens"] + usage["completion_tokens"] == usage["total_tokens"]
    )


def _aggregate(rows: list[dict]) -> dict:
    """统计已有效的 rows，列有缺项或没有样本时不给出汇总值。"""
    count = len(rows)
    totals = {}
    for field in COUNTS:
        values = [row.get(field) for row in rows]
        totals[field] = sum(values) if values and all(_is_count(value) for value in values) else None
    return {
        "n": count, **totals,
        "mean_repeated_reads": totals["repeated_reads"] / count if totals["repeated_reads"] is not None else None,
        "mean_repeated_bytes": totals["repeated_bytes"] / count if totals["repeated_bytes"] is not None else None,
        "mean_tools": totals["tool_calls"] / count if totals["tool_calls"] is not None else None,
        "patch_passes": sum(row["patch_passed"] for row in rows) if rows else None,
        "deliveries": sum(row["delivery"] for row in rows) if rows else None,
        "usage": {key: sum(row["usage"][key] for row in rows) if rows else None for key in TOKENS},
    }


def summarize(rows: list[dict]) -> dict:
    """汇总 rows 的覆盖率、逐配置资源和结果、四个共同题配对差值，返回可序列化数据。"""
    by_case = _index(rows)
    eligible = {case_id: {variant: row for variant, row in slots.items() if _eligible(row)}
                for case_id, slots in by_case.items()}
    variants = {}
    for variant in VARIANTS:
        recorded = [row for row in rows if row["variant"] == variant]
        selected = [slots[variant] for slots in eligible.values() if variant in slots]
        variants[str(variant)] = {
            "recorded": len(recorded),
            "injection_checked": sum(type(row.get("injected_input_verified")) is bool for row in recorded),
            "injection_verified": sum(row.get("injected_input_verified") is True for row in recorded),
            **_aggregate(selected),
        }
    contrasts = {}
    for after, before in CONTRASTS:
        common = [case_id for case_id, slots in eligible.items() if before in slots and after in slots]
        initial = _aggregate([eligible[case_id][before] for case_id in common])
        final = _aggregate([eligible[case_id][after] for case_id in common])
        fields = ("repeated_reads", "repeated_bytes", "mean_tools", "patch_passes", "deliveries")
        delta = {field: final[field] - initial[field] if initial[field] is not None and final[field] is not None else None
                 for field in fields}
        contrasts[f"{after}vs{before}"] = {"n": len(common), "case_ids": common, "before": initial, "after": final, "delta": delta}
    return {
        "planned_cases": PLANNED_CASES, "planned_trials": PLANNED_CASES * len(VARIANTS),
        "recorded_trials": len(rows), "finished_trials": sum(row.get("execution_status") in TERMINAL for row in rows),
        "valid_complete_cases": sum(len(slots) == len(VARIANTS) for slots in eligible.values()),
        "variants": variants, "contrasts": contrasts,
    }


def _display(value: object) -> str:
    """将 value 渲染为单行表格文本；缺项显示为横线。"""
    if value is None:
        return "—"
    if type(value) is int:
        return f"{value:,}"
    if type(value) is float:
        return f"{value:.2f}"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _result(value: object) -> str:
    """显示 value 的真、假或尚无判定。"""
    return "是" if value is True else "否" if value is False else "—"


def _fraction(passed: int | None, count: int) -> str:
    """渲染已通过数 passed 与实测分母 count。无样本留空。"""
    return f"{passed}/{count}" if count and passed is not None else "—"


def _price(picos: object) -> str:
    """将 picos 万亿分之一美元转换为美元显示；缺项留空。"""
    return f"${Decimal(picos).scaleb(-12):f}" if _is_count(picos) else "—"


def _difference(before: int | float | None, after: int | float | None, delta: int | float | None) -> str:
    """显示同题对照的 before、after 与差值 delta。三值之一未知时留空。"""
    if before is None or after is None or delta is None:
        return "—"
    sign = "+" if delta > 0 else ""
    return f"{_display(before)} → {_display(after)}（{sign}{_display(delta)}）"


def _trial_text(row: dict | None) -> list[str]:
    """生成逐题行余下字段，尚无 row 时填充对应数量的空位。"""
    if row is None:
        return ["—"] * 8
    state = _display(row.get("execution_status"))
    if row.get("stop_reason"):
        state += f" / {_display(row['stop_reason'])}"
    return [state, _result(row.get("injected_input_verified")), _display(row.get("repeated_reads")),
            _display(row.get("repeated_bytes")), _display(row.get("tool_calls")), _result(row.get("patch_passed")),
            _result(row.get("delivery")), _display(row.get("run_id"))]


def render(report: dict) -> str:
    """将 report 的原始试次、状态、计划和账本渲染为 Markdown。"""
    rows = report.get("trials", [])
    stats = summarize(rows)
    lines = [
        "# AgentHarness 项目记忆重读诊断", "",
        "四种配置均开启项目记忆。对照因素为公共提示中的核对措辞，以及已确认记忆是否说明宿主核验结果。", "",
        f"计划 {stats['planned_cases']} 题、{stats['planned_trials']} 次。"
        f"已记录 {stats['recorded_trials']}/{stats['planned_trials']}，"
        f"有终态 {stats['finished_trials']}/{stats['planned_trials']}，"
        f"四种配置均有有效结果 {stats['valid_complete_cases']}/{stats['planned_cases']} 题。",
    ]
    if report.get("state"):
        lines += ["", f"批次状态 `{_display(report['state'])}`。"]
    if report.get("stop_reason"):
        lines += ["", f"停批原因 `{_display(report['stop_reason'])}`。"]
    budget = report.get("budget") or {}
    if budget:
        lines += ["", f"账本接纳 {_display(budget.get('calls_admitted'))} 请求，"
                  f"已知 {_display(budget.get('known_tokens'))} token，估算 {_price(budget.get('known_usd_picos'))}，"
                  f"尚保留预留 {_price(budget.get('reserved_usd_picos'))}。"]

    lines += ["", "## 各配置结果", "",
              "| 配置 | 已记录 | 注入核验 | 有效题数 | 重读次数（题均） | 重读字节（题均） | 平均工具步数 | 搜索次数 | 补丁通过 | 完整交付 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for variant, title in VARIANTS.items():
        metrics = stats["variants"][str(variant)]
        cells = [f"{variant} · {title}", _display(metrics["recorded"]),
                 _fraction(metrics["injection_verified"], metrics["injection_checked"]), _display(metrics["n"])]
        for key in ("repeated_reads", "repeated_bytes"):
            total, mean = metrics[key], metrics[f"mean_{key}"]
            cells.append(f"{_display(total)}（{_display(mean)}）" if total is not None else "—")
        cells += [_display(metrics[key]) for key in ("mean_tools", "search_calls")]
        cells += [_fraction(metrics[key], metrics["n"]) for key in ("patch_passes", "deliveries")]
        lines.append("| " + " | ".join(cells) + " |")

    lines += ["", "## 同题对照", "",
              "每行分母取两种配置都完成输入核验、独立评分和用量记录的共同题目。差值为后配置减前配置。", "",
              "| 配置变化 | 共同有效题 | 重读次数 | 重读字节 | 平均工具步数 | 补丁通过 | 完整交付 |",
              "|---|---:|---|---|---|---|---|"]
    for after, before in CONTRASTS:
        comparison = stats["contrasts"][f"{after}vs{before}"]
        old, new, delta = (comparison[key] for key in ("before", "after", "delta"))
        cells = [f"{before} → {after}", _display(comparison["n"])]
        cells += [_difference(old[key], new[key], delta[key]) for key in ("repeated_reads", "repeated_bytes", "mean_tools")]
        cells += [f"{old[key]}/{comparison['n']} → {new[key]}/{comparison['n']}" if comparison["n"] else "—"
                  for key in ("patch_passes", "deliveries")]
        lines.append("| " + " | ".join(cells) + " |")

    lines += ["", "## 逐题回执", "",
              "| 题目 | 类型 | 配置 | 终态 | 注入核验 | 重读次数 | 重读字节 | 工具步数 | 补丁 | 交付 | Run |",
              "|---|---|---:|---|---|---:|---:|---:|---|---|---|"]
    cases = {}
    for item in [*report.get("schedule", []), *rows]:
        cases.setdefault(item["case_id"], (item.get("title") or item["case_id"], item["scenario"]))
    recorded = _index(rows)
    for case_id, (title, scenario) in cases.items():
        for variant in VARIANTS:
            cells = [_display(title), "规则" if scenario == "rule" else "事实", str(variant)]
            cells += _trial_text(recorded.get(case_id, {}).get(variant))
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
