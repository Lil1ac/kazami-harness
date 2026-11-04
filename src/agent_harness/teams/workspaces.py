"""团队内部成果汇集：固定依赖基线、逐任务三方合并和不可变代次；父目录只经C13采纳。"""

from pathlib import Path
from uuid import UUID, uuid4
import os

from agent_harness.projects.integration_files import IntegrationFiles, checked_path, identity
from agent_harness.projects.models import ProjectPreset
from agent_harness.subagents.workspaces import prepare_workspace
from agent_harness.work.tasks import TaskError


def initialize(projects, state: dict, parent: Path, parent_id: UUID) -> str:
    """为state首次固定parent实际源码，返回初始工作树编号；保留父目录与真实索引，失败不启动成员。"""
    identifier = UUID(state["stage_run_id"])
    if projects.get(identifier) is None:
        prepare_workspace(projects, parent, identifier, parent_id, ["."])
        projects.capture(identifier)
    projects.verify_workspace(identifier)
    return str(identifier)


def rebuild(projects, state: dict, parent: Path, parent_id: UUID, identifier: UUID) -> str:
    """按state有效ready成果重建已登记identifier代次并返回编号；冲突/来源改变拒绝，不修改父目录。"""
    current_id = UUID(state["stage_run_id"])
    current = projects.verify_workspace(current_id)
    initial = projects._ownership(UUID(state["initial_stage_id"]))
    common = Path(initial["common_git_dir"])
    evidence = projects.metadata_root / ".teams" / state["id"] / uuid4().hex
    files = IntegrationFiles(evidence / "initial")
    initial_tree = files.git_tree(projects.metadata_root, initial["baseline_tree"], git_dir=common)
    raw = {name: (files.read(item), item["mode"]) for name, item in initial_tree.items()}
    ready = sorted((item for item in state["tasks"] if item["state"] == "ready"), key=lambda item: item.get("generation") or 0)
    for index, item in enumerate(ready):
        review = projects.get(UUID(item["run_id"]))
        if review is None or review.snapshot_status != "ready":
            raise TaskError("team_source_unavailable", "依赖成果没有完整固定快照。")
        projects.patch_path(review.run_id)
        owned = projects._ownership(review.run_id)
        if owned.get("team_id") != state["id"] or owned.get("team_task_id") != item["task_id"]:
            raise TaskError("team_source_unavailable", "团队成果归属无法核对。")
        merge = IntegrationFiles(evidence / str(index))
        base = merge.git_tree(projects.metadata_root, review.base_commit, git_dir=common)
        source = merge.git_tree(projects.metadata_root, owned["snapshot_tree"], git_dir=common)
        target = {name: merge.save(data, mode) for name, (data, mode) in raw.items()}
        candidate, conflicts = merge.merge(base, target, source)
        if conflicts:
            raise TaskError("team_merge_conflict", "团队成果冲突，请在当前基线上重新分配处理：" + "、".join(conflicts[:20]))
        raw = {name: (merge.read(value), value["mode"]) for name, value in candidate.items()}
    # 新代次沿用共享工作树，但快照编号始终不同；旧补丁和文件证据不会被覆盖。
    preset = ProjectPreset(id=current.project_id, label="团队内部成果", path=Path(current.repository))
    review = projects.prepare_turn(preset, current_id, identifier)
    workspace = Path(review.worktree)
    old_files = projects._git(workspace, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    for value in old_files.split(b"\0"):
        if value and (name := value.decode("utf-8")) not in raw:
            checked_path(workspace, name).unlink(missing_ok=True)
    for name, (data, mode) in raw.items():
        target = checked_path(workspace, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if os.name != "nt":
            target.chmod(mode)
    owned = projects._ownership(identifier)
    owned.update(team_id=state["id"], delegation_parent_id=str(parent_id),
                 delegation_parent_path=str(parent), delegation_parent_identity=identity(parent),
                 delegation_scope=list(dict.fromkeys(path for item in state["tasks"] for path in item["write_scope"])))
    projects._write_json(projects._metadata_dir(identifier) / "ownership.json", owned)
    projects.capture(identifier)
    return str(identifier)
