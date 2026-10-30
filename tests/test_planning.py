"""规划授权边界的真实工具回归；测试副作用、目录身份与存储失败，不依赖模型自评。"""

import asyncio
import json
from pathlib import Path

import pytest

from agent_harness.planning import (PlanningController, PlanningError, SubmitPlanArguments,
                                    continue_planning)
from agent_harness.schemas import HumanDecision, PlanningState, RunConfig
from agent_harness.store import StorageError
from agent_harness.tools.registry import ToolExecutor
from agent_harness.tools.listing import ListFilesArguments, list_files


def submitted(workspace: Path) -> PlanningState:
    """在workspace提交最小完整方案并返回状态；只写内存事件列表，不写项目文件。"""
    events = []  # 本测试观察宿主状态事件，不作为用户批准。

    def emit(kind: str, payload: dict) -> None:
        """将kind/payload固定到测试观察列表；返回None，无项目副作用。"""
        events.append((kind, payload))

    controller = PlanningController(PlanningState(phase="planning"), workspace, emit)
    controller.execute("submit_plan", SubmitPlanArguments(title="新功能", summary="实现可验收功能", scope=["result.txt"], steps=["交付"], verification=["核对结果"]))
    return controller.state


def test_approval_rejects_replaced_workspace_and_wrong_action(tmp_path: Path) -> None:
    """批准绑定实际目录对象与当前版本；同名替换或用answer冒充批准均拒绝，无文件修改。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = submitted(workspace)
    pending = state.pending
    wrong = HumanDecision(interaction_id=pending.id, action="answer", plan_version=1)
    with pytest.raises(PlanningError, match="不一致"):
        continue_planning(state, wrong, "同意", workspace)
    workspace.rename(tmp_path / "original-workspace")
    workspace.mkdir()
    approval = HumanDecision(interaction_id=pending.id, action="approve", plan_version=1)
    with pytest.raises(PlanningError, match="工作区"):
        continue_planning(state, approval, "批准", workspace)
    assert state.plans[-1].status == "pending" and not list(workspace.iterdir())


def test_tools_cannot_forge_approval_or_run_commands_in_planning(tmp_path: Path) -> None:
    """通过真实分发器尝试批准工具与命令；宿主拒绝且不产生目标文件。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = tmp_path / "records"
    runs.mkdir()
    executor = ToolExecutor(workspace, runs, RunConfig(tools=["write_file", "run_command"]))

    def emit(kind: str, payload: dict) -> None:
        """本场景不依赖事件内容；kind/payload不产生文件副作用，返回None。"""

    executor.bind_planning(PlanningController(PlanningState(phase="planning"), workspace, emit))

    async def scenario():
        """检查模型不能调用批准入口，且把命令包装成读取也不能绕过只读检查。"""
        forged = await executor.execute("approve_plan", '{"version":1}')
        assert forged["error"]["code"] == "unknown_tool"
        command = await executor.execute("run_command", json.dumps({"argv": ["python", "-c", "print('read only')"]}))
        assert command["error"]["code"] == "planning_read_only"
        write = await executor.execute("write_file", '{"path":"result.txt","content":"bad"}')
        assert write["error"]["code"] == "planning_read_only"
    asyncio.run(scenario())
    assert not list(workspace.iterdir())


def test_failed_state_event_does_not_publish_pending_plan(tmp_path: Path) -> None:
    """状态事件保存失败时不发布半份计划或权限变化；异常传播使Runner停止后续工具批次。"""
    def fail_emit(kind: str, payload: dict) -> None:
        """模拟kind/payload提交失败；始终抛StorageError，不修改规划状态或项目。"""
        raise StorageError("测试磁盘写入失败")

    controller = PlanningController(PlanningState(phase="planning"), tmp_path, fail_emit)
    with pytest.raises(StorageError):
        controller.execute("submit_plan", SubmitPlanArguments(title="功能", summary="目标架构", scope=["result.txt"], steps=["交付"], verification=["检查"]))
    assert controller.state.plans == [] and controller.state.pending is None


def test_directory_listing_pages_and_protects_records(tmp_path: Path) -> None:
    """只读发现支持稳定名称分页及子目录；记录目录和越界路径不能通过目录工具访问。"""
    root = tmp_path / "workspace"
    root.mkdir()
    records = root / "records"
    records.mkdir()
    (records / "secret.json").write_text("protected")
    (root / "a.txt").write_text("a")
    (root / "b").mkdir()
    (root / "b/child.txt").write_text("child")
    first = list_files(root, records, ListFilesArguments(limit=1))
    assert first["entries"] == [{"path": "a.txt", "kind": "file"}] and first["next_offset"] == 1
    assert first["skipped"] == 1 and first["truncated"]
    second = list_files(root, records, ListFilesArguments(offset=1))
    assert second["entries"] == [{"path": "b", "kind": "directory"}]
    assert second["next_offset"] is None
    child = list_files(root, records, ListFilesArguments(directory="b"))
    assert child["entries"] == [{"path": "b/child.txt", "kind": "file"}]
    from agent_harness.workspace import WorkspaceError
    with pytest.raises(WorkspaceError):
        list_files(root, records, ListFilesArguments(directory="records"))
    with pytest.raises(WorkspaceError):
        list_files(root, records, ListFilesArguments(directory=".."))
