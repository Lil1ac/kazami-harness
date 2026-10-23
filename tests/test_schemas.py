"""验证公共数据结构的时间约束和任务成功判断规则。"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_harness.schemas import RunEvent, RunResult


def test_event_timestamp_requires_timezone_and_normalizes_to_utc():
    """验证事件时间必须带时区，且其他时区输入统一转换为 UTC。"""
    timestamp = datetime(2026, 10, 19, 8, tzinfo=timezone(timedelta(hours=8)))
    event = RunEvent(run_id=uuid4(), seq=1, elapsed_ms=0, type="run_created", time_utc=timestamp)
    assert event.time_utc == datetime(2026, 10, 19, tzinfo=UTC)
    assert event.time_utc.tzinfo is UTC
    with pytest.raises(ValidationError):
        RunEvent(run_id=uuid4(), seq=1, elapsed_ms=0, type="run_created", time_utc=datetime(2026, 10, 19))


def test_incomplete_execution_cannot_claim_validation_passed():
    """验证未完成执行不能宣称验收通过，执行完成与验收异常可以分别记录。"""
    with pytest.raises(ValidationError):
        RunResult(run_id=uuid4(), execution_status="created", validation_status="passed")
    result = RunResult(run_id=uuid4(), execution_status="completed", validation_status="error")
    assert result.execution_status == "completed"
