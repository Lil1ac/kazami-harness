"""检查点预算计算的独立边界测试，不启动模型、工具或Git。"""

from uuid import uuid4

import pytest

from agent_harness.checkpoints import accumulated_budget
from agent_harness.schemas import ResumeBudget, RunConfig, RunEvent, RunLimits, RunManifest, TaskSpec
from agent_harness.store import RunHistory


def history(tmp_path, events: list[tuple[str, dict]]) -> RunHistory:
    """将事件对转换为连续只读中断历史，模型超时固定2秒以核验保守预算。"""
    identifier = uuid4()
    manifest = RunManifest(run_id=identifier, task=TaskSpec(prompt="原任务", workspace=tmp_path),
                           config=RunConfig(limits=RunLimits(model_turn_timeout_seconds=2.0)))
    records = [RunEvent(run_id=identifier, seq=index+1, elapsed_ms=index*100, type=kind, payload=payload)
               for index, (kind, payload) in enumerate(events)]
    return RunHistory(manifest=manifest, events=records, result=None, interrupted=True)


def test_unanswered_requests_count_and_reserve_timeout_without_downtime(tmp_path) -> None:
    """中断请求计模型/HTTP与不完整usage；仅预留一次timeout，不累计任意停机时间。"""
    value = history(tmp_path, [("run_created", {}), ("model_request_started", {"purpose": "execution"}),
                               ("model_http_attempt", {"attempt": 1})])
    budget = accumulated_budget(value)
    assert budget.model_turns == 1 and budget.http_attempts == 1
    assert budget.usage_complete is False
    assert budget.active_duration_seconds == pytest.approx(2.2)


def test_summary_and_resumed_prior_budget_accumulate(tmp_path) -> None:
    """摘要也占累计模型与摘要次数；旧预算由resume_loaded恢复，不依赖不存在的终态。"""
    prior = ResumeBudget(model_turns=3, http_attempts=4, tool_calls=2, summary_calls=1,
                         total_tokens=7, active_duration_seconds=3.0, usage_complete=False)
    value = history(tmp_path, [("run_created", {}), ("resume_loaded", {"budget": prior.model_dump(mode="json")}),
                               ("model_request_started", {"purpose": "summary"}),
                               ("model_http_attempt", {"attempt": 1}),
                               ("model_response", {"usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}})])
    # 来源字段可缺省于该最小历史；resume_loaded仍应成为中断恢复的计数事实。
    value.manifest = value.manifest.model_copy(update={"resume_source": object()})
    budget = accumulated_budget(value)
    assert budget.model_turns == 4 and budget.http_attempts == 5 and budget.summary_calls == 2
    assert budget.total_tokens == 13 and budget.active_duration_seconds == pytest.approx(3.4)
    assert budget.usage_complete is False


@pytest.mark.parametrize("cleanup", [None, "not_needed", "complete", "unknown", "incomplete"])
def test_interrupted_view_retains_completed_command_cleanup(tmp_path, cleanup: str | None) -> None:
    """中断视图沿用已完成命令的清理事实；未记录结果的命令仍显示未知。"""
    from agent_harness.web.service import _unfinished_result

    events = [("run_created", {}), ("tool_started", {"call_id": "command", "name": "run_command"})]
    if cleanup is not None:
        events.append(("tool_result", {"call_id": "command", "name": "run_command",
                                       "result": {"ok": True, "data": {"cleanup_status": cleanup}}}))
    view = _unfinished_result(history(tmp_path, events))
    assert view.cleanup_status == (cleanup or "unknown")
    assert view.execution_status == "running"
    assert view.usage_complete is False
