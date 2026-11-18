"""跨校准、正式对照和稳定性试次的单一账本；任何重启都从磁盘恢复费用。"""

import json
import hashlib
import os
import time
from pathlib import Path
from collections.abc import Callable

from agent_harness.evaluation.budget import BatchBudget, USD_PICOS, RESERVATION_PICOS, PEAK_RATES, MODEL_VERSION, PRICING_URL, priced_usage
from agent_harness.runtime.runner import AgentStopped

# 本轮批准上限；修改这些值须重新核对协议和用户授权。
PHASES = {"calibration": (5 * USD_PICOS, 1080), "formal": (28 * USD_PICOS, 9000),
          "stability": (7 * USD_PICOS, 2160)}
TOTAL_LIMIT = 40 * USD_PICOS  # 所有阶段、失败及重试共用的峰时美元上限。
TOTAL_CALLS = 12240  # 所有用途请求的累计接纳上限。


def atomic_json(path: Path, value: dict) -> None:
    """将value原子写入path；Windows短暂共享冲突最多重试620毫秒，持续失败保留pending并抛OSError。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    with pending.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    # Windows读取者可能短暂持有不共享删除的句柄；只重试文件替换，绝不重发模型请求。
    for attempt in range(6):
        try:
            pending.replace(path)
            return
        except PermissionError as error:
            if os.name != "nt" or getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 5:
                raise
            time.sleep(.02 * 2 ** attempt)


def receipt_sha256(row: dict) -> str:
    """返回逐请求row的稳定摘要；包含未知状态与原始回执，不改变金额或标记。"""
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class CampaignBudget(BatchBudget):
    """单写入者拥有的持久账本；调用者必须持有整轮文件锁，不自动重发未知请求。"""

    def __init__(self, path: Path, protocol_sha256: str, *, total_limit: int = TOTAL_LIMIT,
                 total_calls: int = TOTAL_CALLS, phases: dict[str, tuple[int, int]] | None = None):
        """载入或创建 path 账本，绑定冻结协议指纹；未结算请求保留预留并阻止续费。

        protocol_sha256 是协议摘要。total_limit 单位为万亿分之一美元，total_calls 含所有用途。
        phases 映射阶段到费用和请求上限，None 采用三组协议；各阶段始终共用整轮上限。
        """
        self.path = path.resolve()  # 唯一权威金额文件，不从阶段报告反推开销。
        self.phases = dict(PHASES if phases is None else phases)  # 固定阶段配额，重新打开时逐项核验。
        if total_limit <= 0 or total_calls <= 0 or not self.phases or any(
            not 0 < cost <= total_limit or not 0 < calls <= total_calls for cost, calls in self.phases.values()
        ):
            raise ValueError("整轮或阶段上限无效")
        limits = {name: {"limit_usd_picos": cost, "max_calls": calls} for name, (cost, calls) in self.phases.items()}
        self.report = {}  # 当前阶段报告的只读投影绑定，初始化时尚未绑定。
        self._report_save: Callable[[], None] | None = None  # 每次账本落盘后刷新当前报告。
        self.phase: str | None = None  # 必须由阶段调度显式选择。
        self._held_requests: set[tuple[str, int]] = set()  # 仅经明确批准并绑定回执哈希的未知请求；允许已批准的400或传输失败，永久预留仍参与所有上限。
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if (self.state["protocol_sha256"] != protocol_sha256 or self.state["limit_usd_picos"] != total_limit
                    or self.state["max_calls"] != total_calls or self.state["phase_limits"] != limits
                    or self.state["reservation_usd_picos"] != RESERVATION_PICOS
                    or self.state["peak_usd_picos_per_token"] != PEAK_RATES):
                raise ValueError("整轮账本协议改变，不能复用或重置费用")
            rows = self.state["requests"]
            if len({(row["run_id"], row["turn"]) for row in rows}) != len(rows):
                raise ValueError("账本请求身份重复")
            known, tokens, reserved = 0, 0, 0
            for row in rows:
                if row.get("phase") not in self.phases or row.get("reserved_usd_picos") != RESERVATION_PICOS:
                    raise ValueError("账本阶段或预留损坏")
                if row["status"] == "settled":
                    charge = priced_usage(row.get("usage"))
                    if charge != (row.get("known_tokens"), row.get("known_usd_picos")):
                        raise ValueError("账本回执和费用无法对账")
                    tokens += charge[0]
                    known += charge[1]
                elif row["status"] in {"reserved", "unknown"}:
                    reserved += RESERVATION_PICOS
                elif row["status"] != "not_sent":
                    raise ValueError("账本请求状态损坏")
            if (len(rows), tokens, known, reserved) != (self.state["calls_admitted"], self.state["known_tokens"],
                    self.state["known_usd_picos"], self.state["reserved_usd_picos"]):
                raise ValueError("账本累计金额与逐请求明细不一致")
            for held in self.state.get("approved_holds", []):
                matching = [row for row in rows if (row["run_id"], row["turn"]) == (held["run_id"], held["turn"])]
                if (len(matching) != 1 or matching[0]["status"] != "unknown"
                        or matching[0].get("error_code") not in {"model_bad_request", "model_transport"}
                        or held.get("reserved_usd_picos") != RESERVATION_PICOS
                        or receipt_sha256(matching[0]) != held.get("receipt_sha256")
                        or not held.get("approval_sha256")):
                    raise ValueError("批准的未知费用预留与原始回执不符")
                self._held_requests.add((held["run_id"], held["turn"]))
            if self.has_pending_request():
                self.state["stop_reason"] = "unknown_usage"
        else:
            self.state = {"protocol_sha256": protocol_sha256, "limit_usd_picos": total_limit,
                "reservation_usd_picos": RESERVATION_PICOS, "pricing_url": PRICING_URL,
                "pricing_model": MODEL_VERSION, "peak_usd_picos_per_token": dict(PEAK_RATES),
                "max_calls": total_calls, "max_known_tokens": 10**12, "calls_admitted": 0,
                "known_tokens": 0, "known_usd_picos": 0, "reserved_usd_picos": 0,
                "stop_reason": None, "requests": [], "phase_limits": limits}
        self.save()

    def has_pending_request(self) -> bool:
        """返回是否存在未获例外批准的未结算请求；已批准未知款仍保持全额预算占用。"""
        return any(row["status"] in {"reserved", "unknown"} and
                   (row["run_id"], row["turn"]) not in self._held_requests for row in self.state["requests"])

    def save(self) -> None:
        """先保存唯一费用账本，再刷新已绑定阶段报告；任一步失败都阻止新请求。"""
        try:
            atomic_json(self.path, self.state)
            if self._report_save is not None:
                self._report_save()
        except OSError as error:
            # 进度报告同样属于发单前门禁；失败必须成为整轮停止原因，不能退化成单题失败。
            self.state["stop_reason"] = "budget_checkpoint_error"
            try:
                atomic_json(self.path, self.state)
            except OSError:
                pass  # 持续不可写时保留内存状态与pending原件；进程仍向上抛停止，绝不继续发单。
            raise AgentStopped("failed", "budget_checkpoint_error") from error

    def bind(self, phase: str, report: dict, save: Callable[[], None]) -> None:
        """绑定 phase 和报告保存函数；phase 必须属于批准阶段，不改变任何累计额度。"""
        if phase not in self.phases:
            raise ValueError("未知评测阶段")
        self.phase, self.report, self._report_save = phase, report, save
        report["budget"] = self.state
        report["campaign_phase"] = phase
        self.save()

    def phase_totals(self, phase: str) -> dict:
        """汇总 phase 的所有接纳请求，返回调用数、已知费用和未结算预留，失败同样计入。"""
        rows = [row for row in self.state["requests"] if row["phase"] == phase]
        return {"calls": len(rows), "known_usd_picos": sum(row.get("known_usd_picos", 0) for row in rows),
                "reserved_usd_picos": sum(row["reserved_usd_picos"] for row in rows if row["status"] in {"reserved", "unknown"})}

    def blocking_reason(self) -> str | None:
        """检查整轮和当前阶段是否还能接纳请求；试次额度仅在 authorize 中检查。"""
        if (self.path.parent / "pause.json").exists():
            return "campaign_paused"
        reason = super().blocking_reason()
        if reason or self.phase is None:
            return reason or "campaign_phase_unbound"
        if self.has_pending_request():
            return "unknown_usage"
        totals = self.phase_totals(self.phase)
        cost, calls = self.phases[self.phase]
        if totals["calls"] >= calls:
            return "phase_model_limit"
        if totals["known_usd_picos"] + totals["reserved_usd_picos"] + RESERVATION_PICOS > cost:
            return "phase_cost_limit"
        return None

    def authorize(self, run_id: str, turn: int, purpose: str) -> None:
        """在 run_id/turn 发单前检查整轮、阶段及单题上限并落盘；拒绝抛 AgentStopped。

        purpose 标识执行、摘要或格式修复。相同 run_id/turn 禁止再次接纳，不自动重发。
        """
        if any(row["run_id"] == run_id and row["turn"] == turn for row in self.state["requests"]):
            raise ValueError("重复请求身份，禁止重新发送")
        reason = self.blocking_reason()
        if reason:
            self.state["stop_reason"] = reason
            self.save()
            raise AgentStopped("limit_exceeded", reason)
        rows = [row for row in self.state["requests"] if row["run_id"] == run_id]
        cost = self.run_cost(run_id)
        trial_reason = "trial_model_limit" if len(rows) >= 60 else (
            "trial_cost_limit" if cost["known_usd_picos"] + cost["reserved_usd_picos"] + RESERVATION_PICOS > USD_PICOS else None)
        if trial_reason:
            raise AgentStopped("limit_exceeded", trial_reason)
        self.state["calls_admitted"] += 1
        self.state["reserved_usd_picos"] += RESERVATION_PICOS
        self.state["requests"].append({"run_id": run_id, "turn": turn, "purpose": purpose, "phase": self.phase,
            "status": "reserved", "reserved_usd_picos": RESERVATION_PICOS})
        try:
            self.save()
        except AgentStopped as error:
            if error.reason == "budget_checkpoint_error":
                # authorize尚未返回，两个执行器都不可能进入HTTP；保留接纳序号并标记明确未发送。
                self.state["requests"][-1].update(status="not_sent", error_code=error.reason)
                self.state["reserved_usd_picos"] -= RESERVATION_PICOS
                try:
                    atomic_json(self.path, self.state)
                except OSError:
                    pass  # 保存失败不等于允许续跑；停止原因和pending证据必须保留。
            raise

    def settle(self, run_id: str, turn: int, usage: dict | None, model: str | None, error_code: str | None = None) -> str | None:
        """结算一条回执，重复的完全相同回执幂等返回；变化回执抛异常，不重复收费。"""
        existing = next((row for row in self.state["requests"] if row["run_id"] == run_id and row["turn"] == turn), None)
        if existing and existing["status"] in {"settled", "unknown"}:
            if (existing.get("usage"), existing.get("model"), existing.get("error_code")) != (usage, model, error_code):
                raise ValueError("已结算请求的回执发生变化")
            return self.stop_reason if existing["status"] == "unknown" else None
        return super().settle(run_id, turn, usage, model, error_code)
