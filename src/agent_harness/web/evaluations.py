"""评测报告只读入口，使用既有运行详情回看证据，GET不启动模型。"""

import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from agent_harness.evaluation.engine import read_report, report_path


def trial_artifact_path(root: Path, identifier: UUID, run_id: UUID, kind: Literal["patch", "score", "log", "trajectory"]) -> Path:
    """返回 root 下已登记且指纹一致的补丁或评分原件路径；编号、类型、文件不符时抛 FileNotFoundError。"""
    try:
        report = read_report(root, str(identifier))
        trial = next(row for row in report["trials"] if row["run_id"] == str(run_id))
        directory = report_path(root, str(identifier)).parent
        if kind == "trajectory":
            if not trial.get("trajectory_available") or not trial.get("trajectory_sha256"):
                raise FileNotFoundError()
            path = directory / (str(run_id) + ".trajectory.json")
            expected = trial["trajectory_sha256"]
        elif kind == "patch":
            if not trial.get("patch_available"):
                raise FileNotFoundError()
            path, expected = directory / (str(run_id) + ".patch"), trial.get("patch_sha256")
        else:
            filename = {"score": "official.json", "log": "test_output.txt"}[kind]
            path = directory / "grades" / str(run_id) / filename
            expected = trial["grade_artifacts"][kind]
        if path.resolve() != path or not path.is_file():
            raise FileNotFoundError()
        if expected:
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError("评分原件指纹变化")
        return path
    except (OSError, ValueError, KeyError, StopIteration):
        raise FileNotFoundError("已登记的评测原件不存在或无法核对") from None


def trial_artifact(root: Path, identifier: UUID, run_id: UUID, kind: Literal["patch", "score", "log", "trajectory"]) -> FileResponse:
    """为 identifier/run_id 下载 kind 对应的已核对原件，缺失时返回404。"""
    try:
        path = trial_artifact_path(root, identifier, run_id, kind)
    except FileNotFoundError as error:
        raise HTTPException(404, str(error)) from None
    media_type = "application/json" if kind in {"score", "trajectory"} else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media_type, filename=path.name)


def attach_entry_reviews(directory: Path, report: dict) -> None:
    """在响应 report 上附加 directory 内的宿主入口回执，只有匹配协议、试次和补丁才展示。

    缺少回执是有效状态。损坏、越界或归属不符时抛 ValueError，磁盘原始报告始终保持冻结。
    """
    folder = directory / "entry-reviews"
    if not folder.exists():
        return
    for trial in report["trials"]:
        path = folder / (str(UUID(trial["run_id"])) + ".json")
        if path.resolve() != path:
            raise ValueError("独立入口回执路径不匹配")
        if not path.is_file():
            continue
        fact = json.loads(path.read_bytes())
        if (not isinstance(fact, dict) or fact.get("run_id") != trial["run_id"]
                or fact.get("protocol_sha256") != report["protocol_sha256"]
                or not trial.get("patch_sha256") or fact.get("patch_sha256") != trial["patch_sha256"]
                or fact.get("status") not in {"passed", "failed", "error"} or not isinstance(fact.get("output"), str)):
            raise ValueError("独立入口回执与本次送评补丁不匹配")
        trial["host_entry"] = {"status": fact["status"], "output": fact["output"]}


def install_evaluation_routes(app: FastAPI, manager) -> None:
    """为app安装当前manager数据目录的报告查询；不接受任意文件路径，返回None。"""
    @app.get("/api/evaluations")
    async def evaluations() -> dict:
        """返回最多100份最新报告摘要；损坏记录单独计数，不执行修复。"""
        root = manager.runs_root / ".evaluations"
        if root.resolve() != root or not root.exists():
            return {"items": [], "unreadable": 0}
        reports, unreadable = [], 0
        for directory in sorted(root.iterdir(), key=lambda path: path.name):
            try:
                value = read_report(manager.runs_root, directory.name)
                if value.get("internal"):
                    continue
                reports.append({key: value[key] for key in ("id", "name", "created_at", "state", "mode", "model", "experiment", "summary")})
                reports[-1]["quality_review"] = value.get("quality_review")
            except (OSError, ValueError, KeyError):
                unreadable += 1
        reports.sort(key=lambda item: item["created_at"], reverse=True)
        return {"items": reports[:100], "unreadable": unreadable}

    @app.get("/api/evaluations/{identifier}")
    async def evaluation(identifier: UUID) -> dict:
        """返回identifier冻结报告；缺失返回404，损坏返回503，不从当前库补造历史。"""
        try:
            report = read_report(manager.runs_root, str(identifier))
            attach_entry_reviews(report_path(manager.runs_root, str(identifier)).parent, report)
            campaign_path = manager.runs_root.parent / "campaign.json"
            if report.get("recovery") and campaign_path.is_file():
                campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
                if str(identifier) in {phase["report_id"] for phase in campaign.get("phases", {}).values()}:
                    ledger_path = manager.runs_root.parent / "ledger.json"
                    if ledger_path.is_file():
                        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                        if ledger["protocol_sha256"] != campaign["protocol_sha256"]:
                            raise ValueError("恢复事务尚未完成，账本协议未对齐")
                        report["budget"] = ledger  # 展示权威累计账本，不能沿用暂停前阶段快照的金额。
                    reason = campaign.get("stop_reason")
                    report["recovery"]["status"] = "paused" if reason == "campaign_paused" else "stopped" if reason else "ready"
                    if reason:
                        report["stop_reason"] = reason
            active = report.get("active_trial")
            if active:
                run = manager.runs_root / str(UUID(active["run_id"])) / "run.json"
                active["run_available"] = run.resolve() == run and run.is_file()
            return report
        except FileNotFoundError:
            raise HTTPException(404, "评测报告不存在") from None
        except (ValueError, OSError):
            raise HTTPException(503, "评测报告无法核对") from None

    @app.get("/api/evaluations/{identifier}/patches/{run_id}")
    async def evaluation_patch(identifier: UUID, run_id: UUID):
        """只返回identifier内已记录run_id的补丁文件；拒绝越界、链接及未登记文件，不执行补丁。"""
        return trial_artifact(manager.runs_root, identifier, run_id, "patch")

    @app.get("/api/evaluations/{identifier}/grades/{run_id}/{kind}")
    async def evaluation_grade(identifier: UUID, run_id: UUID, kind: Literal["score", "log"]):
        """读取 identifier 中 run_id 的 score 或 log；未登记原件返回404。"""
        return trial_artifact(manager.runs_root, identifier, run_id, kind)

    @app.get("/api/evaluations/{identifier}/trajectories/{run_id}")
    async def evaluation_trajectory(identifier: UUID, run_id: UUID):
        """读取本报告已登记并核对哈希的原生轨迹；不会启动或重放任何请求。"""
        return trial_artifact(manager.runs_root, identifier, run_id, "trajectory")
