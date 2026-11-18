"""可信本机评测执行器：冻结输入、隔离试次、外部评分、逐条落盘，不自动重放中断。"""

import asyncio
import hashlib
import json
import os
import platform
from importlib.metadata import version
from pathlib import Path
import random
from itertools import permutations
from agent_harness.evaluation.reproduction import GUIDE, RepositoryDelivery
from types import SimpleNamespace
import sys
import time
from datetime import datetime, UTC
from uuid import uuid4, UUID

from agent_harness.config import prepare_task
from agent_harness.evaluation.budget import BatchBudget, validate_billing_config
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.execution.processes import ProcessRunner
from agent_harness.runtime.runner import run_task, validate_execution
from agent_harness.schemas import RunConfig, RunEvent, RunManifest
from agent_harness.storage.runs import RunStore
from agent_harness.evaluation.models import Suite, Experiment, QualityReview
from agent_harness.evaluation.report import summarize
from agent_harness.evaluation.repository import RepositoryEnvironment, RepositoryExecutor


def trial_schedule(case_ids: list[str], experiment: Experiment, repository: bool) -> list[dict]:
    """为 case_ids 分配 Run ID。仓库双组使用 experiment 的种子平衡先后；返回实际执行顺序。"""
    rng = random.Random(experiment.seed)
    slots = [(case_id, repeat) for case_id in case_ids for repeat in range(experiment.repeat_offset, experiment.repeat_offset + experiment.repeats)]
    if repository and len(experiment.groups) == 3:
        orders = list(permutations(experiment.group_ids))
        rng.shuffle(orders)
        return [{"case_id": case_id, "strategy": strategy, "repeat": repeat, "run_id": str(uuid4())}
                for index, (case_id, repeat) in enumerate(slots)
                for strategy in orders[index % len(orders)]]
    if repository and len(experiment.group_ids) == 2:
        rng.shuffle(slots)
        return [{"case_id": case_id, "strategy": strategy, "repeat": repeat, "run_id": str(uuid4())}
                for index, (case_id, repeat) in enumerate(slots)
                for strategy in experiment.group_ids[::1 if index % 2 == 0 else -1]]
    schedule = [{"case_id": case_id, "strategy": strategy, "repeat": repeat, "run_id": str(uuid4())}
                for case_id, repeat in slots for strategy in experiment.group_ids]
    rng.shuffle(schedule)
    return schedule


def context_profile(strategy: str) -> dict:
    """把已校验的评测 strategy 映射为冻结的压缩、预算投影与末轮行为。"""
    if strategy == "native":
        return {"compression": "off", "root_handoff": False, "budget_projection": False}
    if strategy == "window":
        return {"compression": "window", "root_handoff": True, "budget_projection": False,
                "keep_recent_groups": 5, "max_summary_calls": 0, "root_handoff_after_input_rejection": False}
    budgeted = strategy in {"hybrid_budgeted", "hybrid_budgeted_verified_handoff"}
    handoff = budgeted or strategy == "hybrid_handoff"
    return {
        "compression": "hybrid" if handoff else strategy,
        "root_handoff": handoff,
        "budget_projection": budgeted,
        "root_handoff_after_input_rejection": strategy == "hybrid_budgeted_verified_handoff",
    }


def trial_tool_evidence(events: list[RunEvent]) -> dict:
    """从 events 配对搜索开始和回执，返回墙钟用时、超时数与首条成功写入回执。

    时间是 Run 事件时钟上的毫秒。记录缺失时不猜测启动或写入，候选补丁另由外部采集。
    """
    searches = {"started": 0, "total_ms": 0, "local_timeouts": 0, "run_timeouts": 0}
    started = {}
    first_write = None
    turn = None
    for event in events:
        payload = event.payload
        name = payload.get("name")
        if event.type == "assistant_message":
            turn = payload.get("model_turn")
        if name == "search_files" and event.type == "tool_started":
            started[payload["call_id"]] = event.elapsed_ms
            searches["started"] += 1
        if event.type != "tool_result":
            continue
        receipt = payload["result"]
        if name == "search_files":
            start = started.pop(payload["call_id"], None)
            if start is not None:
                searches["total_ms"] += max(0, event.elapsed_ms - start)
            error = (receipt.get("error") or {}).get("code")
            searches["local_timeouts"] += int(error == "search_timeout")
            searches["run_timeouts"] += int(error == "tool_timed_out")
        if first_write is None and name in {"edit_file", "write_file"} and receipt.get("ok") is True:
            data = receipt.get("data") or {}
            if isinstance(data.get("path"), str) and type(data.get("bytes_written")) is int:
                first_write = {"path": data["path"], "bytes_written": data["bytes_written"],
                               "model_turn": turn, "seq": event.seq, "elapsed_ms": event.elapsed_ms,
                               "tool": name, "call_id": payload["call_id"]}
    return {"search": searches, "first_write": first_write}


def trial_input_evidence(events: list[RunEvent]) -> dict:
    """从 events 返回输入检查、已发请求和收尾回执；同轮的普通拒绝和收尾分开关联。

    仅真实接纳的执行请求更新最后发送字节数；待发送、被拒或未配对的记录保留其原有含义。
    """
    checks = {}
    newest_by_turn = {}
    last_admitted = last_rejected = handoff_input = handoff_checked = None
    peak = execution_calls = summary_calls = 0
    handoff_status, handoff_trigger, handoff_skip_reason = "not_triggered", None, None
    for event in events:
        payload = event.payload
        if event.type == "context_checked":
            peak = max(peak, payload["input_bytes"])
            if payload["purpose"] == "execution":
                checks[event.seq] = payload
                newest_by_turn[payload["model_turn"]] = payload
                if payload.get("phase") == "handoff":
                    handoff_checked = payload["input_bytes"]
                elif not payload["allowed"]:
                    last_rejected = payload["input_bytes"]
        elif event.type == "model_request_started" and payload["purpose"] == "execution":
            execution_calls += 1
            seq = payload.get("input_check_seq")
            check = checks.get(seq) if seq is not None else newest_by_turn.get(payload["model_turn"])
            if check and check["allowed"] and check["model_turn"] == payload["model_turn"]:
                last_admitted = check["input_bytes"]
                if check.get("phase") == "handoff":
                    handoff_input = last_admitted
        elif event.type == "context_compaction_started":
            summary_calls += 1
        elif event.type == "budget_handoff_started" and payload.get("mode") == "root":
            handoff_status = "started"
            handoff_trigger = payload.get("trigger")
        elif event.type == "budget_handoff_finished" and payload.get("mode") == "root":
            handoff_status = payload["status"]
        elif event.type == "budget_handoff_skipped":
            handoff_trigger = payload.get("trigger")
            handoff_skip_reason = payload.get("reason")
    return {
        "input_bytes_peak": peak, "summary_calls": summary_calls, "execution_calls": execution_calls,
        "last_admitted_input_bytes": last_admitted, "last_rejected_input_bytes": last_rejected,
        "handoff_status": handoff_status, "handoff_trigger": handoff_trigger, "handoff_input_bytes": handoff_input,
        "handoff_checked_bytes": handoff_checked, "handoff_skip_reason": handoff_skip_reason,
    }


def trial_timing_evidence(events: list[RunEvent]) -> dict:
    """从 events 配对请求与工具起止；返回秒数与请求延迟，缺失终点不估造耗时。"""
    models, commands, latencies, tool_seconds = {}, {}, [], 0.0
    for event in events:
        payload = event.payload
        if event.type == "model_request_started":
            models[payload["model_turn"]] = event.elapsed_ms
        elif event.type == "model_response":
            start = models.pop(payload["model_turn"], None)
            if start is not None:
                latencies.append(max(0, event.elapsed_ms - start) / 1000)
        elif event.type == "tool_started":
            commands[payload["call_id"]] = event.elapsed_ms
        elif event.type == "tool_result":
            start = commands.pop(payload["call_id"], None)
            if start is not None:
                tool_seconds += max(0, event.elapsed_ms - start) / 1000
    return {"model_seconds": sum(latencies), "tool_seconds": tool_seconds, "model_latencies": latencies,
            "timing_complete": not models and not commands}


def fingerprint(value: object) -> str:
    """返回规范JSON对象value的SHA256；不读取凭据或外部状态。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def files_at(directory: Path) -> dict[str, str]:
    """读取directory内UTF-8任务文件，返回相对路径到正文；拒绝链接、特殊文件及超过8MiB的输入。"""
    if directory.is_symlink() or directory.is_junction():
        raise ValueError("评测目录禁止链接")
    result = {}
    size = 0
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise ValueError("评测副本禁止链接")
        if path.is_dir():
            continue
        if any(part in {".git", "__pycache__", ".pytest_cache"} for part in path.relative_to(directory).parts):
            continue
        if not path.is_file():
            raise ValueError("评测资源必须为普通文件")
        size += path.stat().st_size
        if size > 8 * 1024 * 1024:
            raise ValueError("单项评测源码超过8MiB")
        result[path.relative_to(directory).as_posix()] = path.read_text(encoding="utf-8")
    if not result:
        raise ValueError("评测源码不能为空")
    return result


def materialize(directory: Path, files: dict[str, str]) -> None:
    """在尚不存在的directory中排他写入可信files；不删除或复用上次试次。"""
    directory.mkdir(parents=True, exist_ok=False)
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def save_report(directory: Path, report: dict) -> None:
    """原子写入report及派生统计；失败不覆盖上一份有效检查点。"""
    report["summary"] = summarize(report)
    atomic_json(directory / "report.json", report)


def report_path(root: Path, identifier: str) -> Path:
    """返回root内合法UUID报告路径；拒绝目录替换和链接，不创建数据。"""
    path = root.resolve() / ".evaluations" / str(UUID(identifier)) / "report.json"
    if path.resolve() != path or path.parent.is_junction():
        raise ValueError("评测记录路径发生变化")
    return path


def read_report(root: Path, identifier: str) -> dict:
    """只读返回identifier报告；缺失/损坏抛异常，绝不恢复执行。"""
    path = report_path(root, identifier)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("评测报告超限")
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("id") != identifier or not isinstance(report.get("summary"), dict):
        raise ValueError("评测报告结构无效")
    review_path = path.with_name("review.json")
    if review_path.exists():
        if review_path.resolve() != review_path or review_path.stat().st_size > 32768:
            raise ValueError("评测复核文件无效")
        review = QualityReview.model_validate_json(review_path.read_bytes())
        if review.protocol_sha256 != report["protocol_sha256"] or not set(review.case_ids).issubset({case["id"] for case in report["cases"]}):
            raise ValueError("评测复核不属于此协议")
        report["quality_review"] = review.model_dump()
    return report


def grading_artifacts(evidence: Path) -> dict[str, str]:
    """核对 evidence 内存在的评分原件，返回下载类型到文件 SHA256 的映射。"""
    references = {}
    for kind, name in {"score": "official.json", "log": "test_output.txt"}.items():
        path = evidence / name
        if path.is_file():
            with path.open("rb") as stream:
                references[kind] = hashlib.file_digest(stream, "sha256").hexdigest()
    return references


async def grade(files: dict[str, str], script: str, work: Path, evidence: Path, config: RunConfig, seconds: float) -> dict:
    """在独立work副本运行可信script，返回评分与日志；seconds为超时秒数，配置仅提供进程限额。"""
    materialize(work, files)
    evidence.mkdir(parents=True, exist_ok=False)
    grader = evidence / "grader.py"
    grader.write_text(script, encoding="utf-8")
    result = await ProcessRunner(config.limits, evidence / "artifacts").run(
        [sys.executable, "-I", str(grader), str(work)], work, timeout_seconds=seconds)
    healthy = not any(result.get(key) for key in ("timed_out", "cancelled", "output_limit_exceeded"))
    healthy = healthy and result.get("cleanup_status") in {"complete", "not_needed"}
    healthy = healthy and result.get("error_code") in {None, "command_nonzero_exit"}
    status = "passed" if healthy and result.get("exit_code") == 0 else "failed" if healthy and result.get("exit_code") == 1 else "error"
    return {"status": status, "exit_code": result.get("exit_code"), "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""), "reason": result.get("error_code"), "timed_out": result.get("timed_out", False)}


async def evaluate(suite_path: Path, config: RunConfig, experiment: Experiment, runs_root: Path, work_root: Path,
                   api_key: str, *, client_factory=None, transport=None, preflight_only: bool = False,
                   swe_python: Path | None = None, required_preflight: str | None = None,
                   campaign_budget=None, campaign_phase: str | None = None, frozen_schedule: list[dict] | None = None,
                   progress=None, completed_schedule: list[dict] | None = None,
                   preflight_certificate_sha256: str | None = None, grader_http_cache: Path | None = None) -> dict:
    """冻结并执行有界对照，返回报告并逐次保存；api_key只交模型，None客户端工厂使用真实适配器。

    runs_root保存普通Run及报告；work_root必须在其外，专属副本不自动删除。
    preflight_only只校验基线/参考解，不调用模型。取消保留报告并传播，不自动续跑。
    swe_python是安装固定官方评分器的解释器；仓库题必须提供，纯本地题可为None。
    transport为测试注入的HTTP传输，None使用真实网络。required_preflight绑定同目录的零模型预检批次。
    campaign_budget为跨阶段权威账本，campaign_phase指定批准阶段；二者为None时使用单批协议。
    frozen_schedule可传入整轮预分配的本题试次；只接受与题集和组定义严格匹配的槽位。
    completed_schedule 为已终结有效槽位；与 frozen_schedule 合起来必须恰好覆盖原计划，防止重复执行。
    preflight_certificate_sha256 绑定持久检查点的证书摘要；None 用于独立批次的新证书。
    progress为可选同步只读进度回调；每次报告保存后接收当前report，不得发起模型请求。
    grader_http_cache 可指定获批的公开 HTTPS 响应目录，仅挂入独立评分器，None 走原无网环境。
    """
    suite_path = suite_path.resolve(strict=True)
    suite = Suite.model_validate_json(suite_path.read_bytes())
    if suite.kind != experiment.kind:
        raise ValueError("题集类型与评测协议不一致")
    selected = [case for case in suite.cases if case.split == experiment.split]
    if not selected:
        raise ValueError("所选划分没有任务")
    repository_cases = any(case.backend == "swebench" for case in selected)
    public_self_test = any(group.context_policy == "hybrid_budgeted_verified_handoff" for group in experiment.groups)
    if public_self_test and any(case.backend != "swebench" for case in selected):
        raise ValueError("经自测的预算收尾需要仓库题的公开自测环境")
    if repository_cases and (swe_python is None or not swe_python.is_file()):
        raise ValueError("仓库题需提供安装swebench==4.1.0的--swe-python")
    if repository_cases and not set(config.tools).issubset({"read_file", "search_files", "write_file", "edit_file", "run_command"}):
        raise ValueError("仓库实验仅允许固定文件工具与前台命令")
    runs_root, work_root = runs_root.resolve(), work_root.resolve()
    if work_root.is_relative_to(runs_root) or runs_root.is_relative_to(work_root):
        raise ValueError("试次目录与运行记录目录必须互不包含")
    # 两类实验都关闭基础配置的记忆读写，只有记忆试次的固定投影可以开放读能力。
    if config.memory.use or config.memory.capture or config.skills or config.skill_auto or config.mcp_tools or config.auto_repair:
        raise ValueError("评测基础配置须关闭记忆、技能、MCP及自动修复，试次按协议投影")
    if experiment.kind == "memory" and (config.limits.max_model_turns > 6 or config.limits.max_http_attempts_per_turn != 1):
        raise ValueError("首轮记忆对照每试次至多6次模型调用，每轮只允许一次HTTP尝试")
    config = config.freeze_models()
    if experiment.max_cost_cents is not None:
        validate_billing_config(config)
        if repository_cases and not (preflight_only or required_preflight or client_factory or transport):
            raise ValueError("费用受控仓库评测须绑定先前通过的零模型预检")
    batch = str(uuid4())
    directory = runs_root / ".evaluations" / batch
    if directory.resolve() != directory:
        raise ValueError("评测记录目录不能使用链接")
    directory.mkdir(parents=True)
    frozen = []
    for case in selected:
        paths = [suite_path.parent / name for name in (case.source, case.reference, case.grader) if name is not None]
        if any(path.resolve() != path or not path.resolve().is_relative_to(suite_path.parent) for path in paths):
            raise ValueError("题集资源越界或包含链接")
        if case.backend == "swebench":
            record = json.loads(paths[0].read_text(encoding="utf-8"))
            if record["instance_id"] != case.id or record["problem_statement"] != case.prompt:
                raise ValueError("题号及原始issue必须与固定记录一致")
            frozen.append({**case.model_dump(), "record": record})
        else:
            frozen.append({**case.model_dump(), "files": files_at(paths[0]), "solution": files_at(paths[1]), "script": paths[2].read_text(encoding="utf-8")})
    source_root = Path(__file__).resolve().parents[1]
    runtime_hashes = {path.relative_to(source_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_root.rglob("*.py")}
    profiles = {group.id: context_profile(group.context_policy) for group in experiment.groups} if experiment.kind == "context" else {}
    protocol = {"suite": suite.model_dump(), "frozen": frozen, "config": config.model_dump(mode="json"),
                "context_profiles": profiles,
                "experiment": experiment.model_dump(), "runtime_hashes": runtime_hashes,
                "environment": {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
                                "packages": {name: version(name) for name in ("pydantic", "httpx", "fastapi")}}}
    schedule = trial_schedule([case.id for case in selected], experiment, repository_cases)
    if frozen_schedule is not None:
        expected = sorted((row["case_id"], row["strategy"], row["repeat"]) for row in schedule)
        actual = sorted((row["case_id"], row["strategy"], row["repeat"]) for row in [*frozen_schedule, *(completed_schedule or [])])
        if actual != expected or len({row["run_id"] for row in frozen_schedule}) != len(frozen_schedule):
            raise ValueError("整轮试次与当前题集或分组不匹配")
        schedule = frozen_schedule
    protocol["schedule"] = schedule
    protocol_path = directory / "protocol.json"
    report = {"id": batch, "name": suite.name, "suite_version": suite.version, "provenance": suite.provenance,
              "protocol_sha256": fingerprint(protocol), "created_at": datetime.now(UTC).isoformat(),
              "mode": "preflight" if preflight_only else "scripted" if client_factory or transport else "real_model",
              "state": "incomplete", "stop_reason": None, "experiment": experiment.model_dump(),
              "model": config.requested_model, "cases": [case.model_dump() for case in selected],
              "context_profiles": profiles,
              "schedule": schedule, "trials": [], "preflight": [], "active_trial": None, "environments": {}}
    def checkpoint() -> None:
        """保存本题检查点，再更新整轮只读进度；回调失败使调度停止。"""
        save_report(directory, report)
        if progress is not None:
            progress(report)

    if experiment.kind == "memory":
        report.update(consolidations=[], active_consolidation=None)
    budget = None
    if experiment.max_cost_cents is not None:
        if campaign_budget is not None and not preflight_only:
            budget = campaign_budget
            budget.bind(campaign_phase, report, lambda: checkpoint())
        else:
            budget = BatchBudget(experiment, report, lambda: checkpoint())
        protocol["billing"] = {key: value for key, value in budget.state.items()
                               if key in {"limit_usd_picos", "reservation_usd_picos", "pricing_url", "pricing_model", "peak_usd_picos_per_token"}}
    if required_preflight is not None:
        protocol["required_preflight"] = required_preflight
    protocol_path.write_text(json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8")
    report["protocol_sha256"] = fingerprint(protocol)
    checkpoint()
    started = 0.0  # 活跃试次单调时钟；异常回执计入已消耗的等待时间。
    environments = {}  # 题号到隔离环境；宿主持有参考记录，不传入模型上下文。
    memory_projects = {}  # 记忆实验每项目共享一次整理，其余试次重放独立副本。
    consolidation_calls, consolidation_tokens = 0, 0
    try:
        previous = None
        if required_preflight is not None:
            previous = read_report(runs_root, required_preflight)
            previous_path = report_path(runs_root, required_preflight).with_name("protocol.json")
            previous_protocol = json.loads(previous_path.read_text(encoding="utf-8"))
            from agent_harness.evaluation.preflight import verify_certificate
            certificate_path = previous_path.with_name("preflight-certificate.json")
            try:
                certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
                if (preflight_only or not repository_cases
                        or preflight_certificate_sha256 is not None and fingerprint(certificate) != preflight_certificate_sha256
                        or previous_protocol["environment"] != protocol["environment"]):
                    raise ValueError("预检证书或宿主环境不匹配")
                previous = verify_certificate(runs_root, certificate, frozen, protocol["config"],
                                              public_self_test=public_self_test)
            except (OSError, ValueError, KeyError) as error:
                report.update(state="stopped", stop_reason="preflight_mismatch")
                report["preflight_error"] = {"case_id": frozen[0]["id"], "reason": str(error)}
                return report
            report["preflight_reuse"] = {"status": "reused", "report_id": required_preflight,
                                        "certificate_sha256": fingerprint(certificate),
                                        "adopted": bool(certificate.get("adoption"))}
            report["preflight_source_id"] = required_preflight
        for case in frozen:
            outcomes = {}
            if case["backend"] == "swebench":
                environment_options = {"public_self_test": True} if public_self_test else {}
                env_type = RepositoryEnvironment
                if grader_http_cache is not None:
                    from agent_harness.evaluation.grading_http import CachedGradingEnvironment
                    env_type = CachedGradingEnvironment
                    environment_options["http_cache"] = grader_http_cache
                environment = env_type(case["record"], directory / "environments" / case["id"], config,
                                       swe_python, **environment_options)
                environments[case["id"]] = environment
                try:
                    expected_environment = previous["environments"][case["id"]] if previous else None
                    report["environments"][case["id"]] = await environment.prepare(expected_environment)
                except (ValueError, RuntimeError, OSError) as error:
                    report.update(state="stopped", stop_reason="preflight_environment")
                    report["preflight_error"] = {"case_id": case["id"], "reason": str(error)}
                    return report
                # 执行前把真实镜像ID纳入协议，标签后续变动不能改变本批已固定的环境。
                protocol["repository_environments"] = report["environments"]
                protocol_path.write_text(json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8")
                report["protocol_sha256"] = fingerprint(protocol)
                checkpoint()
                try:
                    from agent_harness.evaluation.preflight import verify_repository
                    previous_outcome = next((item for item in previous["preflight"] if item["case_id"] == case["id"]), None) if previous else None
                    outcomes = await verify_repository(environment, directory / "preflight" / case["id"], work_root,
                                                       previous=previous_outcome)
                except (ValueError, RuntimeError, OSError) as error:
                    report.update(state="stopped", stop_reason="preflight_environment")
                    report["preflight_error"] = {"case_id": case["id"], "reason": str(error)}
                    return report
            else:
                for variant, files in (("baseline", case["files"]), ("reference", case["solution"])):
                    outcomes[variant] = await grade(files, case["script"], work_root / batch / (case["id"] + "-" + variant),
                                                    directory / "preflight" / case["id"] / variant, config, experiment.grader_timeout_seconds)
                outcomes["baseline_valid"] = outcomes["baseline"]["status"] == "failed"
            report["preflight"].append({"case_id": case["id"], **outcomes})
            checkpoint()
            if outcomes["reference"]["status"] != "passed" or not outcomes["baseline_valid"]:
                report.update(state="stopped", stop_reason="invalid_case")
                return report
        if preflight_only:
            report.update(state="preflight_passed")
            return report
        if experiment.kind == "memory":
            from agent_harness.evaluation.memory_cases import MemoryTrialEnvironment

            for project in sorted({case.project for case in selected}):
                if consolidation_calls + min(experiment.max_consolidation_calls, config.limits.max_model_turns) > experiment.max_model_calls:
                    report.update(state="stopped", stop_reason="batch_model_limit")
                    return report
                if consolidation_tokens >= experiment.max_known_tokens:
                    report.update(state="stopped", stop_reason="batch_known_token_threshold")
                    return report
                fixture = suite.learning[project + ":" + experiment.split]
                environment = MemoryTrialEnvironment(fixture, project, runs_root, work_root / batch / ("learn-" + project), config)
                service = await environment.seed()

                def consolidation_progress(result: dict) -> None:
                    """保存正在整理的用量检查点；退出或中断仍可定位已启动的请求。"""
                    report["active_consolidation"] = {"project": project, **result}
                    checkpoint()

                result = await environment.organize(service, api_key, max_calls=experiment.max_consolidation_calls,
                                                    client=client_factory() if client_factory else None, progress=consolidation_progress)
                report["consolidations"].append(result)
                report["active_consolidation"] = None
                memory_projects[project] = environment
                consolidation_calls += result["model_calls"]
                consolidation_tokens += result["usage"].get("total_tokens", 0)
                checkpoint()
                if not result["usage_complete"]:
                    report.update(state="stopped", stop_reason="unknown_usage")
                    return report
                if result["state"] == "cancelled":
                    report.update(state="stopped", stop_reason="cancelled")
                    return report
                if result["state"] == "failed" or result.get("review_error"):
                    report.update(state="stopped", stop_reason="memory_consolidation_failed")
                    return report  # 没有有效草案时不把 C 组降级成纯检索继续收费。
        by_id = {case["id"]: case for case in frozen}
        for planned in schedule:
            blocked = budget.blocking_reason() if budget is not None else None
            if blocked:
                report.update(state="stopped", stop_reason=blocked)
                break
            if budget is None and consolidation_calls + sum(row["model_calls"] for row in report["trials"]) + config.limits.max_model_turns > experiment.max_model_calls:
                report.update(state="stopped", stop_reason="batch_model_limit")
                break
            if consolidation_tokens + sum(row["known_tokens"] for row in report["trials"]) >= experiment.max_known_tokens:
                report.update(state="stopped", stop_reason="batch_known_token_threshold")
                break
            if any(not row["usage_complete"] for row in report["trials"]):
                report.update(state="stopped", stop_reason="unknown_usage")
                break
            case = by_id[planned["case_id"]]
            started = time.monotonic()
            # 先登记再启动；即使进程被强制终止，也能定位最后一个未完成试次。
            report["active_trial"] = {**planned, "project": case["project"], "category": case["category"],
                "success": False, "execution_status": "running", "stop_reason": None,
                "grade": {"status": "not_scored"}, "needs_input": False, "duration_seconds": 0,
                "model_calls": 0, "http_attempts": 0, "tool_calls": 0, "known_tokens": 0,
                "usage_complete": False, "input_bytes_peak": 0, "summary_calls": 0, "response_model": None}
            checkpoint()
            environment = environments.get(case["id"])
            # 真实仓库含长测试路径；UUID已全局唯一，去掉重复批次层级以避免Windows路径上限。
            workspace = work_root / planned["run_id"] if environment else work_root / batch / planned["run_id"]
            if environment:
                await environment.workspace(workspace)
                await environment.start_session(workspace)
                await environment.verify_workspace(workspace)
            else:
                materialize(workspace, case["files"])
            preparation_seconds = time.monotonic() - started
            group = next(group for group in experiment.groups if group.id == planned["strategy"])
            trial_config = config.model_copy(deep=True)
            aliases = {}
            if experiment.kind == "memory":
                trial_config, aliases = memory_projects[case["project"]].bind(workspace, next(group.context_policy for group in experiment.groups if group.id == planned["strategy"]))
            else:
                trial_config.context = trial_config.context.model_copy(update=profiles[planned["strategy"]])
            verify_entry = (environment is not None and group.executor == "agent_harness"
                            and "run_command" in trial_config.tools and not public_self_test)
            if verify_entry:
                trial_config.system_prompt = (trial_config.system_prompt or "") + "\n" + GUIDE
            task = prepare_task(case["prompt"], workspace, python_executable=sys.executable)
            manifest = RunManifest(run_id=UUID(planned["run_id"]), task=task, config=trial_config)
            validate_execution(manifest, api_key)
            if experiment.kind == "memory":
                from agent_harness.evaluation.memory_cases import MemoryTrialExecutor

                executor = MemoryTrialExecutor(workspace, runs_root, trial_config)
            else:
                executor = RepositoryExecutor(environment, workspace, runs_root, trial_config) if environment else None
                if verify_entry:
                    executor.delivery = RepositoryDelivery(environment, workspace, directory / "entries" / planned["run_id"],
                                                           case["prompt"])
            agent_started = time.monotonic()
            native_evidence = {}
            if group.executor == "mini_swe_agent":
                if environment is None or budget is None:
                    raise ValueError("外部基线需要固定仓库环境与请求账本")
                from agent_harness.evaluation.mini import run_mini
                result, native_evidence = await run_mini(trial_config, environment, workspace, planned["run_id"],
                    case["prompt"], budget, api_key, directory / (planned["run_id"] + ".trajectory.json"), transport=transport)
            else:
                outcome = await run_task(manifest, runs_root, api_key, model_client=client_factory() if client_factory else None,
                                         executor=executor, transport=transport, model_request_guard=budget)
                result = outcome.result
            agent_seconds = time.monotonic() - agent_started
            trial_cost = budget.run_cost(planned["run_id"]) if budget is not None else None
            report["active_trial"].update(execution_status=result.execution_status, model_calls=result.model_turns,
                http_attempts=result.http_attempts, tool_calls=result.tool_calls, known_tokens=result.usage.total_tokens,
                usage_complete=result.usage_complete, response_model=result.response_model, cost=trial_cost, run_available=group.executor == "agent_harness")
            checkpoint()
            grading_started = time.monotonic()
            patch = None  # 本地夹具没有Git补丁，仓库题保存宿主采集的完整差异。
            grade_directory = directory / "grades" / planned["run_id"]
            cleanup_confirmed = result.cleanup_status in {"complete", "not_needed"}
            try:
                if not cleanup_confirmed or result.execution_status == "cancelled":
                    grading = {"status": "not_scored", "reason": result.stop_reason}
                elif environment:
                    patch_path = directory / (planned["run_id"] + ".patch")
                    selected = executor.delivery.selected if verify_entry else ()
                    patch = await environment.patch(workspace, patch_path, selected) if selected else await environment.patch(workspace, patch_path)
                    grading = await environment.grade(patch, grade_directory)
                else:
                    grading = await grade(files_at(workspace), case["script"], work_root / batch / (planned["run_id"] + "-grade"),
                                          directory / "grades" / planned["run_id"], config, experiment.grader_timeout_seconds)
            except (OSError, ValueError, UnicodeError):
                grading = {"status": "error", "reason": "artifact_unreadable"}
            except RuntimeError:
                grading = {"status": "error", "reason": "cleanup_unconfirmed"}
            history = RunStore.read(runs_root, planned["run_id"]) if group.executor == "agent_harness" else SimpleNamespace(events=[])
            context_actions = [event.payload.get("kind") for event in history.events if event.type == "context_action"]
            self_test_events = [event for event in history.events if event.type == "public_self_test_finished"] if public_self_test else []
            current_test = None
            if self_test_events:
                recent_test = self_test_events[-1].payload
                # Run 已结束，报告只复核候选，保留缺少候选指纹的原始失败原因。
                current_test = (executor.public_self_test_facts(emit_invalidation=False)
                                if recent_test.get("candidate_sha256") else recent_test)
            self_test_environment_failed = any(event.payload.get("reason") == "self_test_environment" for event in self_test_events)
            row = {**planned, "project": case["project"], "category": case["category"],
                   "success": result.execution_status == "completed" and grading["status"] == "passed" and cleanup_confirmed,
                   "execution_status": result.execution_status, "stop_reason": result.stop_reason, "grade": grading,
                   "needs_input": result.execution_status == "waiting_input", "duration_seconds": time.monotonic() - started,
                   "model_calls": result.model_turns, "http_attempts": result.http_attempts, "tool_calls": result.tool_calls,
                   "known_tokens": result.usage.total_tokens, "usage_complete": result.usage_complete, "cost": trial_cost, "run_available": group.executor == "agent_harness",
                   "executor": group.executor, "context_policy": group.context_policy,
                   "preparation_seconds": preparation_seconds, "agent_seconds": agent_seconds,
                   "grading_seconds": time.monotonic() - grading_started,
                   "response_model": result.response_model,
                   "patch_available": patch is not None,
                   "patch_bytes": len(patch.encode("utf-8")) if patch is not None else None,
                   **trial_tool_evidence(history.events),
                   **trial_timing_evidence(history.events),
                   "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest() if patch is not None else None,
                   "grade_artifacts": grading_artifacts(grade_directory) if environment else {},
                   "source_url": f'https://github.com/{case["record"]["repo"]}/pull/{case["id"].rsplit("-", 1)[1]}' if environment else None,
                   "failure_type": "grading_environment" if grading["status"] == "error" else
                       "patch_apply" if grading.get("reason") == "patch_apply_failed" else
                       "execution" if result.execution_status != "completed" else
                       "test_assertion" if grading["status"] == "failed" else None,
                   **trial_input_evidence(history.events),
                   "context_actions": context_actions,
                   "budget_projection_actions": sum(event.type == "context_action" and event.payload.get("reason") == "input_budget"
                                                    for event in history.events),
                   "compression_applied": any(action in {"offload", "mask", "summary"} for action in context_actions),
                   **native_evidence}
            if verify_entry:
                row["entry"] = executor.delivery.final(patch) if patch is not None else {"status": "unverified"}
                row["success"] = row["success"] and row["entry"]["status"] == "verified"
            if public_self_test:
                row["public_self_test"] = current_test or {"status": "not_run", "reason": "candidate_changed" if self_test_events else None}
            if environment:
                row["comparison_valid"] = (cleanup_confirmed and not self_test_environment_failed
                    and grading["status"] in {"passed", "failed"}
                    and result.usage_complete and (budget is None or trial_cost["complete"] and budget.stop_reason is None))
            if experiment.kind == "memory":
                from agent_harness.evaluation.memory_cases import billable_usage, memory_trace

                row.update(memory=memory_trace(history, aliases, case["required_evidence"], case["forbidden_evidence"]),
                           usage=result.usage.model_dump(), model_usage=billable_usage(history),
                           started_at=history.manifest.created_at.isoformat(),
                           comparison_valid=result.usage_complete and grading["status"] != "error"
                           and result.cleanup_status in {"complete", "not_needed"})
            # 内部宿主、外部适配器及检查点故障不属于模型能力失败；原评分和日志仍完整保留。
            infrastructure_failed = result.stop_reason in {"internal_error", "mini_adapter_error", "budget_checkpoint_error", "campaign_paused"}
            if infrastructure_failed:
                row["comparison_valid"] = False
            report["trials"].append(row)
            if environment:
                await environment.close_session(workspace)
            report["active_trial"] = None
            checkpoint()
            if not cleanup_confirmed or grading.get("reason") == "cleanup_unconfirmed":
                report.update(state="stopped", stop_reason="cleanup_unconfirmed")
                break
            if result.execution_status == "cancelled":
                report.update(state="stopped", stop_reason="cancelled")
                break
            if budget is not None and budget.stop_reason:
                report.update(state="stopped", stop_reason=budget.stop_reason)
                break
            if infrastructure_failed:
                report.update(state="stopped", stop_reason=result.stop_reason)
                break
            if self_test_environment_failed:
                report.update(state="stopped", stop_reason="self_test_environment")
                break
            if environment and grading["status"] == "error":
                report.update(state="stopped", stop_reason="grading_environment")
                break
            if not result.usage_complete:
                report.update(state="stopped", stop_reason="unknown_usage")
                break
        else:
            report.update(state="completed")
        return report
    except asyncio.CancelledError:
        report.update(state="stopped", stop_reason="cancelled")
        raise
    except Exception:
        report.update(state="stopped", stop_reason="evaluation_error")
        raise
    finally:
        for environment in environments.values():
            for active_path in list(environment.sessions):
                await asyncio.shield(environment.close_session(active_path))
        if report["active_trial"] is not None:
            unfinished = report["active_trial"]
            unfinished.update(success=False, execution_status="cancelled" if report["stop_reason"] == "cancelled" else "failed",
                              stop_reason=report["stop_reason"], duration_seconds=time.monotonic() - started, interrupted=True,
                              run_available=(runs_root / unfinished["run_id"] / "run.json").is_file())
            if budget is not None:
                unfinished["cost"] = budget.run_cost(unfinished["run_id"])
            report["trials"].append(unfinished)
            report["active_trial"] = None
        checkpoint()
        if preflight_only and repository_cases and report["state"] == "preflight_passed":
            from agent_harness.evaluation.preflight import make_certificate
            atomic_json(directory / "preflight-certificate.json", make_certificate(directory))
