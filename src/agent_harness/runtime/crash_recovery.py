"""从完整事件与当前工作区还原崩溃接续所需的预算和工具回执。"""

from datetime import datetime
from hashlib import sha256
from pathlib import Path

from agent_harness.runtime.receipts import skipped
from agent_harness.schemas import CrashResume
from agent_harness.storage.runs import RunHistory
from agent_harness.storage.sessions import history_messages
from agent_harness.tools.files import inspect_file_write
from agent_harness.work.tasks import TaskError


# 首版核对本机文件与命令证据。带有其他效果的工具批次留给相应的原有审阅流程。
READ_TOOLS = frozenset({"read_file", "search_files", "list_files", "read_context"})
WRITE_TOOLS = frozenset({"edit_file", "write_file"})
COMMAND_TOOLS = frozenset({"run_command", "read_command", "wait_command", "stop_command"})
SAFE_CLEANUP = frozenset({"complete", "not_needed"})
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _budgets(history: RunHistory, observed: datetime) -> dict:
    """按已接纳事件与首次核对时刻计算消耗。observed 使用 UTC，缺失用量保持未知。"""
    prior = history.manifest.resumed_budget
    turns = {event.payload["model_turn"] for event in history.events if event.type == "model_request_started"}
    responses = {event.payload["model_turn"]: event.payload.get("usage") for event in history.events if event.type == "model_response"}
    spent_usage = {key: prior.spent_usage.get(key, 0) if prior else 0 for key in USAGE_FIELDS}
    complete = (prior.spent_usage_complete if prior else True) and turns == responses.keys()
    for usage in responses.values():
        for key in USAGE_FIELDS:
            value = (usage or {}).get(key)
            if type(value) is int and value >= 0:
                spent_usage[key] += value
            else:
                complete = False
    if any(event.type == "model_http_attempt" and event.payload["attempt"] > 1 for event in history.events):
        complete = False
    wall = (observed - history.manifest.created_at).total_seconds()
    elapsed = history.events[-1].elapsed_ms / 1000
    if wall < 0:
        raise TaskError("recovery_clock_changed", "后台时钟无法核对，不能确定剩余时限。")
    return {"spent_model_turns": (prior.spent_model_turns if prior else 0) + len(turns),
            "spent_tool_calls": (prior.spent_tool_calls if prior else 0) + sum(event.type == "tool_requested" for event in history.events),
            "spent_seconds": (prior.spent_seconds if prior else 0) + max(wall, elapsed),
            "spent_usage": spent_usage, "spent_usage_complete": complete,
            "repair_attempts": prior.repair_attempts if prior else 0}


def _command_boundary(history: RunHistory) -> None:
    """要求命令均有可信进程终态。已开始的宿主验收在中断后保留原证据，需独立核对。"""
    commands = {}
    for event in history.events:
        if event.type == "command_snapshot":
            record = event.payload["command"]
            commands[record["id"]] = record
    if any(record["status"] not in {"completed", "failed", "cancelled"} or record["cleanup_status"] not in SAFE_CLEANUP
           for record in commands.values()):
        raise TaskError("recovery_command_unknown", "有命令尚无可信结束与清理记录，请先核对本机进程。")
    if any(event.type == "verification_started" for event in history.events):
        detail = next((event.payload["verification"] for event in reversed(history.events) if event.type == "verification_result"), None)
        if detail is None or detail["cleanup_status"] not in SAFE_CLEANUP:
            raise TaskError("recovery_verification_unknown", "验收进程尚无可信结束与清理记录，请先核对本机进程。")
        raise TaskError("recovery_verification_finished", "原验收已有结果，但交付未收尾，请核对原始验收记录。")


def _tool_batches(history: RunHistory) -> list[tuple[dict, dict]]:
    """按每条 assistant_message 固定调用、意图与回执。拒绝跨批次或参数不匹配的写入证据。"""
    records = []
    batch = {}
    seq = None
    for event in history.events:
        value = event.payload
        if event.type == "assistant_message":
            seq = event.seq
            calls = value["message"].get("tool_calls") or []
            batch = {call["id"]: (call, {"index": index}) for index, call in enumerate(calls)}
            records.extend(batch.values())
        elif event.type in {"tool_started", "file_write_preparing", "file_write_intent", "tool_result"}:
            call, facts = batch[value["call_id"]]
            if event.type in facts or "tool_result" in facts:
                raise TaskError("recovery_evidence_invalid", "同一工具出现重复或倒序记录，请核对原始轨迹。")
            if event.type != "tool_result":
                arguments = call["function"]["arguments"]
                digest = sha256(arguments.encode("utf-8", errors="surrogatepass")).hexdigest()
                if (value["response_seq"], value["index"], value["arguments_sha256"]) != (seq, facts["index"], digest):
                    raise TaskError("recovery_evidence_invalid", "写入证据与原模型工具批次不一致。")
            facts[event.type] = value
    return records


def inspect_crash(history: RunHistory, runs_root: Path, observed: datetime,
                  workspace_identity: list[int], *, clean: bool = False) -> CrashResume:
    """核对 history 的恢复条件，返回直接父段的接续输入。

    runs_root 是记录保护边界，observed 是首次持锁核对的 UTC 时刻，workspace_identity 由宿主验证。
    clean 默认只读。认领资源并核对恢复版本后才可清除证据匹配的暂存文件。
    不安全状态抛 TaskError，历史格式错误沿用 SessionError。
    """
    manifest = history.manifest
    history_messages(history, allow_interrupted=True)
    if (not manifest.crash_recovery or not history.interrupted or manifest.config.mcp_tools
            or any(event.type in {"permission_requested", "hook_record", "subagent_snapshot", "team_snapshot",
                                 "cancellation_received", "repair_started", "runtime_error"}
                   for event in history.events)):
        raise TaskError("recovery_stage_unsupported", "中断阶段包含需要单独核对的动作。")
    _command_boundary(history)
    budget = _budgets(history, observed)
    limits = manifest.config.limits
    if (budget["spent_model_turns"] >= limits.max_model_turns or budget["spent_tool_calls"] >= limits.max_tool_calls
            or budget["spent_seconds"] >= limits.run_timeout_seconds):
        raise TaskError("recovery_budget_exhausted", "原运行的模型、工具或活动时限额度已用尽。")

    results = {}
    writes = {}
    for call, facts in _tool_batches(history):
        name = call["function"]["name"]
        identifier = call["id"]
        started = "tool_started" in facts
        receipt = facts.get("tool_result")
        if started and name not in READ_TOOLS | WRITE_TOOLS | COMMAND_TOOLS:
            raise TaskError("recovery_tool_unsupported", "中断运行使用了需要单独核对的工具。")
        if receipt is not None and (receipt["result"].get("data") or {}).get("cleanup_status", "not_needed") not in SAFE_CLEANUP:
            raise TaskError("recovery_command_unknown", "工具的进程清理尚无可信结果。")
        preparing = facts.get("file_write_preparing")
        if started and name in WRITE_TOOLS and (receipt is None or receipt["result"]["ok"]):
            if preparing is None:
                raise TaskError("recovery_file_unverified", "文件写入缺少可核对的对象证据，请检查工作树。")
            # 同一路径在后续成功编辑后只能按最新写入核对，早期回执仍照常继承。
            writes[preparing["path"]] = (name, identifier, preparing, facts.get("file_write_intent"), receipt)
        elif preparing is not None and receipt is not None:
            stage = manifest.task.workspace / preparing["path"]
            if (stage.parent / preparing["stage"]).exists():
                raise TaskError("recovery_file_unverified", "失败写入仍留有暂存文件，请检查工作树。")
        elif receipt is None:
            if started and name not in READ_TOOLS:
                raise TaskError("recovery_tool_unknown", "已启动的工具缺少结束回执，请先核对现场。")
            results[identifier] = skipped("recovery_tool_skipped", "中断后已结束原工具调用，请按当前工作区状态继续。")

    for name, identifier, preparing, intent, receipt in writes.values():
        status = inspect_file_write(manifest.task.workspace, runs_root, preparing, intent, clean=clean)
        if status == "unknown" or receipt is not None and status != "applied":
            raise TaskError("recovery_file_changed", "目标文件、暂存对象或路径已变化，无法确认原写入。")
        if receipt is not None:
            continue
        if status == "not_applied":
            results[identifier] = skipped("recovery_write_not_applied", "已核对原写入没有提交，请重新决定下一步。")
            continue
        data = {"path": preparing["path"], "bytes_written": preparing["size"], "sha256": preparing["sha256"]}
        data.update({"replacements": 1} if name == "edit_file" else {"created": True, "overwritten": False})
        results[identifier] = {"ok": True, "data": data, "error": None, "truncated": False, "recovered": True}
    history_messages(history, allow_interrupted=True, recovered=results)
    return CrashResume(source_run_id=manifest.run_id, workspace_identity=workspace_identity, tool_results=results, **budget)
