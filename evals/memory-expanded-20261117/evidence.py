"""为两条并行评测试次维护唯一计费账本，并绑定每条实际模型请求的证据。"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
import json

from agent_harness.evaluation.budget import RESERVATION_PICOS
from agent_harness.evaluation.campaign_budget import atomic_json
from agent_harness.runtime.runner import AgentStopped
from agent_harness.storage.runs import RunStore

from run_repository import BASE_TRANSPORT, EvidenceAudit, RequestLedger, memory_run


AUDIT: ContextVar[EvidenceAudit] = ContextVar("memory_expanded_audit")  # 同一事件循环中隔离各试次观察器。


class ConcurrentLedger(RequestLedger):
    """复用原费用公式，允许不同 Run 各持有一条未结算请求。"""

    def __init__(self, report: dict, save: Callable[[], None], max_calls: int) -> None:
        """report 为共享批次，save 同步持久，max_calls 固定整批模型请求上限。恢复时复用持久账本。"""
        previous = report.get("budget")
        super().__init__(report, save)
        if previous is not None:
            self.state = previous
            report["budget"] = previous
        self.state.update(max_calls=max_calls, max_known_tokens=max_calls * 1_000_000)
        self.pending = {(row["run_id"], row["turn"]): row for row in self.state["requests"]
                        if row["status"] == "reserved"}  # 已落盘且须等待原回执的请求。

    def authorize(self, run_id: str, turn: int, purpose: str) -> None:
        """先持久化 run_id 的第 turn 次 purpose 请求预留，不能接纳时抛出 AgentStopped。"""
        reason = self.blocking_reason()
        if reason:
            self.state["stop_reason"] = reason
            self.save()
            raise AgentStopped("limit_exceeded", reason)
        if any(key[0] == run_id for key in self.pending):
            raise ValueError("同一 Run 的前次模型请求未结算")
        row = {"run_id": run_id, "turn": turn, "purpose": purpose,
               "status": "reserved", "reserved_usd_picos": RESERVATION_PICOS}
        self.pending[run_id, turn] = row
        self.state["calls_admitted"] += 1
        self.state["reserved_usd_picos"] += RESERVATION_PICOS
        self.state["requests"].append(row)
        self.save()

    def _reservation(self, run_id: str, turn: int) -> dict:
        """返回并认领 run_id/turn 唯一待结算记录，身份不匹配时抛出异常。"""
        row = self.pending.pop((run_id, turn), None)
        if row is None or row["status"] != "reserved":
            raise ValueError("模型回执与唯一预留不对应")
        return row


class TraceTransport(BASE_TRANSPORT):
    """继续使用原有官方 HTTPS 传输，对真实成功请求启用独立槽位观测。"""

    async def handle_async_request(self, request):
        """传输 HTTP request 并记录实际响应状态；请求或状态异常交由原客户端与账本处理。"""
        audit = AUDIT.get()
        audit._consider(json.loads(request.content))
        response = await super().handle_async_request(request)
        audit.requests[-1]["status"] = response.status_code
        return response


async def prepare_seed(case: dict, arm: str, attempt: Path) -> dict:
    """在同一工作区先记录冻结学习资料，再核对完整答题树，按 case 与 arm 返回零模型回执。"""
    slot = attempt / "slots" / case["id"] / arm
    learning = {name: text for name, text in case["files"].items() if name.startswith("docs/maintainers/")}
    if any(path not in learning and path not in case.get("foreign_files", {}) for episode in case["episodes"]
           for path in episode["read"]):
        raise ValueError("离线学习范围遗漏了预注册文件证据")
    workspace, reads, receipt = await memory_run.seed(case | {"files": learning}, slot, attempt / "runs")
    identity = (workspace.stat().st_dev, workspace.stat().st_ino)
    final = case["files"] | case.get("updates", {})
    memory_run.write_files(workspace, {path: text for path, text in final.items() if path not in learning})
    if (workspace.stat().st_dev, workspace.stat().st_ino) != identity:
        raise ValueError("离线学习与答题工作区的目录实体变化")
    for path, text in final.items():
        if memory_run.safe_path(workspace, path).read_bytes() != text.encode("utf-8"):
            raise ValueError(f"最终公开工作区内容与冻结输入不同 {path}")
    normalized = [{"actual": item["actual"], "expected": item["expected"],
                   "historical": [check["actual"] for check in item["history"]]} for item in receipt["probes"]]
    receipt["fixture_sha256"] = memory_run.fingerprint([reads, normalized, final])
    answer = {"case_id": case["id"], "arm": arm, **receipt, "reads": reads,
              "learning_files": len(learning), "answer_files": len(final)}
    atomic_json(slot / "seed.json", answer)
    return answer


async def measured_trial(case: dict, arm: str, attempt: Path, key: str, ledger: ConcurrentLedger | None) -> dict:
    """获取 case/arm 在 attempt 中的原始运行与搜索证据，ledger 为 None 时运行离线负控制。"""
    audit = EvidenceAudit(case)
    token = AUDIT.set(audit)
    try:
        row = await memory_run.trial(case, arm, attempt, key=key, guard=ledger, real=ledger is not None)
    finally:
        AUDIT.reset(token)
    history = RunStore.read(attempt / "runs", row["run_id"])
    successful = [request for request in audit.requests if request.get("status", 500) < 400]
    listing = sum(event.type == "tool_result" and event.payload.get("name") == "list_files" for event in history.events)
    row.update(scenario=case["scenario"], list_calls=listing, find_calls=listing + row["search_calls"],
               locate_steps=min((x["locate_steps"] for x in successful if x["locate_steps"] is not None), default=None),
               evidence_steps=min((x["evidence_steps"] for x in successful if x["evidence_steps"] is not None), default=None),
               search_source_lines=len(audit.original_lines),
               search_source_bytes=sum(len(line.encode("utf-8")) for _, _, line in audit.original_lines))
    folder = attempt / "slots" / case["id"] / arm
    atomic_json(folder / "lookup.json", {"requests": audit.requests, "search_source_lines": row["search_source_lines"],
                                         "search_source_bytes": row["search_source_bytes"]})
    return row
