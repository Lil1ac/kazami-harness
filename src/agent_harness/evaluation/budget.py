"""真实 DeepSeek 评测按请求持久预留与结算，使用整数皮美元避免浮点边界。"""

from collections.abc import Callable

from agent_harness.evaluation.models import Experiment
from agent_harness.runtime.runner import AgentStopped
from agent_harness.schemas import RunConfig


# 1 USD = 10^12 皮美元。以下单 token 峰时费率按本轮批准时的官方价格固定。
USD_PICOS = 10**12
PEAK_RATES = {"prompt_cache_hit_tokens": 6_000, "prompt_cache_miss_tokens": 300_000, "completion_tokens": 1_200_000}
RESERVATION_PICOS = 35 * (USD_PICOS // 100)
MODEL_NAME = "deepseek-flash"
MODEL_VERSION = "deepseek-v4.1-flash-1017"
PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"


def validate_billing_config(config: RunConfig) -> None:
    """验证 config 的真实端点、模型及每轮输出和重试边界；超出固定预留适用范围则抛 ValueError。"""
    provider = config.custom_providers.get(config.provider)
    if (provider is None or provider.base_url.rstrip("/") not in {"https://api.deepseek.com/v1", "https://api.deepseek.com"}
            or config.requested_model != MODEL_NAME or not config.requires_model_key
            or config.limits.max_http_attempts_per_turn != 1 or config.limits.model_max_output_tokens > 8192):
        raise ValueError("逐请求费用检查要求官方 DeepSeek Flash、至多8192输出 token 且关闭 HTTP 重试")


def priced_usage(usage: dict | None) -> tuple[int, int] | None:
    """用 usage 的有效供应商计数返回 (总 token, 峰时皮美元)；字段缺失或不一致时返回 None。"""
    fields = (*PEAK_RATES, "prompt_tokens", "total_tokens")
    if usage is None or any(type(usage.get(key)) is not int or usage[key] < 0 for key in fields):
        return None
    if (usage["prompt_tokens"] != usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]
            or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
        return None
    return usage["total_tokens"], sum(usage[key] * rate for key, rate in PEAK_RATES.items())


class BatchBudget:
    """串行评测的批次账本。save 在每次发单前及回执后保存同一报告检查点。"""

    def __init__(self, experiment: Experiment, report: dict, save: Callable[[], None]) -> None:
        """将冻结 experiment 的限额写入 report；save 无参数且同步持久，保存异常向宿主传播。"""
        assert experiment.max_cost_cents is not None
        self.report = report  # 与目标题单和当前活跃试次一起落盘。
        self.save = save  # 保存失败时让请求在发送前终止。
        self.state = {  # 每次同步更新，报告读取方看到一个原子快照。
            "limit_usd_picos": experiment.max_cost_cents * (USD_PICOS // 100),
            "reservation_usd_picos": RESERVATION_PICOS,
            "pricing_url": PRICING_URL,
            "pricing_model": MODEL_VERSION,
            "peak_usd_picos_per_token": PEAK_RATES,
            "max_calls": experiment.max_model_calls,
            "max_known_tokens": experiment.max_known_tokens,
            "calls_admitted": 0,
            "known_tokens": 0,
            "known_usd_picos": 0,
            "reserved_usd_picos": 0,
            "stop_reason": None,
            "requests": [],
        }
        report["budget"] = self.state

    @property
    def stop_reason(self) -> str | None:
        """返回当前整批停止原因；空表示可继续接纳，额度拒绝同样先持久化。"""
        return self.state["stop_reason"]

    def blocking_reason(self) -> str | None:
        """返回下一次请求无法接纳的原因，额度足够且无停止原因时返回 None。"""
        state = self.state
        if self.stop_reason is not None:
            return self.stop_reason
        if state["calls_admitted"] >= state["max_calls"]:
            return "batch_model_limit"
        if state["known_usd_picos"] + state["reserved_usd_picos"] + RESERVATION_PICOS > state["limit_usd_picos"]:
            return "batch_cost_limit"
        return None

    def authorize(self, run_id: str, turn: int, purpose: str) -> None:
        """在发送 run_id 第 turn 个执行或摘要请求前落盘预留，预算拒绝抛 AgentStopped。"""
        state = self.state
        reason = self.blocking_reason()
        if reason:
            state["stop_reason"] = reason
            self.save()
            raise AgentStopped("limit_exceeded", reason)
        if state["reserved_usd_picos"]:
            raise ValueError("上次模型请求未结算")
        state["calls_admitted"] += 1
        state["reserved_usd_picos"] += RESERVATION_PICOS
        state["requests"].append({"run_id": run_id, "turn": turn, "purpose": purpose,
                                  "status": "reserved", "reserved_usd_picos": RESERVATION_PICOS})
        self.save()

    def _reservation(self, run_id: str, turn: int) -> dict:
        """返回 run_id/turn 对应的唯一待结算记录；顺序或身份错误抛 ValueError。"""
        requests = self.state["requests"]
        if not requests or (requests[-1]["run_id"], requests[-1]["turn"], requests[-1]["status"]) != (run_id, turn, "reserved"):
            raise ValueError("模型回执未绑定唯一预留")
        return requests[-1]

    def release_not_sent(self, run_id: str, turn: int, error_code: str) -> None:
        """释放 run_id/turn 明确未发送的费用预留；保留逻辑序号和 error_code 并原子保存。"""
        request = self._reservation(run_id, turn)
        request.update(status="not_sent", error_code=error_code)
        self.state["reserved_usd_picos"] -= request["reserved_usd_picos"]
        self.save()

    def settle(self, run_id: str, turn: int, usage: dict | None, model: str | None,
               error_code: str | None = None) -> str | None:
        """结算 run_id/turn 对应的一次回执。缺失 usage 保留预留；异常模型或未知回执立即阻断执行。"""
        state = self.state
        request = self._reservation(run_id, turn)
        charge = priced_usage(usage)
        unexpected = model is not None and model.lower() not in {MODEL_NAME, MODEL_VERSION}
        if unexpected:
            charge = None  # 原费率不能用于已切换的服务端模型。
        request.update(model=model, usage=usage, error_code=error_code)
        if charge is None:
            request["status"] = "unknown"
            state["stop_reason"] = ("evaluation_model_changed" if unexpected else
                                    error_code if error_code in {"model_authentication", "model_rate_limited", "model_bad_request"} else
                                    "unknown_usage")
            self.save()
            return self.stop_reason
        tokens, cost = charge
        request.update(status="settled", known_tokens=tokens, known_usd_picos=cost)
        state["known_tokens"] += tokens
        state["known_usd_picos"] += cost
        state["reserved_usd_picos"] -= RESERVATION_PICOS
        if cost > RESERVATION_PICOS:
            state["stop_reason"] = "evaluation_rate_mismatch"
        elif state["known_tokens"] >= state["max_known_tokens"]:
            state["stop_reason"] = "batch_known_token_threshold"
        self.save()
        return self.stop_reason if state["stop_reason"] == "evaluation_rate_mismatch" else None

    def run_cost(self, run_id: str) -> dict:
        """汇总 run_id 的已预留请求，返回该试次费用、保留预留及完整性，可在异常终态调用。"""
        requests = [item for item in self.state["requests"] if item["run_id"] == run_id]
        pending = [item for item in requests if item["status"] in {"reserved", "unknown"}]
        return {"known_usd_picos": sum(item.get("known_usd_picos", 0) for item in requests),
                "reserved_usd_picos": sum(item["reserved_usd_picos"] for item in pending),
                "complete": not pending}
