"""在真实Git、真实工具与离线模型上验证检查点恢复完整性。"""

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from agent_harness.checkpoints import CheckpointError, CheckpointStore, load_resume
from agent_harness.context_archive import ContextArchive, ReadContextArguments
from agent_harness.projects import ProjectManager, ProjectPreset
from agent_harness.providers.base import ModelError, ModelResponse, ToolCall
from agent_harness.runner import run_task
from agent_harness.schemas import ContextPolicy, ResumeSource, RunConfig, RunLimits, RunManifest, RunOrigin, TaskSpec
from agent_harness.store import RunStore
from agent_harness.workspace import WorkspaceError


def git(root: Path, *arguments: str) -> bytes:
    """只在测试临时仓库运行固定Git参数，返回stdout；失败立即终止测试。"""
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, timeout=20).stdout


def model_response(calls: list[ToolCall] | None = None, content: str = "") -> ModelResponse:
    """构造带确切usage的离线响应，calls=None表示最终文本，无网络副作用。"""
    calls = calls or []
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": call.call_id, "type": "function",
                                  "function": {"name": call.name, "arguments": call.arguments_json}} for call in calls]
    return ModelResponse(message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop",
                         response_model="fixture", usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})


class SourceModel:
    """首请求执行真实文件/命令工具，下一请求模拟模型中断。"""

    def __init__(self) -> None:
        """初始化调用计数；每个测试新实例，避免跨运行共享模型状态。"""
        self.calls = 0  # 第一次返回工具，第二次在无工具副作用边界中断。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """返回一次完整工具批次或抛ModelError；messages/tools只读，实际工具由Runner执行。"""
        self.calls += 1
        if self.calls > 1:
            raise ModelError("model_timeout", "离线模拟等待模型超时")
        return model_response([
            ToolCall("write", "write_file", json.dumps({"path": "app.txt", "content": "fixed\n", "overwrite": True})),
            ToolCall("count", "run_command", json.dumps({"argv": ["python", "-c", "from pathlib import Path; p=Path('count.txt'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"]})),
            ToolCall("read", "read_file", json.dumps({"path": "large.txt", "max_bytes": 12000})),
        ])


class SummarySourceModel(SourceModel):
    """用长分析和真实读取触发摘要，再于下一执行请求中断。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """摘要请求返回严格工作JSON；正常请求先真实工具再中断，无网络调用。"""
        if not tools:
            return model_response(content=json.dumps({"goal": "继续修复app", "decisions": ["保留旧接口"],
                                  "changes": ["修改app.txt"], "evidence": ["之前读取了文件"],
                                  "open_issues": [], "next_steps": ["继续确认结果"]}, ensure_ascii=False))
        self.calls += 1
        if self.calls >= 4:
            raise ModelError("model_timeout", "离线模拟模型中断")
        if self.calls == 1:
            self.calls -= 1
            response = await super().complete(messages, tools)
            return ModelResponse(message={**response.message, "content": "历史分析记录abcd；" * 220},
                                 tool_calls=response.tool_calls, finish_reason=response.finish_reason,
                                 response_model=response.response_model, usage=response.usage)
        return model_response([ToolCall("read-" + str(self.calls), "read_file",
                                       json.dumps({"path": "app.txt"}))], content="历史分析记录abcd；" * 220)


class FinalModel:
    """捕获恢复后的模型输入并立即返回最终回答，验证宿主不重放工具。"""

    def __init__(self) -> None:
        """建立独立请求副本列表，不执行任何文件或进程操作。"""
        self.messages = []  # 保存每次输入副本供断言重复用户要求和路径迁移。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """只记录messages副本并返回最终响应，tools不改变执行权限。"""
        self.messages.append(deepcopy(messages))
        return model_response(content="继续已完成的修改。")


@pytest.fixture
def saved_source(tmp_path: Path, request) -> dict:
    """创建真实独立Git工作树并运行到模型失败；返回封存证据和管理器，保留全部原记录。"""
    source = tmp_path / "source"
    source.mkdir()
    for arguments in (("init",), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid"), ("config", "core.autocrlf", "false")):
        git(source, *arguments)
    if getattr(request, "param", None) == "crlf":
        git(source, "config", "core.autocrlf", "true")
    (source / "app.txt").write_text("initial\n", encoding="utf-8")
    (source / "large.txt").write_text("evidence line\n" * 850, encoding="utf-8")
    (source / "AGENTS.md").write_text("保留原始项目规则。\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "baseline")
    root = tmp_path / "runs"
    manager = ProjectManager(root, tmp_path / "worktrees")
    preset = ProjectPreset(id="fixture", label="测试项目", path=source, test_argv=["python", "-c", "pass"])
    identifier = uuid4()
    review = manager.prepare(preset, identifier)
    summary_mode = getattr(request, "param", None) == "summary"
    manifest = RunManifest(run_id=identifier, session_id=identifier, origin=RunOrigin(kind="project", preset_id=preset.id, registered_path=source),
                           task=TaskSpec(prompt="修复app并保留证据", workspace=Path(review.worktree)),
                           config=RunConfig(tools=["read_file", "write_file", "run_command"], context=ContextPolicy(compression="hybrid" if summary_mode else "masking", inline_tool_bytes=512,
                                                                  max_input_bytes=20000 if summary_mode else 256*1024, keep_recent_groups=1),
                                            limits=RunLimits(model_turn_timeout_seconds=0.5, run_timeout_seconds=120.0)))
    store = CheckpointStore(root, manager)

    async def capture(context, messages, compressor, builder):
        """在线程封存并将结果交回Runner单写发布，不自行追加运行日志。"""
        return await asyncio.to_thread(store.capture, context, messages, compressor, builder)

    outcome = asyncio.run(run_task(manifest, root, "fixture", model_client=SummarySourceModel() if summary_mode else SourceModel(), on_checkpoint=capture))
    history = RunStore.read(root, str(identifier))
    assert outcome.result.execution_status == "timed_out"
    assert not [event for event in history.events if event.type == "checkpoint_failed"]
    checkpoint = store.latest(history)
    assert checkpoint is not None and checkpoint.tool_batch_count >= 1
    return {"root": root, "manager": manager, "preset": preset, "history": history, "store": store,
            "checkpoint": checkpoint, "capture": capture}


def resumed_manifest(saved: dict) -> RunManifest:
    """核验与派生保存来源，返回同用户轮次的新尝试清单；不执行模型。"""
    history, store = saved["history"], saved["store"]
    checkpoint = store.verify(history)
    identifier = uuid4()
    review = store.prepare_resume(saved["preset"], history, checkpoint, identifier)
    return history.manifest.model_copy(update={
        "run_id": identifier, "parent_run_id": history.manifest.run_id, "attempt_index": 2,
        "history_source": None,
        "resume_source": ResumeSource(source_run_id=history.manifest.run_id,
                                     checkpoint_id=checkpoint.checkpoint_id, checkpoint_sha256=checkpoint.sha256),
        "task": history.manifest.task.model_copy(update={"workspace": Path(review.worktree)}),
    }, deep=True)


def test_resume_preserves_code_and_messages_without_replaying_command(saved_source: dict) -> None:
    """中断后恢复继承完整代码和配对历史；旧命令只执行一次，原记录和审阅补丁均不改。"""
    saved = saved_source
    history, root = saved["history"], saved["root"]
    original_dir = root / str(history.manifest.run_id)
    original = {name: hashlib.sha256((original_dir / name).read_bytes()).hexdigest() for name in ("run.json", "events.jsonl", "summary.json")}
    saved["manager"].capture(history.manifest.run_id)
    review_patch = saved["manager"].patch_path(history.manifest.run_id).read_bytes()
    manifest = resumed_manifest(saved)
    model = FinalModel()
    outcome = asyncio.run(run_task(manifest, root, "fixture", model_client=model, on_checkpoint=saved["capture"]))
    assert outcome.result.execution_status == "completed"
    assert outcome.result.model_turns == 1 and outcome.result.tool_calls == 0
    assert outcome.result.resume_budget.model_turns == 2 and outcome.result.resume_budget.tool_calls == 3
    assert outcome.result.resume_budget.usage_complete is False
    assert manifest.turn_index == history.manifest.turn_index and manifest.attempt_index == 2
    assert (manifest.task.workspace / "count.txt").read_text() == "1"
    assert (manifest.task.workspace / "app.txt").read_text() == "fixed\n"
    assert sum(message.get("content") == manifest.task.prompt for message in model.messages[0]) == 1
    assert str(manifest.task.workspace) in model.messages[0][0]["content"]
    assert str(history.manifest.task.workspace) not in model.messages[0][0]["content"]
    assert saved["manager"].patch_path(history.manifest.run_id).read_bytes() == review_patch
    assert {name: hashlib.sha256((original_dir / name).read_bytes()).hexdigest() for name in original} == original
    restored = RunStore.read(root, str(manifest.run_id))
    assert len([event for event in restored.events if event.type == "resume_loaded"]) == 1
    assert not restored.warnings and not restored.interrupted


def test_resume_reference_authorization_is_exact_not_entire_interrupted_owner(saved_source: dict) -> None:
    """恢复可读封存的外置大输出，但不能读取同一中断owner中新写入的未封存对象。"""
    saved = saved_source
    manifest = resumed_manifest(saved)
    run = RunStore.create(saved["root"], manifest)
    archive = ContextArchive(run.run_dir, manifest)
    references = saved["checkpoint"].references
    assert references
    archive.bind_checkpoint_references(references)
    assert archive.read(ReadContextArguments(reference=references[0]))["size_bytes"] > 0
    source_archive = ContextArchive(saved["root"] / str(saved["history"].manifest.run_id), saved["history"].manifest)
    extra = source_archive.save("没有在检查点发布的内容")
    with pytest.raises(WorkspaceError, match="只能读取"):
        archive.load(extra)


def test_resume_refuses_worktree_modified_after_checkpoint(saved_source: dict) -> None:
    """静态页面读取不触发Git；恢复POST发现人工代码变化明确拒绝并保留现场。"""
    saved = saved_source
    path = saved["history"].manifest.task.workspace / "app.txt"
    path.write_text("manual change", encoding="utf-8")
    assert saved["store"].check_readiness(saved["history"], saved["checkpoint"]).checkpoint_id == saved["checkpoint"].checkpoint_id
    with pytest.raises(CheckpointError) as error:
        saved["store"].verify(saved["history"])
    assert error.value.code == "checkpoint_workspace_changed"
    assert path.read_text() == "manual change"


def test_resume_rejects_damaged_patch_without_falling_back(saved_source: dict) -> None:
    """最新已发布检查点损坏明确失败，不能静默退回可能重放副作用的旧检查点。"""
    saved = saved_source
    checkpoint = saved["checkpoint"]
    folder = saved["root"] / str(checkpoint.run_id) / "checkpoints"
    (folder / (str(checkpoint.checkpoint_id) + ".patch")).write_bytes(b"bad")
    with pytest.raises(CheckpointError) as error:
        saved["store"].latest(saved["history"])
    assert error.value.code == "checkpoint_invalid"


def test_resume_counts_unanswered_request_and_cannot_reset_model_budget(saved_source: dict) -> None:
    """封存后已发起但失败的模型请求计入新尝试；累计模型上限耗尽时拒绝恢复。"""
    saved = saved_source
    history = deepcopy(saved["history"])
    history.manifest.config.limits.max_model_turns = 2
    with pytest.raises(CheckpointError) as error:
        saved["store"].check_readiness(history, saved["checkpoint"])
    assert error.value.code == "checkpoint_budget_exhausted"


def test_resume_refuses_any_tool_started_after_checkpoint(saved_source: dict) -> None:
    """即使工具结果随后已配齐也拒绝旧检查点，避免回退后让Agent再次调度同一副作用。"""
    saved = saved_source
    history = deepcopy(saved["history"])
    from agent_harness.schemas import RunEvent
    history.events.append(RunEvent(run_id=history.manifest.run_id, seq=history.events[-1].seq+1,
                                   elapsed_ms=history.events[-1].elapsed_ms, type="tool_started",
                                   payload={"call_id": "uncertain", "name": "run_command"}))
    with pytest.raises(CheckpointError) as error:
        saved["store"].check_readiness(history, saved["checkpoint"])
    assert error.value.code == "checkpoint_side_effect_uncertain"


@pytest.mark.parametrize("saved_source", ["summary"], indirect=True)
def test_restored_summary_is_inherited_by_next_user_round(saved_source: dict) -> None:
    """实际摘要封存→恢复→追加下一用户轮仍保留摘要与原文回读，且中断owner未整目录授权。"""
    from agent_harness.schemas import HistorySource
    from agent_harness.sessions import history_messages, payload_digest

    saved = saved_source
    assert saved["checkpoint"].projection["summaries"]
    manifest = resumed_manifest(saved)
    asyncio.run(run_task(manifest, saved["root"], "fixture", model_client=FinalModel(), on_checkpoint=saved["capture"]))
    parent = RunStore.read(saved["root"], str(manifest.run_id))
    assert parent.result.execution_status == "completed"
    assert any(event.type == "context_action" and event.payload.get("reason") == "resumed" for event in parent.events)
    saved["manager"].capture(manifest.run_id)
    identifier = uuid4()
    review = saved["manager"].prepare_followup(saved["preset"], manifest.run_id, identifier)
    inherited = history_messages(parent)
    child = RunManifest(
        run_id=identifier, session_id=manifest.session_id, parent_run_id=manifest.run_id, turn_index=2,
        origin=manifest.origin, config=manifest.config,
        task=manifest.task.model_copy(update={"workspace": Path(review.worktree), "prompt": "补充确认结果"}),
        history_source=HistorySource(last_seq=parent.events[-1].seq, messages_sha256=payload_digest(inherited)),
    )
    model = FinalModel()
    outcome = asyncio.run(run_task(child, saved["root"], "fixture", model_client=model))
    assert outcome.result.execution_status == "completed"
    history = RunStore.read(saved["root"], str(identifier))
    assert any(event.type == "context_action" and event.payload.get("kind") == "summary"
               and event.payload.get("reason") == "inherited" for event in history.events)
    archive = ContextArchive(saved["root"] / str(identifier), child)
    reference = saved["checkpoint"].references[0]
    assert archive.read(ReadContextArguments(reference=reference))["size_bytes"] > 0
    source_archive = ContextArchive(saved["root"] / str(saved["history"].manifest.run_id), saved["history"].manifest)
    extra = source_archive.save("未在原检查点封存的另一个对象")
    with pytest.raises(WorkspaceError):
        archive.load(extra)


@pytest.mark.parametrize("saved_source", ["crlf"], indirect=True)
def test_resume_honors_git_crlf_checkout_policy(saved_source: dict) -> None:
    """宿主autocrlf=true时从CRLF基线应用标准补丁，保留改动和命令计数，不修改Git配置。"""
    saved = saved_source
    manifest = resumed_manifest(saved)
    assert (manifest.task.workspace / "app.txt").read_text() == "fixed\n"
    assert (manifest.task.workspace / "count.txt").read_text() == "1"
    assert git(saved["preset"].path, "config", "core.autocrlf").strip() == b"true"
