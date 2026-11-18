"""采纳同一次模型执行的独立补评分；原异常、计费和运行身份不可改写。"""

from copy import deepcopy
import json
from pathlib import Path
from uuid import UUID

from agent_harness.evaluation.protocols import digest, file_hash


def adopt_grading(root: Path, report: dict, identifier: str, protocol_sha256: str,
                  ledger: dict, expected: dict, copies: dict) -> None:
    """核对 identifier 的补评分并更新内存 report；同时登记事务读集 expected 和产物复制表 copies。

    root 是整轮目录，protocol_sha256 为获批的新代码协议，ledger 为原累计账本。
    只接受已完成且用量完整的 artifact_unreadable 试次；有效成败均不能重新评分覆盖。
    原始子报告始终只读；复制和父报告更新由持锁恢复事务统一应用，错误时不写文件。
    """
    folder = root / "grading-recoveries" / str(UUID(identifier))
    receipt_path = folder / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    matches = [row for row in report["trials"] if row["run_id"] == receipt["run_id"]]
    if len(matches) != 1:
        raise ValueError("补评分未对应唯一原始试次")
    row = matches[0]
    if (digest(row) != receipt["original_row_sha256"] or row.get("comparison_valid")
            or row["execution_status"] != "completed" or not row["usage_complete"]
            or not row.get("cost", {}).get("complete")
            or row["grade"] != {"status": "error", "reason": "artifact_unreadable"}
            or receipt["source_report_id"] != row["source_report_id"]
            or receipt["case_id"] != row["case_id"] or receipt["protocol_sha256"] != protocol_sha256
            or receipt.get("external_model_requests") != 0):
        raise ValueError("补评分改变了原模型执行或不属于获批协议")
    requests = [item for item in ledger["requests"] if item["run_id"] == row["run_id"]]
    if (len(requests) != row["model_calls"] or any(item["status"] != "settled" for item in requests)
            or sum(item["known_usd_picos"] for item in requests) != row["cost"]["known_usd_picos"]):
        raise ValueError("补评分原执行用量无法与账本逐条对齐")
    source = root / "runs" / ".evaluations" / str(UUID(row["source_report_id"]))
    if file_hash(source / "report.json") != receipt["source_report_sha256"]:
        raise ValueError("原异常报告改变")
    original = json.loads((source / "report.json").read_text(encoding="utf-8"))
    original_row = next(item for item in original["trials"] if item["run_id"] == row["run_id"])
    if any(original_row.get(key) != value for key, value in row.items() if key != "source_report_id"):
        raise ValueError("父报告和原模型执行事实不一致")
    if original["environments"][row["case_id"]] != receipt["environment"]:
        raise ValueError("补评分镜像、源码或评分条件改变")
    expected[receipt_path.relative_to(root).as_posix()] = file_hash(receipt_path)
    expected[(source / "report.json").relative_to(root).as_posix()] = receipt["source_report_sha256"]
    target = root / "runs" / ".evaluations" / report["id"]
    destinations = {"candidate.patch": target / (row["run_id"] + ".patch"),
                    "official.json": target / "grades" / row["run_id"] / "official.json",
                    "test_output.txt": target / "grades" / row["run_id"] / "test_output.txt"}
    for filename, destination in destinations.items():
        path = folder / filename
        if file_hash(path) != receipt["artifacts"][filename]:
            raise ValueError("补评分原件摘要不一致")
        relative = destination.relative_to(root).as_posix()
        expected[path.relative_to(root).as_posix()] = receipt["artifacts"][filename]
        expected[relative] = file_hash(destination) if destination.exists() else None
        copies[relative] = {"source": path.relative_to(root).as_posix(), "sha256": receipt["artifacts"][filename]}
    grade = receipt["grade"]
    official = json.loads((folder / "official.json").read_text(encoding="utf-8"))
    if (grade.get("status") not in {"passed", "failed"} or grade.get("official") != official
            or not official.get("tests_status") or (grade["status"] == "passed") != bool(official["resolved"])):
        raise ValueError("补评分不是完整的官方终态")
    seconds = receipt["grading_seconds"]
    if not isinstance(seconds, (int, float)) or not 0 < seconds < 1800:
        raise ValueError("补评分耗时无效")
    previous_grade = deepcopy(row["grade"])
    row.update(grade=deepcopy(grade), comparison_valid=True, success=grade["status"] == "passed",
               failure_type=None if grade["status"] == "passed" else "test_assertion",
               patch_available=True, patch_bytes=(folder / "candidate.patch").stat().st_size,
               patch_sha256=receipt["artifacts"]["candidate.patch"],
               grade_artifacts={"score": receipt["artifacts"]["official.json"], "log": receipt["artifacts"]["test_output.txt"]},
               grading_seconds=row["grading_seconds"] + seconds, duration_seconds=row["duration_seconds"] + seconds)
    row["grading_recovery"] = {"receipt_id": identifier, "original_report_id": row["source_report_id"],
        "original_grade": previous_grade, "receipt_sha256": file_hash(receipt_path),
        "external_model_requests": 0, "grading_seconds": seconds}
