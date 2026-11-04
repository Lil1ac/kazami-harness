"""开发委派的真实代码基线、独立工作树与可写范围；复用项目快照和三方集成。"""

import os
from pathlib import Path
from uuid import UUID

from agent_harness.projects.integration_files import IntegrationFiles, checked_path, identity, PROTECTED
from agent_harness.projects.integrations import IntegrationStore
from agent_harness.projects.models import ProjectPreset
from agent_harness.work.tasks import TaskError


def scope_paths(values: list[str]) -> list[str]:
    """校验相对路径前缀values并返回统一POSIX形式；空、越界、通配符及控制目录拒绝。"""
    from agent_harness.permissions.models import PermissionPolicy
    PermissionPolicy(write_roots=values)
    paths = [Path(value).as_posix() for value in values]
    if not paths or any(any(part.lower() in PROTECTED for part in Path(value).parts) for value in paths):
        raise TaskError("subagent_scope_invalid", "开发委派必须明确非空的普通文件或目录范围。")
    return list(dict.fromkeys(paths))


def in_scope(name: str, prefixes: list[str]) -> bool:
    """返回相对文件name是否位于任一prefixes内；目录前缀按路径组件匹配，禁止字符串相似误判。"""
    return any(Path(name).is_relative_to(Path(prefix)) for prefix in prefixes)


def intersect_scope(scope: list[str], parent: list[str]) -> list[str]:
    """返回scope与父授权parent的路径交集；无交集拒绝，不借子工作树扩大父权限。"""
    result = []
    for requested in scope:
        for allowed in parent:
            if Path(requested).is_relative_to(Path(allowed)):
                result.append(requested)
            elif Path(allowed).is_relative_to(Path(requested)):
                result.append(allowed)
    if not result:
        raise TaskError("permission_path_denied", "委派范围不属于父级可写目录。")
    return list(dict.fromkeys(result))


def prepare_workspace(projects, parent: Path, child_id: UUID, parent_id: UUID, scope: list[str]):
    """固定parent实际源码并建立child_id工作树，返回审阅记录；parent_id与scope写入宿主归属。

    包含受支持的非忽略未跟踪文件及未提交修改，保留原目录与索引。忽略文件不复制，
    链接、子模块或采集中变化明确拒绝；失败目录保留供核对，不作为成功委派启动。
    """
    store = IntegrationStore(projects)
    preset = ProjectPreset(id="delegation", label="开发子任务", path=parent)
    review = projects.prepare(preset, child_id)
    evidence = projects.metadata_root / str(child_id) / "baseline"
    files = IntegrationFiles(evidence)
    before = store.target(parent, files)
    if before["head"] != review.base_commit:
        raise TaskError("subagent_baseline_changed", "固定子任务起点期间父HEAD变化，未启动模型。")
    workspace = Path(review.worktree)
    # 新工作树只删除其自身由Git检出的普通文件，绝不递归删除父目录或用户路径。
    tracked = projects._git(workspace, ["ls-files", "-z"])
    for raw in tracked.split(b"\0"):
        if raw and (name := raw.decode("utf-8")) not in before["manifest"]:
            checked_path(workspace, name).unlink(missing_ok=True)
    for name, item in before["manifest"].items():
        target = checked_path(workspace, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files.read(item))
        if os.name != "nt":
            target.chmod(item["mode"])
    if store.target(parent, files) != before:
        raise TaskError("subagent_baseline_changed", "建立子任务起点时父代码发生变化，尚未启动子模型。")
    tree = projects._snapshot(review)[0]
    review.base_commit = tree  # Git树即可作为差异基线；不创建提交、不修改来源索引。
    projects._save(review)
    owned = projects._ownership(child_id)
    owned.update(delegation_parent_id=str(parent_id), delegation_scope=scope,
                 delegation_parent_path=str(parent), delegation_parent_identity=identity(parent), baseline_tree=tree)
    projects._write_json(projects._metadata_dir(child_id) / "ownership.json", owned)
    return review


def changed_paths(projects, review) -> list[str]:
    """返回review固定基线到成果树的全部变更路径；重命名同时包含来源与目标，不读取现场。"""
    owned = projects._ownership(review.run_id)
    raw = projects._repository_git(owned, ["diff", "--name-only", "--no-renames", "-z", review.base_commit, owned["snapshot_tree"], "--"])
    return [name.decode("utf-8") for name in raw.split(b"\0") if name]
