"""对已结束但本机评分容器中断的试次补做原协议的离线评分。"""

from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys

from agent_harness.evaluation.campaign_budget import atomic_json


ROOT = Path(__file__).resolve().parents[2]  # AgentHarness 工作区。
OUTPUT = ROOT / "output" / "memory-expanded-20261117"  # 本专项预检和正式批次目录。
GATE = ROOT / "output" / "specialty-expanded-20261117" / "preflight-gate.json"  # 三项原始预检证书。
ORIGINAL = ROOT / "evals" / "memory-repository-20261117"  # 已冻结的 Runner 与离线评分入口所在目录。
sys.path.insert(0, str(ORIGINAL))
from run_repository import memory_run, scored


def read_json(path: Path) -> dict:
    """读取 path 对应的本地 UTF-8 回执并返回其结构化内容。"""
    return json.loads(path.read_bytes())


def workspace_inventory(workspace: Path) -> dict[str, str]:
    """返回原答题目录下各相对文件的字节指纹，遇到链接或目录接合点时停止。"""
    inventory = {}
    for path in sorted(workspace.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise ValueError("候选目录出现非普通路径，无法证明离线复核范围")
        if path.is_file():
            inventory[path.relative_to(workspace).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return inventory


def docker_interrupted(grade: Path, score: dict) -> bool:
    """由原 grade 评分流与退出状态确认本机基础设施异常，返回是否可补评分。"""
    if score.get("error") == "TimeoutExpired" and score.get("exit_code") is None:
        return True
    stderr = grade / "independent.stderr"
    return bool(
        score.get("error") == "grader_process"
        and score.get("exit_code") == 127
        and stderr.is_file()
        and "failed to connect to the docker API" in stderr.read_text(encoding="utf-8", errors="replace")
    )


def validate_terminal(batch: Path, record: dict, receipts: list[dict]) -> dict:
    """校验 record 对应原 Run 已收尾，且 receipts 中的真实请求均结算；返回原始运行摘要。"""
    row = record["outcome"]
    run = batch / row["attempt"] / "runs" / row["run_id"]
    summary = read_json(run / "summary.json")
    comparable = ("run_id", "execution_status", "stop_reason", "usage", "usage_complete",
                  "http_attempts", "cleanup_status", "tool_calls")
    if any(summary[key] != row[key] for key in comparable):
        raise ValueError("终态摘要与原试次回执不一致")
    if not summary["usage_complete"] or summary["cleanup_status"] not in {"complete", "not_needed"}:
        raise ValueError("原试次尚无可接受的用量或清理证据")
    with (run / "events.jsonl").open("rb") as stream:
        terminal = [event["payload"]["result"] for line in stream
                    if (event := json.loads(line))["type"] == "run_finished"]
    if terminal != [summary]:
        raise ValueError("原始事件无法证明同一试次终态")
    matching = [item for item in receipts if item["run_id"] == row["run_id"]]
    if (len(matching) != summary["http_attempts"] or not matching
            or any(item["status"] != "settled" for item in matching)
            or sum(item["known_tokens"] for item in matching) != summary["usage"]["total_tokens"]):
        raise ValueError("原模型 Run 的正式账本仍不完整")
    return summary


def regrade(
    batch: Path, record: dict, case: dict, receipts: list[dict], evidence: Path, expected_seed: str
) -> dict:
    """将 case 的冻结评分脚本用于 record 的原始候选，保存独立 evidence 并返回可入账的试次。

    expected_seed 是该题离线预检时的学习和答题素材指纹。此步只读原候选和 Run，
    写入单独的评分目录。若评分基础设施仍异常，则保留新回执并停止，不接受试次。
    """
    row = dict(record["outcome"])
    terminal = validate_terminal(batch, record, receipts)
    slot = batch / row["attempt"] / "slots" / row["case_id"] / row["arm"]
    seed = read_json(slot / "seed.json")
    if seed["fixture_sha256"] != expected_seed or row["fixture_sha256"] != expected_seed:
        raise ValueError("原试次素材与预检证书不一致")
    grade = slot / "grade"
    previous = read_json(grade / "score.json")
    if row["patch_passed"] is not None or not docker_interrupted(grade, previous):
        raise ValueError("原回执不是已确认的本机评分故障")
    if (grade / "independent.py").read_text(encoding="utf-8") != case["score"]:
        raise ValueError("原评分脚本与冻结题单不一致")
    workspace = slot / "workspace"
    target = (workspace / case["target"]).resolve()
    if not target.is_relative_to(workspace.resolve()):
        raise ValueError("原候选的目标路径超出已验真工作区")
    candidate = target.read_bytes() if target.is_file() else None
    artifact = grade / "candidate.bin"
    if candidate != (artifact.read_bytes() if artifact.is_file() else None):
        raise ValueError("原候选已脱离该试次归档的产物")
    inventory = workspace_inventory(workspace)
    evidence.mkdir()
    atomic_json(evidence / "original.json", {"outcome": row, "score": previous,
                "original_grade": grade.relative_to(batch).as_posix(),
                "workspace_sha256": inventory})
    score = memory_run.grade(workspace, evidence, case["score"], "independent")
    atomic_json(evidence / "score.json", score)
    if score["error"] is not None:
        raise RuntimeError(f"本机补评分失败：{score['error']}")

    misuse = row["misuse"]
    if case.get("misuse") and row["changed"] and misuse is None:
        if (grade / "misuse.py").read_text(encoding="utf-8") != case["misuse"]:
            raise ValueError("原失效探针与冻结题单不一致")
        probe = memory_run.grade(workspace, evidence, case["misuse"], "misuse")
        atomic_json(evidence / "misuse.json", probe)
        if probe["error"] is not None:
            raise RuntimeError(f"本机失效探针补评分失败：{probe['error']}")
        if not probe["exception"]:
            misuse = probe["exit_code"] == 0
    if workspace_inventory(workspace) != inventory:
        raise ValueError("补评分过程中原候选文件发生了变化")
    passed = score["exit_code"] == 0
    row.update(
        patch_passed=passed,
        delivery=bool(passed and row["changed"] and terminal["execution_status"] == "completed"
                      and bool(terminal["final_answer"]) and terminal["cleanup_status"] in {"complete", "not_needed"}),
        misuse=misuse,
        offline_score=evidence.joinpath("score.json").relative_to(batch).as_posix(),
    )
    if not scored(row):
        raise ValueError("补评分回执不满足原协议的可计量条件")
    atomic_json(evidence / "recovered-outcome.json", row)
    return row


def main() -> None:
    """从正式批次指针读取已停止的批次，补评分缺失槽位并原子提交可续跑检查点。"""
    batch = Path(read_json(OUTPUT / "paid-latest.json")["batch"]).resolve(strict=True)
    if not batch.is_relative_to((OUTPUT / "paid").resolve()):
        raise ValueError("正式批次路径不在本专项范围")
    report_path = batch / "batch.json"
    before = report_path.read_bytes()
    report = json.loads(before)
    if (report["state"], report.get("stop_reason"), bool(report["active"])) != (
        "stopped", "evaluation_infrastructure", False
    ):
        raise ValueError("当前批次并非已停止且无在途试次的评分故障")
    budget = report["budget"]
    receipts = budget["requests"]
    if budget["stop_reason"] or any(item["status"] == "reserved" for item in receipts):
        raise ValueError("请求账本仍有在途发单或阻断原因")
    receipt = read_json(GATE)["tracks"]["memory"]
    preflight = ROOT / receipt["report"]
    if (receipt["protocol"] != report["protocol"]
            or sha256(preflight.read_bytes()).hexdigest() != receipt["sha256"]):
        raise ValueError("正在恢复的正式批次和原预检证书不一致")
    registered = read_json(batch / "frozen-suite.json")
    if registered["manifest"]["protocol"] != report["protocol"] or registered["manifest"]["schedule"] != report["schedule"]:
        raise ValueError("正式评分候选和调度协议不一致")
    cases = {case["id"]: case for case in registered["cases"]}
    planned = {(row["round"], row["case_id"], row["arm"]): index
               for index, row in enumerate(report["schedule"])}
    valid = {(row["round"], row["case_id"], row["arm"]) for row in report["trials"]}
    if len(valid) != len(report["trials"]) or not valid <= planned.keys():
        raise ValueError("已有有效试次存在重复或超出冻结调度")

    pending = []
    for attempts in report["attempts"].values():
        record = attempts[-1]
        key = (record["round"], record["case_id"], record["arm"])
        if key not in valid:
            if key not in planned or not record.get("outcome"):
                raise ValueError("仍有无法从原产物安全补评分的槽位")
            pending.append(record)
    if not pending:
        raise ValueError("没有缺失原始独立评分的试次")
    seed_receipts = read_json(preflight)["seeds"]
    recovery = batch / "recoveries" / datetime.now().strftime("offline-grade-%Y%m%d-%H%M%S-%f")
    recovery.mkdir(parents=True)
    (recovery / "batch-before.json").write_bytes(before)
    recovered = []
    for index, record in enumerate(pending, 1):
        case = cases[record["case_id"]]
        row = regrade(batch, record, case, receipts, recovery / str(index), seed_receipts[case["id"]]["fixture_sha256"])
        recovered.append(row)
        record["offline_recovered_outcome"] = row
        print(f"{row['round']}/{row['case_id']}/{row['arm']} 原 Run {row['run_id']} 补评分完成", flush=True)
    if report_path.read_bytes() != before:
        raise ValueError("补评分过程中正式检查点已被其他进程更新")
    report["trials"].extend(recovered)
    report["trials"].sort(key=lambda row: planned[row["round"], row["case_id"], row["arm"]])
    report["recoveries"].append({
        "kind": "offline_grade",
        "evidence": recovery.relative_to(batch).as_posix(),
        "original_stop": report["stop_reason"],
        "recovered_run_ids": [row["run_id"] for row in recovered],
        "external_model_requests": 0,
        "model_request_count_before_and_after": budget["calls_admitted"],
    })
    report.update(state="preparing", stop_reason=None)
    atomic_json(report_path, report)
    print(f"已保留原 {len(valid)} 条有效回执并补齐 {len(recovered)} 条原试次评分，"
          f"账本 {budget['calls_admitted']} 笔请求保持原样", flush=True)


if __name__ == "__main__":
    main()
