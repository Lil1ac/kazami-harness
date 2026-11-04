"""固定成果的三方集成、目标状态校验、逐文件写回与受保护撤销；不调用模型。"""

from __future__ import annotations

import difflib
import json
import os
import shutil
from pathlib import Path
import threading
from uuid import UUID, uuid4

from agent_harness.permissions.policy import roots, path_allowed
from agent_harness.projects.integration_files import IntegrationFiles, PROTECTED, checked_path, entry, fingerprint, git, identity, normalized, read_file, text
from agent_harness.projects.manager import ProjectManager
from agent_harness.storage.sessions import payload_digest
from agent_harness.work.tasks import TaskError, WorkStore, encode, now


class IntegrationStore:
    """复用工作数据库与事件表保存集成事实；文件内容和回执保存在独占证据目录。"""

    def __init__(self, projects: ProjectManager):
        """绑定 projects 的记录根和临时目录；构造不创建磁盘内容。"""
        self.projects = projects  # 已有工作树与固定补丁管理器。
        self.work = WorkStore(projects.runs_root)  # 与队列及权限共用事务事实源。
        self.root = projects.runs_root / ".integrations"  # 固定来源、目标备份和写入回执。
        self.candidates = projects.worktrees_root / ".integrations"  # 仅用于本机检查的独立目录。
        self.lock = threading.RLock()  # 保护同进程的版本检查、解决结果和写回准备。

    def directory(self, identifier: str) -> Path:
        """返回标准UUID identifier 的证据目录；记录根或目录被链接替换则拒绝。"""
        path = self.root / str(UUID(identifier))
        if path.resolve() != path or self.root.resolve() != self.root:
            raise TaskError("integration_evidence_invalid", "集成记录目录身份发生变化。")
        return path

    def get(self, identifier: str, db=None) -> dict:
        """读取 identifier 的持久事实副本；db为空时独立事务，不存在抛业务错误。"""
        if db is None:
            with self.work.transaction() as connection:
                return self.get(identifier, connection)
        row = db.execute("SELECT body FROM integrations WHERE id=?", (str(UUID(identifier)),)).fetchone()
        if row is None:
            raise TaskError("integration_not_found", "成果采纳记录不存在。")
        return json.loads(row[0])

    def list(self, session_id: str) -> list[dict]:
        """返回 session_id 的全部集成记录，按创建时间倒序；仅查询，不刷新现场事实。"""
        with self.work.transaction() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM integrations WHERE json_extract(body,'$.session_id')=? ORDER BY rowid DESC", (session_id,))]

    def save(self, value: dict, db=None) -> None:
        """保存 value 并发布版本事件；db为空时独立事务，不修改目标文件。"""
        if db is None:
            with self.work.transaction() as connection:
                self.save(value, connection)
            return
        value["updated_at"] = now()
        db.execute("INSERT OR REPLACE INTO integrations VALUES (?,?)", (value["id"], encode(value)))
        self.work.event(db, value["session_id"], {"type": "integration_updated", "integration_id": value["id"], "revision": value["revision"], "status": value["status"]})

    def create(self, run_id: str, session_id: str, target_id: str, request_id: str, source_run_ids: list[str] | None = None) -> dict:
        """幂等创建指定来源及目标的空集成记录；只登记事实，重试不得更换请求内容。"""
        identifier = str(UUID(request_id))
        digest = payload_digest([run_id, session_id, target_id, *([source_run_ids] if source_run_ids is not None else [])])
        with self.work.transaction() as db:
            old = db.execute("SELECT body FROM integrations WHERE id=?", (identifier,)).fetchone()
            if old:
                value = json.loads(old[0])
                if value["request_digest"] != digest:
                    raise TaskError("request_conflict", "请求编号已用于其他成果或目标。")
                return value
            value = {"id": identifier, "run_id": run_id, "session_id": session_id, "target_id": target_id,
                     "request_digest": digest, "revision": 1, "status": "preparing", "kind": "apply",
                     "created_at": now(), "updated_at": now(), "error": None, "conflicts": [],
                     "checks": [], "files": [], "applied_at": None, "fingerprint": None}
            if source_run_ids is not None:
                value["source_run_ids"] = source_run_ids
            self.save(value, db)
        return value

    def expect(self, identifier: str, revision: int) -> dict:
        """读取并核对 identifier 的 revision；旧页面版本拒绝，不隐式合并决定。"""
        value = self.get(identifier)
        if value["revision"] != revision:
            raise TaskError("integration_stale", "集成版本已经变化，请刷新后重新审阅。")
        return value

    def target(self, root: Path, files: IntegrationFiles) -> dict:
        """固定 root 的仓库身份、HEAD、完整索引及实际源码；不暂存，不包含忽略文件正文。"""
        directory = identity(root)
        common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve()
        if git(root, "ls-files", "--unmerged", "-z"):
            raise TaskError("integration_target_unmerged", "目标暂存区有未解决冲突，请先完成原有操作。")
        if git(root, "config", "--bool", "core.sparseCheckout", allowed=(0, 1)).strip() == b"true":
            raise TaskError("integration_sparse_unsupported", "首版不支持稀疏检出目标。")
        index_path = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
        index = files.save(index_path.read_bytes()) if index_path.exists() else None
        stages = git(root, "ls-files", "--stage", "-z")
        if any(not record.startswith(b"100") for record in stages.split(b"\0") if record):
            raise TaskError("integration_unsupported_file", "目标索引包含链接或子模块，首版不能采纳。")
        names = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
        names += git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD").split(b"\0")
        protected = tuple(path for path in (self.projects.runs_root, self.projects.worktrees_root) if not root.is_relative_to(path))
        manifest = files.scan(root, [name.decode("utf-8") for name in names if name and not any((root / name.decode("utf-8")).is_relative_to(path) for path in protected)])
        value = {"path": str(root), "identity": directory, "repository": str(common), "repository_identity": identity(common),
                 "head": git(root, "rev-parse", "HEAD").decode().strip(),
                 "branch": git(root, "symbolic-ref", "-q", "HEAD", allowed=(0, 1)).decode().strip() or None,
                 "index_path": str(index_path), "index": index, "index_entries": stages.decode("utf-8"), "manifest": manifest}
        value["fingerprint"] = payload_digest(value)
        return value

    def assert_target(self, value: dict) -> dict:
        """重新采集 value 的目标并核对完整指纹；代码、分支、索引或目录变化均拒绝。"""
        files = IntegrationFiles(self.directory(value["id"]))
        current = self.target(Path(value["target"]["path"]), files)
        if current["fingerprint"] != value["target"]["fingerprint"]:
            raise TaskError("integration_target_changed", "目标代码、分支或暂存区已变化，请重新准备集成。")
        return current

    def prepare(self, identifier: str, preset, cancel: threading.Event) -> dict:
        """从固定运行成果与登记 preset 准备独立候选；cancel置位时停止，确认前不写目标。"""
        with self.lock:
            value = self.get(identifier)
            if value["status"] != "preparing":
                return value
            review = self.projects.get(UUID(value["run_id"]))
            if review is None or review.snapshot_status != "ready":
                raise TaskError("integration_source_unavailable", "来源没有完整固定成果快照。")
            self.projects.patch_path(review.run_id)  # 先核对原始补丁，绝不以当前工作树冒充来源。
            owned = self.projects._ownership(review.run_id)
            source_common = Path(owned["common_git_dir"])
            if owned.get("delegation_parent_id"):
                from agent_harness.subagents.workspaces import in_scope, changed_paths
                if (value.get("delegation_parent_id") != owned["delegation_parent_id"]
                        or str(preset.path) != owned["delegation_parent_path"]
                        or identity(preset.path) != owned["delegation_parent_identity"]
                        or any(not in_scope(name, owned["delegation_scope"]) for name in changed_paths(self.projects, review))):
                    raise TaskError("integration_source_scope", "子成果归属、父目录或实际修改范围无法核对。")
            if identity(source_common) != owned["common_identity"]:
                raise TaskError("integration_source_unavailable", "来源仓库身份无法核对。")
            directory = self.directory(identifier)
            directory.mkdir(parents=True, exist_ok=True)
            files = IntegrationFiles(directory)
            target = self.target(preset.path, files)
            if target["repository"] != str(source_common) or target["path"] == review.worktree:
                raise TaskError("integration_target_invalid", "请选择同一仓库内、区别于来源工作树的已登记目标。")
            stream = payload_digest([value["session_id"], *([str(review.session_id), *sorted(value.get("source_run_ids", []))] if value.get("delegation_parent_id") else []), target["repository_identity"], target["identity"], target["path"], target["branch"] or target["head"]])
            with self.work.transaction() as db:
                row = db.execute("SELECT body FROM integration_anchors WHERE id=?", (stream,)).fetchone()
                anchor = json.loads(row[0]) if row else None
            if anchor and anchor["run_id"] == value["run_id"]:
                value.update(status="already_applied", target=target, anchor=anchor, stream=stream)
                self.save(value)
                return value
            if anchor:
                prior = self.get(anchor["integration_id"])
                previous_files = IntegrationFiles(self.directory(prior["id"]))
                base = {name: files.save(previous_files.read(item), item["mode"]) for name, item in prior["source"].items()}
                if review.run_id != UUID(value["run_id"]):
                    raise TaskError("integration_source_unavailable", "来源轮次不一致。")
                from agent_harness.storage.runs import RunStore
                if value.get("delegation_parent_id"):
                    from agent_harness.subagents.service import read_child
                    old_history = read_child(self.work.root, UUID(prior["delegation_parent_id"]), UUID(anchor["run_id"]))
                    history = read_child(self.work.root, UUID(value["delegation_parent_id"]), UUID(value["run_id"]))
                    newer = history.manifest.created_at > old_history.manifest.created_at
                else:
                    old_history = RunStore.read(self.work.root, anchor["run_id"])
                    history = RunStore.read(self.work.root, value["run_id"])
                    newer = history.manifest.turn_index > old_history.manifest.turn_index
                if not newer:
                    raise TaskError("integration_source_older", "来源早于上次已采纳成果，不能隐式倒退采纳。")
            else:
                base = files.git_tree(self.projects.metadata_root, review.base_commit, git_dir=source_common)
            source = files.git_tree(self.projects.metadata_root, owned["snapshot_tree"], git_dir=source_common)
            source_hash, patch_hash = owned["snapshot_tree"], owned["patch_sha256"]
            if len(value.get("sources", [])) > 1:
                from agent_harness.subagents.workspaces import changed_paths, in_scope
                touched = set(changed_paths(self.projects, review))
                hashes = [patch_hash]
                for member in value["sources"]:
                    if member["run_id"] == value["run_id"]:
                        continue
                    other = self.projects.get(UUID(member["run_id"]))
                    if other is None or other.snapshot_status != "ready":
                        raise TaskError("integration_source_unavailable", "组合子成果没有完整固定快照。")
                    self.projects.patch_path(other.run_id)
                    ownership = self.projects._ownership(other.run_id)
                    paths = changed_paths(self.projects, other)
                    if (ownership.get("common_git_dir") != str(source_common)
                            or ownership.get("delegation_parent_id") != member["log_parent_id"]
                            or ownership.get("delegation_parent_path") != str(preset.path)
                            or ownership.get("delegation_parent_identity") != identity(preset.path)
                            or any(not in_scope(name, ownership.get("delegation_scope", [])) for name in paths)):
                        raise TaskError("integration_source_scope", "组合子成果的归属或修改范围无法核对。")
                    overlap = touched.intersection(paths)
                    if overlap:
                        raise TaskError("integration_sources_overlap", "子成果修改路径重叠，请逐项审阅处理：" + "、".join(sorted(overlap)[:10]))
                    additional = files.git_tree(self.projects.metadata_root, ownership["snapshot_tree"], git_dir=source_common)
                    for name in paths:
                        if name in additional:
                            source[name] = additional[name]
                        else:
                            source.pop(name, None)
                    touched.update(paths)
                    hashes.append(ownership["patch_sha256"])
                source_hash, patch_hash = payload_digest(source), payload_digest(hashes)
            source_test = value.get("source_test")
            if source_test is not None:
                digest = fingerprint({name: entry(normalized(files.read(item)), item["mode"]) for name, item in source.items()})
                source_test["applicable"] = source_test.get("code_before") == source_test.get("code_after") == digest
            if cancel.is_set():
                raise TaskError("integration_cancelled", "集成准备已取消，目标代码未修改。")
            candidate, conflicts = files.merge(base, target["manifest"], source)
            workspace = self.candidates / identifier
            if workspace.resolve() != workspace or self.candidates.resolve() != self.candidates:
                raise TaskError("integration_directory_changed", "集成检查目录被替换。")
            files.materialize(workspace, candidate)
            if cancel.is_set():
                raise TaskError("integration_cancelled", "集成准备已取消，目标代码未修改。")
            value.update(target=target, source=source, base=base, candidate=candidate, conflicts=conflicts,
                         workspace=str(workspace), stream=stream, anchor=anchor, target_label=preset.label,
                         expected_anchor=anchor,
                         source_patch_sha256=patch_hash, source_tree=source_hash,
                         test_config={"argv": preset.test_argv, "timeout_seconds": preset.test_timeout_seconds,
                                      "python_executable": preset.python_executable, "python_paths": preset.python_paths},
                         status="conflicts" if conflicts else "ready", revision=value["revision"] + 1)
            self.refresh(value)
            self.assert_target(value)
            self.save(value)
            return value

    def refresh(self, value: dict) -> None:
        """重算 value 的实际目标差异和确认指纹；拒绝忽略文件碰撞、目录替换及受保护路径。"""
        before, after = value["target"]["manifest"], value["candidate"]
        files, changes = IntegrationFiles(self.directory(value["id"])), []
        root = Path(value["target"]["path"])
        for name in sorted(set(before) | set(after)):
            if before.get(name) == after.get(name):
                continue
            path = checked_path(root, name)
            if any(part.lower() in PROTECTED for part in Path(name).parts):
                raise TaskError("integration_protected_path", "不能采纳宿主或Agent的控制目录变更。")
            if path.is_relative_to(self.projects.runs_root):
                raise TaskError("integration_protected_path", "不能采纳到宿主记录目录。")
            if path.is_relative_to(self.projects.worktrees_root):
                owned = self.projects._ownership(UUID(value["run_id"]))
                parent_id = value.get("parent_run_id")
                parent_review = self.projects.get(UUID(parent_id)) if parent_id else None
                if (not value.get("delegation_parent_id") or parent_review is None
                        or Path(parent_review.worktree) != root or owned.get("delegation_parent_path") != str(root)
                        or owned.get("delegation_parent_identity") != identity(root)):
                    raise TaskError("integration_protected_path", "只能采纳回本子任务所属的父工作树。")
                self.projects._owned_worktree(parent_review)
            if (path.exists() and name not in before) or (path.exists() and not path.is_file()):
                raise TaskError("integration_ignored_collision", "采纳路径碰撞目标忽略文件或目录，请保留现场并调整成果路径。")
            old, new = before.get(name), after.get(name)
            raw = files.read(new or old)
            changes.append({"path": name, "status": "added" if old is None else "deleted" if new is None else "modified", "binary": text(raw) is None})
        if len(changes) > self.projects.MAX_CHANGED_FILES or sum(max((after.get(item["path"]) or {}).get("size", 0), (before.get(item["path"]) or {}).get("size", 0)) for item in changes) > self.projects.MAX_CHANGED_BYTES:
            raise TaskError("integration_change_limit", "本次写回超过两千文件或32MiB，未截断处理。")
        value["files"] = changes
        value["fingerprint"] = payload_digest({key: value.get(key) for key in ("source_tree", "source_patch_sha256", "target", "candidate", "conflicts", "revision", "stream", "anchor", "expected_anchor", "test_config", "kind")})

    def resolve(self, identifier: str, revision: int, resolutions: list[dict], request_id: str | None = None) -> dict:
        """按revision应用逐路径resolutions；request_id非空时同内容重试幂等，None用于内部调用。

        仅编辑候选并返回新记录；请求回执随版本同事务保存，旧确认不能批准新结果。
        """
        with self.lock:
            value = self.get(identifier)
            digest = payload_digest([revision, resolutions])
            previous = value.get("resolution_requests", {}).get(request_id) if request_id else None
            if previous:
                if previous != digest:
                    raise TaskError("request_conflict", "冲突解决请求编号已用于不同内容。")
                return value
            value = self.expect(identifier, revision)
            if value["status"] not in {"conflicts", "ready"}:
                raise TaskError("integration_busy", "当前状态不能修改集成结果。")
            self.assert_target(value)
            files = IntegrationFiles(self.directory(identifier))
            seen = set()
            for resolution in resolutions:
                name, choice = resolution["path"], resolution["choice"]
                if name not in value["conflicts"] or name in seen:
                    raise TaskError("integration_resolution_invalid", "只能解决当前列出的冲突，每个路径只能出现一次。")
                seen.add(name)
                item = value["target"]["manifest"].get(name) if choice == "target" else value["source"].get(name) if choice == "source" else None
                if choice == "edit":
                    raw = resolution["content"].encode("utf-8")
                    item = files.save(raw, (value["candidate"].get(name) or {}).get("mode", 0o644))
                path = checked_path(Path(value["workspace"]), name)
                if item is None:
                    value["candidate"].pop(name, None)
                    path.unlink(missing_ok=True)
                else:
                    value["candidate"][name] = item
                    path.parent.mkdir(parents=True, exist_ok=True)
                    ProjectManager._atomic_write(path, files.read(item))
            value["conflicts"] = [name for name in value["conflicts"] if name not in seen]
            value.update(status="conflicts" if value["conflicts"] else "ready", revision=revision + 1)
            self.refresh(value)
            if request_id:
                value.setdefault("resolution_requests", {})[request_id] = digest
            self.save(value)
            return value

    def public(self, value: dict) -> dict:
        """返回前端审阅视图，正文按需读取；排除内部索引、对象和备份清单。"""
        hidden = {"source", "base", "candidate", "request_digest", "resolution_requests", "target", "anchor", "expected_anchor", "stream", "test_config", "target_after"}
        result = {key: item for key, item in value.items() if key not in hidden}
        if value.get("target"):
            result["target"] = {key: value["target"][key] for key in ("path", "repository", "head", "branch", "fingerprint")}
        result["test_config"] = value.get("test_config")
        result["candidate_fingerprint"] = fingerprint(value["candidate"]) if value.get("candidate") is not None else None
        with self.work.transaction() as db:
            row = db.execute("SELECT body FROM jobs WHERE json_extract(body,'$.integration_id')=? ORDER BY rowid DESC LIMIT 1", (value["id"],)).fetchone()
        job = json.loads(row[0]) if row else None
        result["last_job"] = {key: job.get(key) for key in ("id", "status", "operation", "error", "recovery_kind")} if job else None
        return result

    def preview(self, identifier: str, name: str) -> dict:
        """返回固定 name 的三方正文与最终差异；二进制正文为None，绝不从当前目标补读历史。"""
        value = self.get(identifier)
        files = IntegrationFiles(self.directory(identifier))
        result = {}
        for key, manifest in (("base", value.get("base", {})), ("target", value.get("target", {}).get("manifest", {})), ("source", value.get("source", {})), ("candidate", value.get("candidate", {}))):
            item = manifest.get(name)
            raw = files.read(item) if item else None
            result[key] = text(raw) if raw is not None and len(raw) <= 512 * 1024 else None
        result["diff"] = "".join(difflib.unified_diff((result["target"] or "").splitlines(True), (result["candidate"] or "").splitlines(True), fromfile="目标/" + name, tofile="采纳后/" + name))[:512 * 1024]
        return result

    def check_artifact(self, identifier: str, check_id: str, relative: str) -> Path:
        """解析identifier内check_id登记的原始输出relative；仅允许真实检查引用，越界或链接拒绝。"""
        value = self.get(identifier)
        check = next((item for item in value["checks"] if item.get("evidence_id") == str(UUID(check_id))), None)
        if check is None or not any(item["path"] == relative for item in check["result"].get("artifacts", [])):
            raise TaskError("integration_artifact_missing", "这份原始输出未登记于该次检查。")
        root = self.directory(identifier) / ("check-" + str(UUID(check_id)))
        path = checked_path(root, relative)
        if not path.is_file():
            raise TaskError("integration_artifact_missing", "原始检查输出缺失，历史不会读取其他同名文件。")
        return path

    def authorize(self, value: dict, policy) -> None:
        """按当前宿主 policy 检查实际写回目录；人工确认不能绕过禁止写入及保护路径。"""
        if policy.write == "deny":
            raise TaskError("permission_denied", "宿主策略禁止成果写回。")
        root = Path(value["target"]["path"])
        allowed = roots(root, policy.write_roots)
        for change in value["files"]:
            if not path_allowed(checked_path(root, change["path"]), allowed):
                raise TaskError("permission_denied", "成果包含目标授权目录范围之外的文件。")

    def apply(self, identifier: str, revision: int, confirmation: str, policy, cancel: threading.Event) -> dict:
        """核验固定确认后逐文件写回；保留目标索引，部分写入只标待核对，不自动回滚。"""
        with self.lock:
            value = self.expect(identifier, revision)
            if value["status"] != "ready" or value["conflicts"] or value["fingerprint"] != confirmation:
                raise TaskError("integration_stale", "确认内容不匹配或仍有未解决冲突。")
            self.authorize(value, policy)
            self.assert_target(value)
            with self.work.transaction() as db:
                row = db.execute("SELECT body FROM integration_anchors WHERE id=?", (value["stream"],)).fetchone()
                anchor = json.loads(row[0]) if row else None
            if anchor != value["expected_anchor"]:
                raise TaskError("integration_stale", "增量采纳基线已变化，请重新准备。")
            checks = [item for item in value["checks"] if item["scope"] == "candidate" and item["code_fingerprint"] == fingerprint(value["candidate"])]
            if value["kind"] == "apply" and value["test_config"]["argv"] and (not checks or checks[-1]["result"]["status"] != "passed" or not checks[-1]["applicable"]):
                raise TaskError("integration_check_required", "请先让当前集成候选通过已配置的项目测试。")
            root, files = Path(value["target"]["path"]), IntegrationFiles(self.directory(identifier))
            value.update(status="applying", revision=revision + 1)
            self.save(value)
            journal = {"id": identifier, "confirmation": confirmation, "before": value["target"],
                       "after": value["candidate"], "files": [{**item, "state": "pending"} for item in value["files"]], "complete": False}
            self.journal(journal)
            try:
                for operation in journal["files"]:
                    if cancel.is_set():
                        raise TaskError("integration_cancelled", "写回已在文件边界停止，请核对已写内容。")
                    self.assert_index(value)
                    name = operation["path"]
                    path = checked_path(root, name)
                    expected = value["target"]["manifest"].get(name)
                    actual = read_file(path)[0] if path.exists() else None
                    if actual != expected:
                        raise TaskError("integration_target_changed", "写回期间目标文件发生变化，已停止并保留现场。")
                    operation["state"] = "intent"
                    self.journal(journal)  # 先持久记录意图，再执行不可与SQLite原子提交的文件副作用。
                    item = value["candidate"].get(name)
                    if item is None:
                        path.unlink()
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        ProjectManager._atomic_write(path, files.read(item))
                        if os.name != "nt":
                            path.chmod(item["mode"])
                    operation["state"] = "written"
                    self.journal(journal)
                self.assert_index(value)
                after = self.target(root, files)
                if after["manifest"] != value["candidate"]:
                    raise TaskError("integration_target_changed", "写回后目标包含未审阅变化，请核对现场。")
                journal.update(complete=True, completed_at=now(), target_after=after)
                self.journal(journal)
                self.finish(value, journal)
            except BaseException as error:
                untouched = all(item["state"] == "pending" for item in journal["files"])
                value.update(status="cancelled" if untouched and getattr(error, "code", None) == "integration_cancelled" else "failed" if untouched else "needs_review",
                             error="确认没有开始写文件，原操作已停止。" if untouched else "写回未获得完整回执；保留备份与现场，请先核对，不会自动重放。")
                self.save(value)
                raise
            return value

    def assert_index(self, value: dict) -> None:
        """核对目标目录、分支、HEAD与原始索引；任何外部暂存或提交变化都停止写回。"""
        target = value["target"]
        root = Path(target["path"])
        path = Path(target["index_path"])
        current = read_file(path)[0] if path.exists() else None
        if (identity(root) != target["identity"] or current != target["index"]
                or git(root, "rev-parse", "HEAD").decode().strip() != target["head"]
                or (git(root, "symbolic-ref", "-q", "HEAD", allowed=(0, 1)).decode().strip() or None) != target["branch"]):
            raise TaskError("integration_target_changed", "目标分支、目录或暂存区已变化，已停止写回。")

    def journal(self, journal: dict) -> None:
        """将逐文件 journal 同目录原子持久化；完成标记先于数据库状态发布。"""
        ProjectManager._write_json(self.directory(journal["id"]) / "write-journal.json", journal)

    def finish(self, value: dict, journal: dict) -> None:
        """按可信完整 journal 原子发布采纳事实和增量锚点；测试通过与否不改变写回事实。"""
        value.update(status="applied", applied_at=journal["completed_at"], target_after=journal["target_after"], error=None)
        anchor = {"integration_id": value["id"], "run_id": value["run_id"]} if value["kind"] == "apply" else value["anchor"]
        with self.work.transaction() as db:
            if anchor:
                db.execute("INSERT OR REPLACE INTO integration_anchors VALUES (?,?)", (value["stream"], encode(anchor)))
            else:
                db.execute("DELETE FROM integration_anchors WHERE id=?", (value["stream"],))
            if value["kind"] == "undo":
                previous = self.get(value["undo_of"], db)
                previous.update(status="undone", revision=previous["revision"] + 1, undone_by=value["id"])
                self.save(previous, db)
            self.save(value, db)

    def reconcile(self, identifier: str, confirm: bool = False) -> dict:
        """只读核对 identifier 的逐文件现场；confirm仅接受所有预期字节已存在，不重放写入。"""
        with self.lock:
            value = self.get(identifier)
            path = self.directory(identifier) / "write-journal.json"
            if path.resolve() != path:
                raise TaskError("integration_evidence_invalid", "写回日志被替换，不能自动核对。")
            if not path.exists():
                value.update(status="failed", error="没有写回日志，原作业未自动重放；请重新准备。")
                self.save(value)
                return value
            journal = json.loads(path.read_text(encoding="utf-8"))
            observations = []
            for operation in journal["files"]:
                name = operation["path"]
                target = checked_path(Path(value["target"]["path"]), name)
                actual = read_file(target)[0] if target.exists() else None
                observations.append({"path": name, "state": "after" if actual == journal["after"].get(name) else "before" if actual == journal["before"]["manifest"].get(name) else "external"})
            value["observations"] = observations
            if journal["complete"]:
                self.finish(value, journal)  # 已持久完成标记可补记SQLite事实，不根据现场猜测旧执行结果。
            elif confirm:
                self.assert_index(value)
                current = self.target(Path(value["target"]["path"]), IntegrationFiles(self.directory(identifier)))
                if current["manifest"] != journal["after"]:
                    raise TaskError("integration_reconcile_incomplete", "当前代码尚未完整达到确认结果；请保留现场并重新准备集成。")
                journal.update(complete=True, completed_at=now(), target_after=current, reconciled_by_user=True)
                self.journal(journal)
                self.finish(value, journal)
            else:
                value.update(status="needs_review", error="缺少完整执行回执，请按逐文件核对结果确认或放弃原操作。")
                self.save(value)
            return value

    def cleanup_files(self, session_id: str) -> None:
        """用户彻底删除session_id时清理其专属证据与候选；越界、链接或未对账状态阻止删除。"""
        values = self.list(session_id)
        if any(value["status"] in {"preparing", "applying", "needs_review"} for value in values):
            raise TaskError("integration_needs_review", "请先完成或核对成果采纳，再彻底删除会话。")
        paths = []
        for value in values:
            for root in (self.root, self.candidates):
                path = root / str(UUID(value["id"]))
                if (root.resolve() != root or path.resolve() != path or path == root or not path.is_relative_to(root)
                        or (path.is_dir() and any(item.is_symlink() or item.is_junction() for item in path.rglob("*")))):
                    raise TaskError("integration_evidence_invalid", "集成证据目录归属无法核对，保留记录。")
                paths.append(path)
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path)

    def cleanup_records(self, session_id: str, db) -> None:
        """在彻底删除事务db中移除session_id集成记录及锚点；文件已先清理，不触碰项目代码。"""
        rows = db.execute("SELECT id FROM integrations WHERE json_extract(body,'$.session_id')=?", (session_id,)).fetchall()
        for row in rows:
            db.execute("DELETE FROM integration_anchors WHERE json_extract(body,'$.integration_id')=?", (row[0],))
            db.execute("DELETE FROM operations WHERE substr(id,1,?)=?", (len("integration:" + row[0] + ":"), "integration:" + row[0] + ":"))
        db.execute("DELETE FROM integrations WHERE json_extract(body,'$.session_id')=?", (session_id,))

    def undo(self, original_id: str, request_id: str, preset, cancel: threading.Event) -> dict:
        """为最近一次有效采纳建立反向候选；相关文件、HEAD或索引已变化则拒绝，不修改目标。"""
        original = self.get(original_id)
        if original["status"] != "applied" or original["kind"] != "apply":
            raise TaskError("integration_undo_unavailable", "该记录不是可以撤销的有效采纳。")
        with self.work.transaction() as db:
            records = [json.loads(row[0]) for row in db.execute("SELECT body FROM integrations WHERE json_extract(body,'$.status')='applied' ORDER BY rowid DESC")]
        latest = next((item for item in records if item["kind"] == "apply" and item.get("target", {}).get("path") == original["target"]["path"]), None)
        if latest is None or latest["id"] != original_id:
            raise TaskError("integration_undo_order", "请先撤销目标最近一次采纳，不能跳过后续成果。")
        value = self.create(original["run_id"], original["session_id"], original["target_id"], request_id, original.get("source_run_ids"))
        for key in ("parent_run_id", "delegation_parent_id", "sources"):
            if key in original:
                value[key] = original[key]
        if value["status"] != "preparing":
            return value
        value.update(kind="undo", undo_of=original_id, target_label=preset.label)
        try:
            return self._prepare_undo(original, value, preset, cancel)
        except BaseException as error:
            # 反向候选失败也必须收尾，不能留下永久“准备中”阻止用户重试或删除会话。
            value.update(status="cancelled" if getattr(error, "code", None) == "integration_cancelled" else "failed",
                         error=getattr(error, "message", "撤销预览未完成，目标代码未修改。"),
                         revision=value["revision"] + 1)
            self.save(value)
            raise

    def _prepare_undo(self, original: dict, value: dict, preset, cancel: threading.Event) -> dict:
        """为original在value专属目录构造反向候选并返回；preset为当前目标，cancel置位停止。

        仅物化候选和证据，不写目标；失败由调用方保存终态，原采纳及增量锚点保留。
        """
        original_id = original["id"]
        directory = self.directory(value["id"])
        directory.mkdir(parents=True, exist_ok=True)
        files, prior = IntegrationFiles(directory), IntegrationFiles(self.directory(original_id))
        current = self.target(preset.path, files)
        after = original["target_after"]
        if any(current[key] != after[key] for key in ("identity", "repository_identity", "head", "branch", "index")):
            raise TaskError("integration_undo_changed", "采纳后分支或暂存区已变化，请手动审阅反向修改。")
        candidate = dict(current["manifest"])
        for change in original["files"]:
            name = change["path"]
            if current["manifest"].get(name) != original["candidate"].get(name):
                raise TaskError("integration_undo_changed", "采纳文件已有后续编辑，受保护撤销不会覆盖。")
            item = original["target"]["manifest"].get(name)
            if item is None:
                candidate.pop(name, None)
            else:
                candidate[name] = files.save(prior.read(item), item["mode"])
        if cancel.is_set():
            raise TaskError("integration_cancelled", "撤销预览已取消。")
        workspace = self.candidates / value["id"]
        files.materialize(workspace, candidate)
        value.update(kind="undo", undo_of=original_id, target=current, candidate=candidate,
                     base=current["manifest"], source=candidate, anchor=original["anchor"], stream=original["stream"],
                     expected_anchor={"integration_id": original_id, "run_id": original["run_id"]},
                     workspace=str(workspace), target_label=preset.label,
                     test_config={"argv": preset.test_argv, "timeout_seconds": preset.test_timeout_seconds,
                                  "python_executable": preset.python_executable, "python_paths": preset.python_paths},
                     source_tree=original["source_tree"], source_patch_sha256=original["source_patch_sha256"],
                     status="ready", revision=value["revision"] + 1)
        self.refresh(value)
        self.save(value)
        return value
