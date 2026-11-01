"""校验模型交接结构与实际回执；失败时保留证据，绝不从工具轨迹伪造研究结论。"""

from pydantic import Field, ValidationError, model_validator

from agent_harness.schemas import DataModel, RunEvent, RunResult, SubagentEvidence, SubagentFinding, SubagentReport
from agent_harness.subagents.profiles import READONLY_TOOLS


class ReportDraft(DataModel):
    """模型必须明确提交的报告正文；宿主来源、部分完成标记和失败原因由另一层生成。"""

    summary: str = Field(min_length=1, max_length=1200)  # 简短结论，空白无效。
    findings: list[SubagentFinding] = Field(max_length=8)  # 模型判断，不直接改变任务范围或授权。
    limitations: list[str] = Field(max_length=8)  # 每项最多240字符的限制，必须显式提供数组。
    pending: list[str] = Field(max_length=8)  # 每项最多240字符的未完成事项。
    complete: bool  # 模型对研究是否结束的声明，不代替宿主校验。

    @model_validator(mode="after")
    def validate_text(self):
        """校验本草稿非空短文本与必要修复证据；返回自身，非法时抛ValueError，不写记录。"""
        if not self.summary.strip() or any(not text.strip() or len(text) > 240 for text in [*self.limitations, *self.pending]):
            raise ValueError("报告文本必须是有界非空内容")
        if not self.complete and not (self.limitations or self.pending):
            raise ValueError("部分报告必须说明限制或未完成事项")
        for finding in self.findings:
            if not finding.title.strip() or not finding.detail.strip() or (finding.category == "required" and not finding.evidence_call_ids):
                raise ValueError("必要修复必须有读取证据，结论不能为空")
        return self


def collect_evidence(events: list[RunEvent]) -> list[SubagentEvidence]:
    """从events按调用顺序提取成功只读回执，返回每次调用一个入口；不采信模型写出的路径。

    搜索和记忆以调用编号指向完整原记录，文件读取保留实际路径、指纹和截断事实。
    """
    evidence = []
    for event in events:
        if event.type != "tool_result" or event.payload.get("name") not in READONLY_TOOLS:
            continue
        result = event.payload.get("result", {})
        if not result.get("ok"):
            continue
        data = result.get("data") or {}
        evidence.append(SubagentEvidence(tool_call_id=event.payload["call_id"], tool=event.payload["name"],
                                         path=data.get("path", data.get("directory")), sha256=data.get("sha256") if event.payload["name"] == "read_file" else None,
                                         truncated=bool(result.get("truncated"))))
    return evidence


def partial_report(events: list[RunEvent], reason: str) -> SubagentReport:
    """用events保存可核对证据并标记reason，返回无虚构发现的部分报告；不发模型或修改执行状态。"""
    return SubagentReport(summary="研究尚未完整交接，已保留实际读取证据。", evidence=collect_evidence(events)[:8],
                          limitations=["当前没有经过结构和来源核验的完整研究结论。"], pending=["核对已有证据，再决定是否继续研究。"], partial=True, reason=reason)


def finalize_report(result: RunResult, events: list[RunEvent]) -> SubagentReport:
    """核验result原始模型JSON及events来源并返回报告；非法正常结束改为明确失败，不掩盖原中断原因。

    报告总UTF-8编码最多32KiB；模型可标记未完成，宿主失败始终保持partial。
    存储不在本函数进行，调用者必须将返回值和执行终态一起固定。
    """
    sources = collect_evidence(events)
    try:
        text = (result.final_answer or "").strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        draft = ReportDraft.model_validate_json(text)
        identifiers = {item.tool_call_id for item in sources}
        referenced = {identifier for finding in draft.findings for identifier in finding.evidence_call_ids}
        if not referenced.issubset(identifiers):
            raise ValueError("subagent_report_evidence_invalid")
        ordered = [item for item in sources if item.tool_call_id in referenced] + [item for item in sources if item.tool_call_id not in referenced]
        partial = not draft.complete or result.execution_status != "completed"
        report = SubagentReport(summary=draft.summary, findings=draft.findings, limitations=draft.limitations,
                                pending=draft.pending, evidence=ordered[:24], partial=partial,
                                reason=(result.stop_reason or "subagent_marked_partial") if partial else None)
        if len(report.model_dump_json().encode("utf-8")) > 32 * 1024:
            raise ValueError("subagent_report_too_large")
        return report
    except (ValidationError, ValueError) as error:
        code = str(error) if str(error) in {"subagent_report_evidence_invalid", "subagent_report_too_large"} else "subagent_report_invalid"
        if result.execution_status == "completed":
            result.execution_status, result.stop_reason = "failed", code
        return partial_report(events, result.stop_reason or code)
