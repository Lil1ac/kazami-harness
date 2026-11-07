"""提交工具配对、证据来源和进程清理的统一回执。"""

import json
from typing import TYPE_CHECKING

from agent_harness.providers.base import ToolCall
from agent_harness.schemas import ArtifactRef

if TYPE_CHECKING:
    from agent_harness.runtime.runner import RunContext


def skipped(code: str, message: str | None = None) -> dict:
    """用 code 标识未完成或未执行的工具；message 为可选的具体补救提示。"""
    return {"ok": False, "data": None, "error": {"code": code, "message": message or "该工具未能完成，运行已停止。"}, "truncated": False}


def record_tool(context: "RunContext", call: ToolCall, result: dict, messages: list[dict]) -> None:
    """校验并编码结果后再提交事件，每个调用只留下一个已配对的结果。"""
    accepted_command = call.name == "run_command" and (result.get("data") or {}).get("accepted")
    if result.get("ok") and not accepted_command and call.name not in {"task_create", "task_get", "task_list", "task_update", "memory_propose"}:
        # 回执明确返回可复制的证据引用，模型不必猜测供应商工具编号或当前运行编号。
        result = {**result, "evidence_ref": {"run_id": str(context.manifest.run_id), "tool_call_id": call.call_id}}
    # 命令原始输出引用相对其所属运行，跨轮回读时不能误用新运行目录。
    if context.manifest.config.context.compression != "off" and call.name == "run_command":
        result = {**result, "context_source_run_id": str(context.manifest.run_id)}
    rank = {"not_needed": 0, "complete": 1, "unknown": 2, "incomplete": 3}
    data = result.get("data") or {}
    status = data.get("cleanup_status", "not_needed")
    if status not in rank:
        raise ValueError("工具清理状态无效")
    references = [ArtifactRef.model_validate(raw) for raw in result.get("artifact_refs", [])]
    # 在事件提交前完成所有可能失败的本地转换，避免校验失败后为同一调用写第二条结果。
    content = json.dumps(result, ensure_ascii=False, allow_nan=False)
    context.emit("tool_result", {"call_id": call.call_id, "name": call.name, "result": result})
    if rank[status] > rank[context.result.cleanup_status]:
        context.result.cleanup_status = status
    for reference in references:
        if reference.location == "run" and reference not in context.result.artifacts:
            context.result.artifacts.append(reference)
    messages.append({"role": "tool", "tool_call_id": call.call_id, "content": content})
