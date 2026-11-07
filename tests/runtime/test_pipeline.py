"""通过常规 Runner 入口证明新增策略只需注册，且宿主约束仍会执行。"""

from pathlib import Path

import pytest

from agent_harness.runtime import assembly
from agent_harness.runtime.pipeline import Pipeline, Proceed, Replan, Stage
from agent_harness.runtime.session import Session
from agent_harness.storage.sessions import history_messages
from tests.runtime.test_auto_repair import repair_manifest
from tests.runtime.test_runner import manifest, response, run_script


def test_register_input_and_completion_without_changing_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """在 tmp_path 注册两项策略，检查输入、候选重决策、真实验收与事件顺序；不修改 Loop。"""
    context_text = "[宿主示例] 本项目用 UTF-8 交付。"
    retry_text = "[宿主示例] 请补充交付摘要。"

    async def add_context(session: Session) -> Proceed:
        """将固定上下文补入 session 首次输入并保存，后续轮次继续使用原历史。"""
        if session.context.result.model_turns == 0:
            session.append({"role": "user", "content": context_text}, "pipeline_feedback")
        return Proceed()

    async def check_candidate(session: Session) -> Proceed | Replan:
        """对 session 的初次答复请求补充；已补充时允许宿主继续独立验收。"""
        return Replan(retry_text) if session.context.result.model_turns == 1 else Proceed()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "answer.txt").write_text("good", encoding="utf-8")
    input_step = assembly.component("test.input", Stage.INPUT, add_context, writes=("messages",))
    completion_step = assembly.component("test.completion", Stage.BEFORE_FINISH, check_candidate,
                                         after=("hooks.stop",), reads=("candidate",))
    monkeypatch.setattr(assembly, "COMPONENTS", (input_step, *assembly.COMPONENTS, completion_step))
    value = repair_manifest(workspace, enabled=False)
    outcome, requests, history = run_script(value, tmp_path / "records",
                                            [response(content="候选答复"), response(content="补充后的交付")])
    assert outcome.result.final_answer == "补充后的交付"
    assert outcome.result.validation_status == "passed"
    assert len(outcome.result.verification_attempts) == 1
    assert requests[0]["messages"][-1]["content"] == context_text
    assert requests[1]["messages"][-1]["content"] == retry_text
    assert history_messages(history)[-2]["content"] == retry_text
    traces = [item for event in history.events if event.type == "pipeline_stage" for item in event.payload["components"]]
    assert any(item["name"] == "test.input" and item["source"] == __name__ for item in traces)
    assert [item["outcome"] for item in traces if item["name"] == "test.completion"] == ["replan", "proceed"]
    assert [item["outcome"] for item in traces if item["name"] == "completion.verification"] == ["proceed"]


def test_final_input_budget_remeasures_extended_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """在 tmp_path 模拟组件改变投影却保留旧计量，网关在发送前按实际请求拒绝。"""
    async def enlarge_projection(session: Session) -> Proceed:
        """向 session 的已计量消息追加超限正文，模拟可变输入被扩展后沿用旧统计。"""
        assert session.turn.input is not None
        session.turn.input.messages.append({"role": "user", "content": "x" * (2 * 1024 * 1024)})
        return Proceed()

    entry = assembly.component("test.oversized", Stage.INPUT, enlarge_projection, after=(assembly.HANDOFF.name,),
                               reads=("input",), writes=("input",))
    monkeypatch.setattr(assembly, "COMPONENTS", (*assembly.COMPONENTS, entry))
    outcome, requests, _ = run_script(manifest(tmp_path), tmp_path / "records", [])
    assert outcome.result.stop_reason == "max_model_input_bytes"
    assert not requests and outcome.result.model_turns == 0


def test_assembly_rejects_missing_check_wrong_order_and_workspace_after_verify() -> None:
    """检查必需验收、投影读写顺序和验收后禁写规则，装配阶段拒绝错误。"""
    entries = assembly.COMPONENTS
    with pytest.raises(ValueError, match="缺少必需"):
        Pipeline(tuple(item for item in entries if item is not assembly.ROOT_CHECK), assembly.REQUIRED, "root")
    with pytest.raises(ValueError, match="顺序或读写"):
        Pipeline((assembly.COMPRESS, *(item for item in entries if item is not assembly.COMPRESS)), assembly.REQUIRED, "root")
    invalid = assembly.component("test.write_late", Stage.AFTER_FINISH, assembly.BOUNDARY.run, writes=("workspace",))
    with pytest.raises(ValueError, match="顺序或读写"):
        Pipeline((*entries, invalid), assembly.REQUIRED, "root")
