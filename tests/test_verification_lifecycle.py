"""验证模型完成后的独立验收生命周期，包括超时、取消、存储和历史兼容。"""

import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

from pydantic import ValidationError
import pytest

from agent_harness.providers.base import ModelError, ModelResponse
from agent_harness.runner import CancellationState, exit_code, run_task
from agent_harness.schemas import RunConfig, RunLimits, RunManifest, RunResult, TaskSpec, VerifierSpec
from agent_harness.store import RunStore, StorageError
from agent_harness.verifier import Verifier


class FinalModel:
    """只返回一次已完成回答并统计调用数，发现误重入时直接令测试失败。"""

    def __init__(self) -> None:
        """初始化调用数为0；返回 None，不创建文件或网络连接。"""
        self.calls = 0  # 累计逻辑模型请求；验收阶段必须保持1。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """接收标准消息/工具声明并返回最终回答和已知用量，无外部副作用。"""
        self.calls += 1
        assert self.calls == 1
        return ModelResponse(
            message={"role": "assistant", "content": "已生成报告。"}, tool_calls=[],
            finish_reason="stop", response_model="offline-model",
            usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        )


def manifest(tmp_path: Path, source: str, *, verifier_timeout: float = 5.0, **limits) -> RunManifest:
    """创建隔离任务与宿主目录，返回清单；source 为验收程序，时限单位为秒。"""
    workspace = tmp_path / "workspace"
    host = tmp_path / "host"
    workspace.mkdir()
    host.mkdir()
    return RunManifest(
        run_id=uuid4(), config=RunConfig(limits=RunLimits(**limits)),
        task=TaskSpec(prompt="生成报告", workspace=workspace, verifier=VerifierSpec(
            argv=[sys.executable, "-c", source], cwd=host, timeout_seconds=verifier_timeout,
        )),
    )


def history_types(tmp_path: Path, value: RunManifest, outcome) -> list[str]:
    """读取 value 的持久化记录并核对 outcome，返回有序事件类型，无写入副作用。"""
    history = RunStore.read(tmp_path / "records", str(value.run_id))
    assert history.result == outcome.result
    assert history.interrupted is False
    assert len(history.events) == outcome.event_count
    types = [event.type for event in history.events]
    assert types.count("verification_result") == types.count("run_finished") == 1
    assert types.index("execution_completed") < types.index("verification_started") < types.index("verification_result") < types.index("run_finished")
    assert "verification_skipped" not in types
    return types


@pytest.mark.parametrize("code,status,reason,expected_exit", [
    (0, "passed", "verification_passed", 0),
    (1, "failed", "verification_failed", 1),
    (2, "error", "verifier_exit_error", 1),
])
def test_verification_changes_validation_not_completed_execution(tmp_path: Path, code: int, status: str, reason: str, expected_exit: int):
    """code 驱动真实验收退出，核对独立状态、expected_exit 和历史；返回 None。"""
    value = manifest(tmp_path, f"import sys;print('host result');sys.exit({code})")
    model = FinalModel()
    outcome = asyncio.run(run_task(value, tmp_path / "records", "dummy-key", model_client=model))
    result = outcome.result
    assert result.execution_status == "completed"
    assert (result.validation_status, result.stop_reason) == (status, reason)
    assert result.verification.exit_code == code
    assert result.final_answer == "已生成报告。"
    assert result.usage_complete is True and result.usage.total_tokens == 8
    assert result.model_turns == model.calls == 1
    assert exit_code(result) == expected_exit
    assert len(result.artifacts) == 2
    assert all((outcome.run_dir / item.path).is_file() for item in result.artifacts)
    history_types(tmp_path, value, outcome)


@pytest.mark.parametrize("total_timeout", [False, True])
def test_verifier_and_total_deadlines_preserve_completed_model(tmp_path: Path, total_timeout: bool):
    """total_timeout 选择总预算或验收时限，核对不同原因/退出码和清理事实；返回 None。"""
    value = manifest(
        tmp_path, "import time;print('started',flush=True);time.sleep(20)",
        verifier_timeout=5.0 if total_timeout else 0.5,
        run_timeout_seconds=0.5 if total_timeout else 10.0,
    )
    model = FinalModel()
    outcome = asyncio.run(run_task(value, tmp_path / "records", "dummy-key", model_client=model))
    result = outcome.result
    assert result.execution_status == "completed"
    assert result.validation_status == "error"
    assert result.stop_reason == ("run_timeout" if total_timeout else "verifier_timeout")
    assert result.verification.timed_out is True
    assert result.verification.cancelled is False
    assert result.verification.cleanup_status == result.cleanup_status == "complete"
    assert "started" in result.verification.stdout
    assert exit_code(result) == (124 if total_timeout else 1)
    assert result.usage_complete is True and model.calls == 1
    history_types(tmp_path, value, outcome)


@pytest.mark.parametrize("user_sigint", [False, True])
def test_cancel_during_verifier_keeps_output_and_source(tmp_path: Path, user_sigint: bool):
    """user_sigint 指定有无CLI信号证据；真实进程启动后取消并检查130和唯一终态。"""
    source = "import pathlib,time;print('before cancel',flush=True);pathlib.Path('ready').write_text('1');time.sleep(20)"
    value = manifest(tmp_path, source)
    state = CancellationState(user_requested=user_sigint)
    model = FinalModel()

    async def scenario():
        """等待宿主 ready 文件再取消任务；最多5秒等启动，返回已收尾的 RunOutcome。"""
        running = asyncio.create_task(run_task(value, tmp_path / "records", "dummy-key", model_client=model, cancellation_state=state))
        async with asyncio.timeout(5):
            while not (tmp_path / "host" / "ready").exists():
                if running.done():
                    raise AssertionError("验收进程未启动便已结束")
                await asyncio.sleep(0.01)
        running.cancel()
        return await running

    outcome = asyncio.run(scenario())
    result = outcome.result
    assert result.execution_status == "completed" and result.validation_status == "error"
    assert result.stop_reason == ("user_cancelled" if user_sigint else "task_cancelled")
    assert result.verification.cancelled is True
    assert result.verification.timed_out is False
    assert result.cleanup_status == "complete"
    assert "before cancel" in result.verification.stdout
    assert exit_code(result) == 130
    assert result.usage_complete is True and model.calls == 1
    history_types(tmp_path, value, outcome)
    history = RunStore.read(tmp_path / "records", str(value.run_id))
    diagnostic = next(event.payload for event in history.events if event.type == "cancellation_received")
    assert diagnostic["source"] == ("user_sigint" if user_sigint else "task_cancel")
    assert diagnostic["phase"] == "verification"


def test_model_failure_does_not_start_verifier(tmp_path: Path, monkeypatch):
    """模型报错时不产生宿主 marker，也不写验收详情；返回 None。"""
    value = manifest(tmp_path, "from pathlib import Path;Path('marker').write_text('bad')")

    async def failing_complete(self, messages, tools):
        """以标准 messages/tools 协议模拟模型故障；抛 ModelError，不执行工具。"""
        raise ModelError("model_protocol_error", "受控模型失败")

    monkeypatch.setattr(FinalModel, "complete", failing_complete)
    outcome = asyncio.run(run_task(value, tmp_path / "records", "dummy-key", model_client=FinalModel()))
    assert outcome.result.execution_status == "failed"
    assert outcome.result.validation_status == "unverified"
    assert outcome.result.verification is None
    assert not (tmp_path / "host" / "marker").exists()
    events = RunStore.read(tmp_path / "records", str(value.run_id)).events
    assert not any(event.type == "verification_started" for event in events)
    skipped = next(event for event in events if event.type == "verification_skipped")
    assert skipped.payload["reason"] == "execution_not_completed"


def test_storage_failure_before_verifier_has_no_process_side_effect(tmp_path: Path, monkeypatch):
    """验收开始事件不能落盘时禁止启动进程，不伪造 summary；返回 None。"""
    value = manifest(tmp_path, "from pathlib import Path;Path('marker').write_text('bad')")
    original = RunStore.append

    def failing_append(self, event_type, payload=None):
        """event_type 为 verification_started 时抛 StorageError，其余按 payload 正常写入。"""
        if event_type == "verification_started":
            raise StorageError("模拟磁盘不可写")
        return original(self, event_type, payload)

    monkeypatch.setattr(RunStore, "append", failing_append)
    with pytest.raises(StorageError):
        asyncio.run(run_task(value, tmp_path / "records", "dummy-key", model_client=FinalModel()))
    assert not (tmp_path / "host" / "marker").exists()
    history = RunStore.read(tmp_path / "records", str(value.run_id))
    assert history.interrupted is True and history.result is None
    assert not any(event.type == "run_finished" for event in history.events)


def test_unexpected_verifier_fault_is_controlled_and_does_not_leak(tmp_path: Path, monkeypatch):
    """验收实现抛异常时保留完成态且只记录类型，不泄露正文；返回 None。"""
    value = manifest(tmp_path, "print('unused')")

    async def broken_run(self, spec, workspace, run_id):
        """接收标准验收参数并抛实现故障，用于验证受控异常结果，不启动进程。"""
        raise RuntimeError("sensitive-internal-message")

    monkeypatch.setattr(Verifier, "run", broken_run)
    outcome = asyncio.run(run_task(value, tmp_path / "records", "dummy-key", model_client=FinalModel()))
    assert outcome.result.execution_status == "completed"
    assert outcome.result.verification.reason == "verifier_runtime_error"
    assert outcome.result.cleanup_status == "unknown"
    assert exit_code(outcome.result) == 1
    assert "sensitive-internal-message" not in (outcome.run_dir / "events.jsonl").read_text(encoding="utf-8")
    history_types(tmp_path, value, outcome)


def test_old_results_remain_readable_but_detail_mismatch_is_rejected():
    """旧JSON允许缺省 verification，新增详情与顶层状态矛盾必须拒绝；返回 None。"""
    old = {"run_id": str(uuid4()), "execution_status": "completed", "validation_status": "passed"}
    assert RunResult.model_validate_json(json.dumps(old)).verification is None
    with pytest.raises(ValidationError):
        RunResult.model_validate_json(json.dumps(old | {"verification": {"status": "failed", "reason": "assertion_failed"}}))
