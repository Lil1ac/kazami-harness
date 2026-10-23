"""通过真实落盘记录验证格式清理：普通历史可读，废弃的执行来源不再接纳。"""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from agent_harness.schemas import RunConfig, RunManifest, RunResult, TaskSpec
from agent_harness.sessions import SessionError, SessionStore
from agent_harness.store import RunStore, StorageError


@pytest.fixture
def old_ordinary_run(tmp_path: Path) -> tuple[RunStore, RunResult]:
    """在专属tmp_path创建普通终态并补入此前的空元数据，返回记录及预期结果；不访问用户文件。"""
    manifest = RunManifest(run_id=uuid4(), task=TaskSpec(prompt="普通文件任务", workspace=tmp_path), config=RunConfig())
    store = RunStore.create(tmp_path / "runs", manifest)
    store.append("run_started", {"messages": [{"role": "system", "content": "宿主规则"}, {"role": "user", "content": "普通文件任务"}]})
    result = RunResult(run_id=manifest.run_id, execution_status="completed", final_answer="已完成")
    store.finalize(result)
    source = store.run_dir / "run.json"
    payload = json.loads(source.read_bytes())
    payload.update({"resume_source": None, "attempt_index": 1})
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    events_path = store.run_dir / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    events[-1]["payload"]["result"]["resume_budget"] = None
    events_path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
    summary = store.run_dir / "summary.json"
    payload = json.loads(summary.read_bytes())
    payload["resume_budget"] = None
    summary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return store, result


def test_old_ordinary_history_read_keeps_original_bytes(old_ordinary_run) -> None:
    """旧普通清单、JSONL和摘要只读核验，返回正常结果且不补造警告或改写文件。"""
    store, result = old_ordinary_run
    before = {name: (store.run_dir / name).read_bytes() for name in ("run.json", "events.jsonl", "summary.json")}
    history = RunStore.read(store.run_dir.parent, str(result.run_id))
    assert history.result == result and not history.warnings
    assert "resume_source" not in history.manifest.model_dump()
    assert "attempt_index" not in history.manifest.model_dump()
    assert "resume_budget" not in history.result.model_dump()
    assert all((store.run_dir / name).read_bytes() == value for name, value in before.items())


@pytest.mark.parametrize("retired", [
    {"resume_source": {"source_run_id": str(uuid4())}},
    {"attempt_index": 2},
    {"attempt_index": True},
    {"unexpected_option": None},
])
def test_nonordinary_manifest_and_unknown_fields_are_refused(old_ordinary_run, retired) -> None:
    """非空执行来源、非首次序号及未知字段仍走严格校验；拒绝把旧恢复尝试当普通运行。"""
    store, result = old_ordinary_run
    source = store.run_dir / "run.json"
    payload = json.loads(source.read_bytes())
    payload.update(retired)
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageError):
        RunStore.read(store.run_dir.parent, str(result.run_id))


def test_nonempty_retired_budget_is_not_loaded(old_ordinary_run) -> None:
    """带历史执行链预算的终态不再支持，只读保留有效前缀并标记不可信，不转换为新预算。"""
    store, result = old_ordinary_run
    path = store.run_dir / "events.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    events[-1]["payload"]["result"]["resume_budget"] = {"model_turns": 3}
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    history = RunStore.read(store.run_dir.parent, str(result.run_id))
    assert history.result is None and history.interrupted and history.warnings


@pytest.mark.parametrize("old_kind", ["followup", "resume"])
@pytest.mark.parametrize("recorded_ordinal", [False, True])
def test_session_reads_only_ordinary_submission_intents(tmp_path: Path, old_kind: str, recorded_ordinal: bool) -> None:
    """普通请求旧空元数据不影响会话读取；真实旧恢复意图拒绝，读取过程不改写会话文件。"""
    session, request, child = uuid4(), uuid4(), uuid4()
    store = SessionStore(tmp_path)
    store.begin(session, request, session, child, "a" * 64, "继续")
    store.commit(session, request)
    path = tmp_path / ".sessions" / (str(session) + ".json")
    payload = json.loads(path.read_bytes())
    payload["requests"][str(request)].update({"kind": old_kind, "checkpoint_id": None, "turn_index": 2 if recorded_ordinal else None, "attempt_index": 1 if recorded_ordinal else None})
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    if old_kind == "resume":
        with pytest.raises(SessionError):
            store.read(session)
    else:
        record = store.read(session)
        assert record.run_ids == [session, child]
        assert "kind" not in record.requests[str(request)].model_dump()
    assert path.read_bytes() == before


def test_session_rejects_redundant_ordinal_conflicting_with_parent(tmp_path: Path) -> None:
    """旧普通意图若轮次与真实父关系矛盾则拒绝，不通过去字段掩盖损坏会话。"""
    session, request, child = uuid4(), uuid4(), uuid4()
    store = SessionStore(tmp_path)
    store.begin(session, request, session, child, "a" * 64, "继续")
    store.commit(session, request)
    path = tmp_path / ".sessions" / (str(session) + ".json")
    payload = json.loads(path.read_bytes())
    payload["requests"][str(request)].update({"kind": "followup", "turn_index": 3, "attempt_index": 1})
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionError):
        store.read(session)
