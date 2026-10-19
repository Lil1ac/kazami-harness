"""只验证调用者可观察的记录完整性与恢复边界。"""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from agent_harness.cli import main
from agent_harness.schemas import RunConfig, RunManifest, RunResult, TaskSpec
from agent_harness.store import RunStore, StorageError


def manifest(workspace: Path) -> RunManifest:
    """为存储测试构造包含绝对工作目录和默认配置的独立运行清单。"""
    return RunManifest(
        run_id=uuid4(),
        task=TaskSpec(prompt="分析中文日志并生成结果", workspace=workspace.resolve()),
        config=RunConfig(),
    )


def test_utf8_round_trip_and_event_order(tmp_path: Path) -> None:
    """验证中文事件可完整写入和读取，序号连续且累计耗时不倒退。"""
    value = manifest(tmp_path)
    store = RunStore.create(tmp_path / "runs", value)
    first = store.append("run_started")
    second = store.append("tool_completed", {"output": "已读取：你好，世界"})
    history = RunStore.read(tmp_path / "runs", str(value.run_id))

    assert history.manifest == value
    assert [event.seq for event in history.events] == [1, 2, 3]
    assert history.events[-1].payload["output"] == "已读取：你好，世界"
    assert second.elapsed_ms >= first.elapsed_ms >= 0
    assert "你好，世界" in (store.run_dir / "events.jsonl").read_text(encoding="utf-8")


def test_create_never_overwrites_existing_run(tmp_path: Path) -> None:
    """验证同一 UUID 不能重复创建运行目录，也不会覆盖已有清单。"""
    value = manifest(tmp_path)
    store = RunStore.create(tmp_path, value)
    original = (store.run_dir / "run.json").read_bytes()
    with pytest.raises(StorageError):
        RunStore.create(tmp_path, value)
    assert (store.run_dir / "run.json").read_bytes() == original


@pytest.mark.parametrize("run_id", ["../outside", "../../", "C:/outside", "not-a-uuid"])
def test_read_rejects_path_traversal(tmp_path: Path, run_id: str) -> None:
    """验证历史读取仅接受 UUID，拒绝将外部路径或父级路径作为运行标识。"""
    with pytest.raises(StorageError):
        RunStore.read(tmp_path, run_id)


def test_read_rejects_symlink_outside_root(tmp_path: Path) -> None:
    """验证运行目录中的外部符号链接无法绕过记录根目录边界。"""
    root = tmp_path / "runs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    identifier = uuid4()
    try:
        (root / str(identifier)).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统未授予创建符号链接权限")
    with pytest.raises(StorageError):
        RunStore.read(root, str(identifier))


def test_terminal_result_is_written_exactly_once(tmp_path: Path) -> None:
    """验证只能提交匹配本次运行的唯一终态，结束后禁止继续追加事件。"""
    value = manifest(tmp_path)
    store = RunStore.create(tmp_path, value)
    with pytest.raises(StorageError):
        store.finalize(RunResult(run_id=value.run_id, execution_status="running"))
    with pytest.raises(StorageError):
        store.finalize(RunResult(run_id=uuid4(), execution_status="failed"))
    with pytest.raises(StorageError):
        store.append("run_finished")

    result = RunResult(run_id=value.run_id, execution_status="completed", validation_status="passed")
    store.finalize(result)
    with pytest.raises(StorageError):
        store.finalize(result)
    with pytest.raises(StorageError):
        store.append("tool_started")
    history = RunStore.read(tmp_path, str(value.run_id))
    assert history.result == result
    assert len([event for event in history.events if event.type == "run_finished"]) == 1
    assert not history.interrupted


def test_result_recovers_from_events_and_ignores_forged_summary(tmp_path: Path) -> None:
    """验证摘要缺失时仍可恢复结果，摘要被篡改时以事件日志为准并告警。"""
    value = manifest(tmp_path)
    store = RunStore.create(tmp_path, value)
    result = RunResult(run_id=value.run_id, execution_status="failed", stop_reason="工具执行失败")
    store.finalize(result)
    summary = store.run_dir / "summary.json"
    summary.unlink()
    assert RunStore.read(tmp_path, str(value.run_id)).result == result

    summary.write_text(
        RunResult(run_id=value.run_id, execution_status="completed").model_dump_json(), encoding="utf-8"
    )
    history = RunStore.read(tmp_path, str(value.run_id))
    assert history.result == result
    assert history.warnings


def test_prepared_run_is_not_interrupted_or_finished(tmp_path: Path) -> None:
    """验证只准备的任务只有创建事件，没有终态，也不会被误判为执行中断。"""
    value = manifest(tmp_path)
    RunStore.create(tmp_path, value)
    history = RunStore.read(tmp_path, str(value.run_id))
    assert [event.type for event in history.events] == ["run_created"]
    assert history.result is None
    assert not history.interrupted


def test_broken_tail_keeps_valid_prefix_without_changing_files(tmp_path: Path) -> None:
    """验证损坏日志尾部不影响有效前缀读取，并且查看历史不改写原文件。"""
    value = manifest(tmp_path)
    store = RunStore.create(tmp_path, value)
    store.append("run_started")
    store.append("tool_completed", {"output": "中文"})
    events_path = store.run_dir / "events.jsonl"
    with events_path.open("ab") as handle:
        handle.write(b'{"seq":4,"payload":"\xe4\xb8')
    before = {path.name: path.read_bytes() for path in store.run_dir.iterdir()}

    history = RunStore.read(tmp_path, str(value.run_id))
    assert len(history.events) == 3
    assert history.interrupted
    assert history.result is None
    assert history.warnings
    assert {path.name: path.read_bytes() for path in store.run_dir.iterdir()} == before


def test_uncommitted_summary_cannot_create_a_result(tmp_path: Path) -> None:
    """验证没有结束事件的摘要不能单独作为任务完成证据。"""
    value = manifest(tmp_path)
    store = RunStore.create(tmp_path, value)
    (store.run_dir / "summary.json").write_text(
        json.dumps(RunResult(run_id=value.run_id, execution_status="completed").model_dump(mode="json")),
        encoding="utf-8",
    )
    history = RunStore.read(tmp_path, str(value.run_id))
    assert history.result is None
    assert history.warnings


@pytest.mark.parametrize("filename", ["run.json", "events.jsonl", "summary.json"])
@pytest.mark.parametrize("as_json", [False, True])
def test_show_does_not_echo_sensitive_values_from_invalid_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], filename: str, as_json: bool
) -> None:
    """验证清单、事件或摘要损坏时，CLI 错误与警告均不回显敏感字段值。"""
    value = manifest(tmp_path)
    store = RunStore.create(tmp_path, value)
    secret = "DUMMY_SECRET_FROM_DAMAGED_RECORD"
    if filename == "run.json":
        damaged = value.model_dump(mode="json")
        damaged["task"]["prompt"] = {"secret": secret}
        (store.run_dir / filename).write_text(json.dumps(damaged), encoding="utf-8")
    elif filename == "events.jsonl":
        damaged = RunStore.read(tmp_path, str(value.run_id)).events[0].model_dump(mode="json")
        damaged["seq"] = secret
        with (store.run_dir / filename).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(damaged) + "\n")
    else:
        damaged = RunResult(run_id=value.run_id).model_dump(mode="json")
        damaged["execution_status"] = secret
        (store.run_dir / filename).write_text(json.dumps(damaged), encoding="utf-8")

    argv = ["show", str(value.run_id), "--runs-dir", str(tmp_path)]
    if as_json:
        argv.append("--json")
    exit_code = main(argv)
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert exit_code == (1 if filename == "run.json" else 0)
    assert filename in output
    assert "ValidationError" in output
    assert secret not in output
    assert "input_value" not in output
