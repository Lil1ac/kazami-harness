"""以真实压缩器回放冻结检查点，独立核验输入大小和旧证据可回读性。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_harness.context.archive import ContextArchive
from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compression import ContextCompressor, encode, message_groups
from agent_harness.execution.workspace import Workspace
from agent_harness.providers.base import ModelError, ModelResponse
from agent_harness.providers.chat import ChatCompletionsClient
from agent_harness.runtime.model_gateway import request_model
from agent_harness.runtime.runner import AgentStopped, RunContext
from agent_harness.schemas import ContextPolicy, RunConfig, RunManifest, RunResult, TaskSpec
from agent_harness.storage.runs import RunStore
from agent_harness.tools.registry import ToolExecutor

if TYPE_CHECKING:
    from agent_harness.evaluation.budget import BatchBudget


# 普通历史读取工具和只读回读定义固定在两臂的输入中，工具声明差不计入压缩收益。
REPLAY_TOOLS = ("read_file", "search_files", "run_command")


@dataclass
class ReplayContext(RunContext):
    """沿用产品事件与预算，按需去掉单独摘要事件中的重复输入正文。"""

    save_summary_requests: bool = False  # 是否在本机回放日志保留完整摘要请求。

    def emit(self, kind: str, payload: dict | None = None) -> None:
        """提交 kind 和字典 payload；未要求保存摘要请求时仅保留其范围与计量。"""
        if kind == "context_compaction_started" and payload is not None and not self.save_summary_requests:
            payload = {key: value for key, value in payload.items() if key not in {"messages", "tools"}}
        super().emit(kind, payload)


def replay_tools(policy: ContextPolicy) -> list[dict]:
    """返回同一检查点两臂使用的当前工具声明，policy 提供压缩规则；不访问模型或执行工具。"""
    workspace = Path(__file__).resolve().parent
    config = RunConfig(tools=list(REPLAY_TOOLS), context=policy.model_copy(update={"compression": "hybrid"}))
    return ToolExecutor(workspace, workspace, config).definitions


def _input_content(messages: list[dict], tools: list[dict]) -> bytes:
    """按产品输入计量顺序编码 messages 和 tools，返回与报告字节值对应的原始内容。"""
    return json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _tool_receipts(messages: list[dict]) -> dict[str, dict]:
    """从完整 messages 按调用编号建立工具结果索引，重号或未配对调用抛 ValueError。"""
    message_groups(messages)
    receipts = {message["tool_call_id"]: message for message in messages if message.get("role") == "tool"}
    if len(receipts) != sum(message.get("role") == "tool" for message in messages):
        raise ValueError("检查点包含重复的工具结果编号")
    return receipts


def _structure(original: list[dict], projected: list[dict]) -> dict:
    """核验 projected 的工具配对、用户和系统原文顺序、失败回执，对照 original 返回布尔明细。"""
    try:
        expected_receipts = _tool_receipts(original)
        actual_receipts = _tool_receipts(projected)
        failed_receipts = {identifier: message for identifier, message in expected_receipts.items()
                           if json.loads(message["content"])["ok"] is not True}
        for message in actual_receipts.values():
            if type(json.loads(message["content"])["ok"]) is not bool:
                raise ValueError("工具回执缺少有效状态")
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return {"paired": False, "system_and_user_preserved": None, "failed_receipts_preserved": None,
                "retained_tool_calls_preserved": None, "error": f"{type(error).__name__}: {error}"}
    protected = [message for message in original if message.get("role") in {"user", "system"}]
    candidates = iter(projected)
    protected_preserved = all(any(candidate == message for candidate in candidates) for message in protected)
    errors_preserved = all(actual_receipts.get(identifier) == message for identifier, message in failed_receipts.items())
    expected_calls = {call["id"]: call for message in original for call in message.get("tool_calls") or []}
    calls_preserved = all(expected_calls.get(call["id"]) == call
                          for message in projected for call in message.get("tool_calls") or [])
    return {"paired": True, "system_and_user_preserved": protected_preserved, "failed_receipts_preserved": errors_preserved,
            "retained_tool_calls_preserved": calls_preserved}


def _visible_references(messages: list[dict], actions: list[dict]) -> set[str]:
    """从压缩器已提交 actions 找出仍在最终 messages 中可见的归档引用，忽略被覆盖的中间引用。"""
    contents = [message.get("content") for message in messages]
    return {item["reference"] for item in actions if item["kind"] != "summary_failed"
            and any(isinstance(content, str) and item["reference"] in content for content in contents)}


async def _read_reference(executor: ToolExecutor, reference: str, expected: bytes) -> dict:
    """经产品回读工具取完整 reference，返回是否匹配 expected 原始字节与页数；失败保持可审计。"""
    pages = 0
    actual = bytearray()
    digest = hashlib.sha256(expected).hexdigest()
    while True:
        offset = len(actual)
        result = await executor.execute("read_context", encode({"reference": reference, "offset": offset, "max_bytes": 4096}))
        if not result["ok"]:
            return {"matched": False, "pages": pages, "error": result["error"]["code"]}
        page = result["data"]
        actual.extend(page["content"].encode("utf-8"))
        pages += 1
        if (page["sha256"] != digest or page["size_bytes"] != len(expected) or page["offset"] != offset
                or page["next_offset"] != len(actual) or len(actual) > len(expected)):
            return {"matched": False, "pages": pages, "error": "archive_metadata_mismatch"}
        if page["eof"]:
            return {"matched": actual == expected, "pages": pages, "error": None if actual == expected else "archive_bytes_mismatch"}
        if len(actual) == offset:
            return {"matched": False, "pages": pages, "error": "archive_cursor_did_not_advance"}


async def _verify_archives(executor: ToolExecutor, original: list[dict], projected: list[dict], actions: list[dict]) -> dict[str, dict]:
    """核验 projected 中可见的 action 归档，返回引用到字节匹配、页数和来源范围的记录。"""
    visible = _visible_references(projected, actions)
    verified = {}
    for item in actions:
        reference = item["reference"]
        if reference not in visible or item["kind"] == "summary_failed":
            continue
        start, end = item["source_start"], item["source_end"]
        source = original[start]["content"] if item["kind"] == "offload" else encode(original[start:end])
        source_bytes = source.encode("utf-8")
        digest = hashlib.sha256(source_bytes).hexdigest()
        if reference not in verified:
            findings = await _read_reference(executor, reference, source_bytes)
            verified[reference] = {**findings, "source_sha256": digest, "source_ranges": []}
        entry = verified[reference]
        entry["source_ranges"].append({"start": start, "end": end, "kind": item["kind"], "source_sha256": digest})
        if digest != entry["source_sha256"] or digest != item["source_sha256"]:
            entry.update(matched=False, error="archive_source_mismatch")
    return verified


def _contains_evidence(message: dict, needle: str) -> bool:
    """在解码后的工具正文、标准流和检索命中行中查找 needle，避免 JSON 转义影响验证。"""
    data = json.loads(message["content"]).get("data")
    if not isinstance(data, dict):
        return False
    texts = [data.get(key) for key in ("content", "stdout", "stderr")]
    texts.extend(match.get("line") for match in data.get("matches") or [] if isinstance(match, dict))
    return any(isinstance(value, str) and needle in value for value in texts)


def _verify_anchors(anchors: list[dict], original: list[dict], projected: list[dict], archives: dict[str, dict]) -> list[dict]:
    """判定预登记 anchors 的直接保留或归档回读；无效原始定位抛 ValueError。"""
    receipts = _tool_receipts(projected)
    output = []
    for anchor in anchors:
        index, needle = anchor["message_index"], anchor["contains"]
        if (type(index) is not int or not 0 <= index < len(original) or not isinstance(needle, str) or not needle
                or original[index].get("role") != "tool" or not _contains_evidence(original[index], needle)):
            raise ValueError("证据锚点必须是原检查点指定工具回执中的非空原文")
        expected = original[index]
        if receipts.get(expected["tool_call_id"]) == expected:
            output.append({**anchor, "mode": "direct", "resolved": True, "source_matched": True, "reference": None})
            continue
        matched = next((ref for ref, row in archives.items()
                        if row["matched"] and any(span["start"] <= index < span["end"] for span in row["source_ranges"])), None)
        output.append({**anchor, "mode": "archived" if matched else "missing", "resolved": matched is not None,
                       "source_matched": matched is not None, "reference": matched})
    return output


async def replay_case(
    case: dict,
    runs_root: Path,
    policy: ContextPolicy,
    *,
    config: RunConfig | None = None,
    summary_api_key: str | None = None,
    request_guard: BatchBudget | None = None,
    save_summary_requests: bool = False,
) -> dict:
    """回放单个 case 并返回可审计的同输入对照及证据明细。

    case 须提供 id、messages、可选 category、tools 和预登记 expected_evidence。runs_root 是本机专用记录目录；
    原始检查点、真实回读归档、运行计量和结果均保存于新建的 UUID 子目录。policy 提供共同的输入上限、触发比例
    和摘要限额，函数固定 off/hybrid 的预算投影和末轮收尾关闭。config 为可选已冻结的模型和运行预算；
    summary_api_key 是仅供内存使用的显式密钥，与 request_guard 成对提供并通过计费配置校验；未提供时只执行本地压缩。
    在本地模式触发摘要即记 summary_fail，未发出模型请求。
    save_summary_requests 决定额外在事件中保留实际摘要输入，默认为 False。结构验证失败、存储错误向调用方抛出。
    """
    if (summary_api_key is None) != (request_guard is None):
        raise ValueError("摘要凭据与请求账本必须同时提供")
    if summary_api_key is not None and config is None:
        raise ValueError("摘要模型必须使用明确冻结的运行配置")

    original = case["messages"]
    anchors = case.get("expected_evidence", [])
    initial_structure = _structure(original, original)
    original_evidence = _verify_anchors(anchors, original, original, {}) if initial_structure["paired"] else []
    tools = replay_tools(policy)
    if case.get("tools", tools) != tools:
        raise ValueError("冻结工具声明与本轮产品定义不一致")
    hybrid = policy.model_copy(update={"compression": "hybrid", "root_handoff": False,
                                      "root_handoff_after_input_rejection": False, "budget_projection": False,
                                      "load_project_instructions": False})
    config = (config or RunConfig()).model_copy(update={"context": hybrid, "tools": list(REPLAY_TOOLS)})
    if summary_api_key is not None:
        from agent_harness.evaluation.budget import BatchBudget, validate_billing_config

        if not isinstance(request_guard, BatchBudget):
            raise ValueError("摘要模型必须通过产品批次账本发送")
        validate_billing_config(config)

    root = runs_root.resolve()
    workspace = root.parent
    task = TaskSpec(prompt="固定检查点回放 " + case["id"], workspace=workspace)
    manifest = RunManifest(run_id=uuid4(), task=task, config=config)
    store = RunStore.create(root, manifest)
    archive = ContextArchive(store.run_dir, manifest)
    executor = ToolExecutor(workspace, root, config)
    executor.bind_context(archive)
    result = RunResult(run_id=manifest.run_id, execution_status="running", usage_complete=True)
    now = asyncio.get_running_loop().time()
    context = ReplayContext(manifest, store, result, now + config.limits.run_timeout_seconds,
                            model_request_guard=request_guard, started_at=now, save_summary_requests=save_summary_requests)
    builder = ContextBuilder(Workspace(workspace, root), task, config)
    off_config = config.model_copy(update={"context": hybrid.model_copy(update={"compression": "off"})})
    off_builder = ContextBuilder(Workspace(workspace, root), task, off_config)
    raw = off_builder.build(original, tools, 1)
    checkpoint = _input_content(original, tools)
    if len(checkpoint) != raw.check.input_bytes:
        raise ValueError("检查点落盘内容与产品字节计量不一致")
    checkpoint_digest = hashlib.sha256(checkpoint).hexdigest()
    (store.run_dir / "checkpoint.json").write_bytes(checkpoint)
    context.emit("replay_started", {"case_id": case["id"], "input_bytes": raw.check.input_bytes,
                                    "checkpoint_sha256": checkpoint_digest})
    actions: list[dict] = []  # 即使投影失败也保留已提交的动作，供诊断但不计未完成的压缩率。

    def collect(kind: str, payload: dict) -> None:
        """采集已落盘的 context_action kind/payload，供当前回放汇总；其他事件仅留在原日志。"""
        if kind == "context_action":
            actions.append(payload)

    context.on_event = collect

    async def request(messages: list[dict], definitions: list[dict], purpose: str) -> ModelResponse:
        """仅用统一账本网关发送产品产生的摘要 messages；供应商未接入时向压缩器报告原因。"""
        if purpose != "summary" or definitions:
            raise ValueError("固定回放仅允许无工具摘要请求")
        if summary_api_key is None:
            raise ModelError("summary_provider_unavailable", "此回放未接入已批准的摘要供应商与账本。")
        # 用本次回放上下文观察实际 HTTP 尝试；统一网关依结果核销或保留预留。
        async with ChatCompletionsClient(summary_api_key, config, on_attempt=context.http_attempt) as client:
            return await request_model(context, client, messages, definitions, purpose, input_check_seq=context.event_count - 1)

    compressor = ContextCompressor(context, builder, archive)
    report = {"case_id": case["id"], "category": case.get("category"), "run_id": str(manifest.run_id),
              "run_directory": str(store.run_dir), "checkpoint_sha256": checkpoint_digest, "projection_sha256": None,
              "raw_input_bytes": raw.check.input_bytes, "raw_allowed": raw.check.allowed, "max_input_bytes": hybrid.max_input_bytes,
              "raw_structure": initial_structure, "raw_evidence": original_evidence,
              "projected_input_bytes": None, "projected_allowed": None, "reduction_pct": None,
              "projected_structure": None, "archive_checks": {}, "evidence": [], "actions": actions}
    try:
        if not initial_structure["paired"]:
            raise ValueError("原始检查点消息结构不完整")
        projection = await compressor.prepare(original, tools, request)
        fitted = builder.freeze_projection(projection, result.model_turns + 1, allow_tools=True)
        if fitted.check.raw_input_bytes != raw.check.input_bytes or _input_content(original, tools) != checkpoint:
            raise ValueError("两臂的原始输入不一致")
        projected = _input_content(fitted.messages, fitted.tools)
        if len(projected) != fitted.check.input_bytes:
            raise ValueError("投影落盘内容与产品字节计量不一致")
        (store.run_dir / "projection.json").write_bytes(projected)
        context.emit("context_checked", {**fitted.check.model_dump(mode="json"), "phase": "execution"})
        structure = _structure(original, fitted.messages)
        checks = await _verify_archives(executor, original, fitted.messages, actions) if structure["paired"] else {}
        evidence = _verify_anchors(anchors, original, fitted.messages, checks) if structure["paired"] else []
        valid = all(structure.values()) and all(row["matched"] for row in checks.values()) and all(row["resolved"] for row in evidence)
        reason = None if valid else "replay_verification_failed"
        report.update(status="measured" if valid else "verification_failed", stop_reason=reason,
                      projected_input_bytes=fitted.check.input_bytes, projected_allowed=fitted.check.allowed,
                      projection_sha256=hashlib.sha256(projected).hexdigest(),
                      reduction_pct=(raw.check.input_bytes - fitted.check.input_bytes) * 100 / raw.check.input_bytes if valid else None,
                      projected_structure=structure, archive_checks=checks, evidence=evidence)
        result.execution_status = "completed" if valid else "failed"
        result.stop_reason = reason
    except (AgentStopped, ModelError) as error:
        reason = error.reason if isinstance(error, AgentStopped) else error.code
        report.update(status="summary_fail", stop_reason=reason)
        result.execution_status, result.stop_reason = "failed", reason
    except ValueError as error:
        reason = "raw_structure_invalid" if not initial_structure["paired"] else "replay_input_invalid"
        report.update(status="verification_failed", stop_reason=reason, error=str(error))
        result.execution_status, result.stop_reason = "failed", reason

    report.update(summary_attempts=compressor.summary_calls, summary_model_calls=result.model_turns,
                  http_attempts=result.http_attempts, usage_complete=result.usage_complete,
                  usage=result.usage.model_dump(mode="json"))
    store.finalize(result)
    (store.run_dir / "replay.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
