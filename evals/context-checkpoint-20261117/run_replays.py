"""读取冻结题单，逐题执行同结构配对回放并导出可审计的报告。"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from statistics import median

from agent_harness.schemas import ContextPolicy

from freeze import OUTPUT, write_json
from replay import _input_content, replay_case


BANDS = {"low": "低于触发线", "near": "接近上限", "over": "超限"}  # 按压缩前输入分层。
MODES = {"direct": "原文保留", "archived": "归档回读", "missing": "未取回", "preview": "预览保留"}  # 证据结果展示。


def read_json(path: Path) -> dict:
    """读取 path 下的冻结 JSON 记录。缺失、格式错误由调用方直接处理。"""
    return json.loads(path.read_text(encoding="utf-8"))


def display_kib(size: int | None) -> str:
    """用两位小数显示输入 KiB，None 表示该端无法度量。"""
    return f"{size / 1024:.2f}" if size is not None else "—"


def summarize(rows: list[dict]) -> dict:
    """只对完整有效配对计算字节均值和压缩率，并保留所有已提交的原始行。"""
    measured = [row for row in rows if row["status"] == "measured"]
    count = len(measured)
    before = sum(row["raw_input_bytes"] for row in measured)
    after = sum(row["projected_input_bytes"] for row in measured)
    evidence = [entry for row in rows for entry in row["evidence"]]
    return {
        "planned_count": len(rows), "measured_count": count,
        "before_bytes_total": before, "after_bytes_total": after,
        "before_bytes_mean": before / count if count else None, "after_bytes_mean": after / count if count else None,
        "total_reduction_pct": (before - after) * 100 / before if before else None,
        "median_reduction_pct": median(row["reduction_pct"] for row in measured) if count else None,
        "mean_reduction_pct": sum(row["reduction_pct"] for row in measured) / count if count else None,
        "min_reduction_pct": min((row["reduction_pct"] for row in measured), default=None),
        "max_reduction_pct": max((row["reduction_pct"] for row in measured), default=None),
        "over_before": sum(not row["raw_allowed"] for row in measured),
        "over_after": sum(not row["projected_allowed"] for row in measured),
        "evidence_resolved": sum(entry["resolved"] for entry in evidence),
        "evidence_modes": dict(Counter(entry["mode"] for entry in evidence)),
        "archive_checks": sum(len(row["archive_checks"]) for row in rows),
        "archive_matches": sum(check["matched"] for row in rows for check in row["archive_checks"].values()),
        "actions": dict(Counter(action["kind"] for row in rows for action in row["actions"])),
        "summary_attempts": sum(row["summary_attempts"] for row in rows),
        "summary_model_calls": sum(row["summary_model_calls"] for row in rows),
        "http_attempts": sum(row["http_attempts"] for row in rows),
    }


def markdown(rows: list[dict], metadata: dict) -> str:
    """将逐题数据和统计口径渲染为可阅读 Markdown，不改变报告数字。"""
    result = [
        "# AgentHarness 固定检查点压缩回放",
        "",
        "题目为基于 pytest、Click 和 CPython 发行包源码自编的维护需求。前序由脚本执行真实文件读取，日志题包含真实诊断结果。"
        "每题有独立的基线失败与参考解通过记录。",
        "",
        "两臂共用原始检查点和工具声明，关闭压缩与混合压缩分别投影。"
        "体积按模型输入的 messages 和 tools 紧凑 JSON UTF-8 字节计算。硬上限为 256 KiB。",
        "",
        "| 题目 | 压力档 | 压缩前 KiB | 压缩后 KiB | 降幅 | 关键证据 |",
        "|---|---|---:|---:|---:|---|",
    ]
    for row in rows:
        evidence = "、".join(MODES.get(item["mode"], item["mode"]) for item in row["evidence"]) or "未验证"
        rate = f"{row['reduction_pct']:.2f}%" if row["reduction_pct"] is not None else "—"
        result.append(
            f"| [{row['title']}](checkpoints/{row['case_id']}.json) | {BANDS[row['band']]} | "
            f"{display_kib(row['raw_input_bytes'])} | {display_kib(row['projected_input_bytes'])} | {rate} | {evidence} |"
        )
    summary = metadata["overall"]
    result += ["", "## 汇总", "", f"- 完整有效配对 {summary['measured_count']}/{summary['planned_count']}。"]
    if summary["measured_count"]:
        result.append(
            f"- 平均输入 {display_kib(summary['before_bytes_mean'])} → {display_kib(summary['after_bytes_mean'])} KiB。"
            f"总体降幅 {summary['total_reduction_pct']:.2f}%，逐题降幅均值 {summary['mean_reduction_pct']:.2f}%，"
            f"中位数 {summary['median_reduction_pct']:.2f}%。"
        )
        result.append(f"- 超过 256 KiB 输入上限 {summary['over_before']} → {summary['over_after']} 题。")
    result += [
        f"- 关键证据核对通过 {summary['evidence_resolved']}/{summary['planned_count']}，"
        f"原文保留 {summary['evidence_modes'].get('direct', 0)} 题，归档回读 {summary['evidence_modes'].get('archived', 0)} 题。",
        f"- 最终输入引用的归档完整匹配 {summary['archive_matches']}/{summary['archive_checks']} 份。",
        f"- 已提交动作：外置 {summary['actions'].get('offload', 0)}、遮蔽 {summary['actions'].get('mask', 0)}、"
        f"摘要 {summary['actions'].get('summary', 0)}。真实模型请求 {summary['http_attempts']}。",
        "",
        "总体降幅按总字节差除以总压缩前字节数计算。统计范围为本批固定检查点。"
        "超过上限的题数是输入门禁结果，续跑交付需另行评测。",
        "",
        "[逐题原始记录](results.json) · [冻结清单](manifest.json)",
    ]
    return "\n".join(result) + "\n"


async def main() -> None:
    """从冻结清单验证检查点指纹，顺序回放并在新运行目录保存审计证据。"""
    manifest = read_json(OUTPUT / "manifest.json")
    policy = ContextPolicy.model_validate(manifest["policy"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    runs_root = OUTPUT / "runs" / stamp
    runs_root.mkdir(parents=True)
    rows = []
    for spec in manifest["cases"]:
        case = read_json(OUTPUT / "checkpoints" / f"{spec['id']}.json")
        checkpoint = _input_content(case["messages"], case["tools"])
        if sha256(checkpoint).hexdigest() != spec["checkpoint_sha256"] or len(checkpoint) != spec["input_bytes"]:
            raise ValueError(f"冻结检查点的内容或大小已变化：{spec['id']}")
        row = await replay_case(case, runs_root, policy)
        if row["checkpoint_sha256"] != spec["checkpoint_sha256"] or row["raw_input_bytes"] != spec["input_bytes"]:
            raise ValueError(f"回放使用的原始输入与冻结检查点不一致：{spec['id']}")
        row.update(title=case["title"], band=case["band"], source=case["source"])
        rows.append(row)
        print(f"{row['case_id']} {row['status']} {row['raw_input_bytes']} -> {row['projected_input_bytes']}", flush=True)
    bands = {band: summarize([row for row in rows if row["band"] == band]) for band in BANDS}
    metadata = {"overall": summarize(rows), "bands": bands, "policy": manifest["policy"]}
    write_json(OUTPUT / "results.json", {**metadata, "trials": rows})
    (OUTPUT / "report.md").write_text(markdown(rows, metadata), encoding="utf-8")
    if metadata["overall"]["measured_count"] != len(manifest["cases"]):
        raise SystemExit("存在未能完成验证的配对，原因已写入 results.json。")


if __name__ == "__main__":
    asyncio.run(main())
