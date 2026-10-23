"""为单机任务建立独立工作树，并保存可恢复、可下载的完整代码快照。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from agent_harness.processes import _child_environment, _windows_taskkill

from .models import ChangedFile, ProjectPreset, ProjectReview


class ProjectError(Exception):
    """携带受控错误码和中文消息，不向网页暴露原始 Git 输出。"""

    def __init__(self, code: str, message: str) -> None:
        """保存业务错误码及可展示消息；参数不得包含密钥或原始进程输出。"""
        self.code = code  # 供 API 将受控错误映射为状态码。
        self.message = message  # 可以直接向用户展示的中文描述。
        super().__init__(message)


class ProjectManager:
    """同步管理宿主项目的 detached 工作树；调用方应在线程中调用阻塞方法。"""

    MAX_PATCH_BYTES = 32 * 1024 * 1024  # 完整补丁上限，超限明确失败，绝不截断下载。
    MAX_CHANGED_BYTES = 32 * 1024 * 1024  # 暂存前限制当前变更文件总字节数，减少 Git 对象膨胀。
    MAX_CHANGED_FILES = 2000  # 单个审阅快照最多处理的变更文件数。
    GIT_TIMEOUT_SECONDS = 30.0  # 单条 Git 命令的最长等待时间，单位秒。
    MAX_METADATA_BYTES = 4 * 1024 * 1024  # 读取持久化 JSON 的硬上限。

    def __init__(self, runs_root: Path, worktrees_root: Path) -> None:
        """绑定运行记录根目录与专用工作树根目录；此时不创建目录或启动进程。"""
        self.runs_root = runs_root.resolve()  # Runner 正常记录的根目录。
        self.worktrees_root = worktrees_root.resolve()  # 仅管理其下 UUID 命名的任务工作树。
        self.metadata_root = self.runs_root / ".projects"  # 与 Runner 的 UUID 目录隔离，启动前可创建。
        self._lock = threading.RLock()  # 串行化创建、快照、保留和丢弃，避免线程内竞争。
        self._record_lock = threading.RLock()  # 只保护短元数据读写；视图不等待后台 Git 捕获或删除。
        self._git_program = shutil.which("git")  # 使用宿主 Git；缺失时在请求执行阶段受控报错。
        self._patch_cache: dict[UUID, tuple[tuple[int, int, int, int], str, int]] = {}  # 最近八份补丁的 stat/摘要核验结果，不缓存大文件正文。

    def prepare(self, preset: ProjectPreset, run_id: UUID, base_ref: str = "HEAD") -> ProjectReview:
        """从 preset 的 base_ref（默认 HEAD）固定提交建立 run_id 工作树；不复制源改动。

        返回待审阅记录；创建 Git 工作树和旁路元数据，但不创建 Runner 正常运行目录。
        无效引用、重复标识或不受支持的项目以 ProjectError 拒绝。
        """
        with self._lock:
            run_id = self._uuid(run_id)
            if not base_ref.strip() or len(base_ref) > 512 or "\0" in base_ref:
                raise ProjectError("project_ref_invalid", "基线引用无效。")
            repository, commit_text, dirty = self._inspect_base(preset, base_ref)
            common = Path(self._git(repository, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).decode().strip()).resolve()
            metadata = self._metadata_dir(run_id)
            worktree = self.worktrees_root / str(run_id)
            if metadata.exists() or worktree.exists() or worktree.is_symlink():
                raise ProjectError("project_run_exists", "该运行的项目工作树或审阅记录已经存在。")
            self._safe_mkdir(self.metadata_root)
            self._safe_mkdir(self.worktrees_root)
            metadata.mkdir()
            self._write_json(metadata / "preparing.json", {"run_id": str(run_id), "repository": str(repository)})
            try:
                self._git(repository, ["worktree", "add", "--detach", "--", str(worktree), commit_text])
                owned = {
                    "run_id": str(run_id),
                    "common_git_dir": str(common),
                    "common_identity": self._identity(common),
                    "source_identity": self._identity(repository),
                    "worktree_identity": self._identity(worktree),
                    "git_file": (worktree / ".git").read_text(encoding="utf-8").strip(),
                }
                review = ProjectReview(
                    run_id=run_id, project_id=preset.id, project_label=preset.label,
                    repository=str(repository), base_ref=base_ref, base_commit=commit_text,
                    source_dirty=dirty, worktree=str(worktree),
                )
                self._write_json(metadata / "ownership.json", owned)
                self._save(review)
                (metadata / "preparing.json").unlink(missing_ok=True)
                return review
            except (OSError, UnicodeError) as exc:
                raise ProjectError("project_prepare_failed", "创建项目工作树后无法保存归属记录，请检查磁盘状态。") from exc

    def verify_snapshot(self, run_id: UUID) -> ProjectReview:
        """核对 run_id 工作树仍等于已保存快照，返回审阅；不重新捕获或覆盖父证据。

        已丢弃、快照缺失、补丁损坏、归属改变或代码变化均抛 ProjectError。
        调用方应在线程执行；校验使用现有有界 Git 和完整补丁摘要。
        """
        with self._lock:
            review = self._required(run_id)
            if review.status == "discarded":
                raise ProjectError("project_already_decided", "前轮工作树已经丢弃，不能继续修改。")
            if review.snapshot_status != "ready" or not review.patch_available:
                raise ProjectError("project_snapshot_required", "前轮没有完整代码快照，不能继续。")
            self._verified_patch(review, force=True)
            owned = self._owned_worktree(review)
            if self._snapshot(review)[0] != owned.get("snapshot_tree"):
                raise ProjectError("project_snapshot_stale", "前轮结束后代码发生变化，请核对后新建任务。")
            return review

    def prepare_followup(self, preset: ProjectPreset, parent_id: UUID, run_id: UUID) -> ProjectReview:
        """从父完整累计补丁派生 run_id 独立工作树，返回待审阅记录，父现场保持不变。

        preset 必须仍登记同一原始仓库；先核对父身份与内容，再固定原始提交应用完整
        binary patch，最后核对父子 tree 相同。准备失败保留已登记目录供核对，不启动模型。
        """
        with self._lock:
            parent = self.verify_snapshot(parent_id)
            if preset.id != parent.project_id or preset.path.resolve() != Path(parent.repository).resolve():
                raise ProjectError("project_source_changed", "宿主项目登记与前轮来源不一致。")
            expected = self._ownership(parent_id)["snapshot_tree"]
            patch = self._verified_patch(parent, force=True)
            child = self.prepare(preset, run_id, parent.base_commit)
            child.base_ref = parent.base_ref
            if patch.stat().st_size:
                # 新工作树先按索引原始字节检出，避免LF提交的CRLF检出与--index校验冲突。
                # 仅处理本次新副本；补丁也按原字节应用，防止原提交为CRLF时被再次转换。
                self._git(Path(child.worktree), ["-c", "core.autocrlf=false", "checkout-index", "--all", "--force", "--index"])
                self._git(Path(child.worktree), ["-c", "core.autocrlf=false", "apply", "--index", "--binary", "--whitespace=nowarn", "--", str(patch)])
            if self._snapshot(child)[0] != expected or self._snapshot(parent)[0] != expected:
                raise ProjectError("project_snapshot_stale", "派生期间代码状态变化，未启动新的执行。")
            self._save(child)
            return child

    def checkpoint_snapshot(self, run_id: UUID) -> tuple[str, bytes]:
        """为运行生成独立检查点的tree与完整补丁；不发布或覆盖终态审阅快照。

        run_id须属于本管理器且未丢弃；只使用临时索引，返回固定基线累计补丁。
        调用方负责不可变落盘；归属、容量或Git失败抛ProjectError。
        """
        with self._lock:
            review = self._required(run_id)
            if review.status == "discarded":
                raise ProjectError("project_already_decided", "工作树已经丢弃，无法恢复。")
            self._owned_worktree(review)
            tree, patch, _ = self._snapshot(review)
            return tree, patch

    def verify_checkpoint(self, run_id: UUID, tree: str) -> ProjectReview:
        """核对当前工作树仍等于封存tree；不重写已有审阅补丁，变化抛ProjectError。"""
        with self._lock:
            review = self._required(run_id)
            actual, _ = self.checkpoint_snapshot(run_id)
            if actual != tree:
                raise ProjectError("checkpoint_workspace_changed", "检查点之后代码发生变化，请先人工核对。")
            return review

    def prepare_checkpoint(self, preset: ProjectPreset, parent_id: UUID, run_id: UUID,
                           tree: str, patch: bytes) -> ProjectReview:
        """按封存补丁派生独立工作树，返回待审阅记录；父现场和审阅补丁保持不变。

        tree是捕获时的Git树哈希，patch须已通过检查点SHA核验；来源登记和派生前后
        父子代码均须一致。失败保留准备现场，不自动启动模型或重放工具。
        """
        with self._lock:
            parent = self.verify_checkpoint(parent_id, tree)
            if preset.id != parent.project_id or preset.path.resolve() != Path(parent.repository).resolve():
                raise ProjectError("project_source_changed", "宿主登记项目与检查点来源不一致。")
            child = self.prepare(preset, run_id, parent.base_commit)
            child.base_ref = parent.base_ref
            if patch:
                # 检查点补丁独立于changes.patch，避免覆盖终态代码审阅证据。
                temporary = self._metadata_dir(run_id) / ("resume-" + uuid4().hex + ".patch")
                try:
                    with temporary.open("xb") as stream:
                        stream.write(patch)
                        stream.flush()
                        os.fsync(stream.fileno())
                    self._git(Path(child.worktree), ["apply", "--index",
                              "--binary", "--whitespace=nowarn", "--", str(temporary)])
                finally:
                    temporary.unlink(missing_ok=True)
            if self.checkpoint_snapshot(run_id)[0] != tree or self.checkpoint_snapshot(parent_id)[0] != tree:
                raise ProjectError("checkpoint_workspace_changed", "恢复准备期间代码发生变化，未开始执行。")
            self._save(child)
            return child

    def inspect(self, preset: ProjectPreset) -> dict[str, str | bool]:
        """只读探测 preset 的 HEAD 与源 dirty 状态；返回固定键，不创建目录或刷新索引。

        不存在、非仓库根目录、无 HEAD、含子模块等情况以 ProjectError 拒绝。
        调用方应放在线程中执行；Git 输出与等待时长都遵循本管理器的上限。
        """
        _, commit, dirty = self._inspect_base(preset, "HEAD")
        return {"head_commit": commit, "source_dirty": dirty}

    def _inspect_base(self, preset: ProjectPreset, base_ref: str) -> tuple[Path, str, bool]:
        """校验宿主 Git 根项目并解析 base_ref，返回实体根目录、固定提交与源 dirty 状态。"""
        repository = preset.path.resolve()
        if not repository.is_dir() or self._is_link(preset.path):
            raise ProjectError("project_unavailable", "项目目录不存在或不是可用的实体目录。")
        try:
            top = Path(self._git(repository, ["rev-parse", "--show-toplevel"]).decode().strip()).resolve()
            if top != repository:
                raise ProjectError("project_root_required", "请将项目配置为 Git 仓库根目录。")
            if self._git(repository, ["rev-parse", "--is-bare-repository"]).strip() != b"false":
                raise ProjectError("project_unavailable", "项目必须包含可执行任务的工作目录。")
            commit = self._git(repository, ["rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"])
        except ProjectError as exc:
            if exc.code == "project_git_failed":
                raise ProjectError("project_ref_invalid", "无法解析项目或基线提交，请检查项目配置和引用。") from exc
            raise
        commit_text = commit.decode().strip()
        if len(commit_text) not in (40, 64) or any(ch not in "0123456789abcdef" for ch in commit_text):
            raise ProjectError("project_ref_invalid", "Git 未返回有效的固定提交。")
        stages = self._git(repository, ["ls-tree", "-r", commit_text])
        if any(line.startswith(b"160000 ") for line in stages.splitlines()):
            raise ProjectError("project_submodule_unsupported", "当前版本暂不支持含子模块的项目。")
        dirty = bool(self._git(repository, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]))
        return repository, commit_text, dirty

    def get(self, run_id: UUID) -> ProjectReview | None:
        """读取持久化审阅；不存在返回 None，工作树缺失只加警告，不推断用户已丢弃。"""
        with self._record_lock:
            path = self._metadata_dir(self._uuid(run_id)) / "review.json"
            if not path.exists():
                return None
            try:
                data = self._read_bytes(path, self.MAX_METADATA_BYTES)
                review = ProjectReview.model_validate_json(data)
            except (OSError, ValueError, ValidationError) as exc:
                raise ProjectError("project_record_invalid", "项目审阅记录损坏或无法读取。") from exc
            if review.run_id != self._uuid(run_id):
                raise ProjectError("project_record_invalid", "项目审阅记录与运行标识不一致。")
            if review.status != "discarded":
                try:
                    self._owned_worktree(review, verify_git=False)
                except ProjectError:
                    self._warn(review, "任务工作树缺失或归属发生变化，请检查后再审阅；原有审阅决定未改变。")
            if review.patch_available:
                try:
                    self._verified_patch(review)
                except ProjectError:
                    review.patch_available = False
                    review.snapshot_status = "error"
                    self._warn(review, "补丁文件缺失或完整性检查失败，当前不可下载。")
            return review

    def capture(self, run_id: UUID) -> ProjectReview:
        """为 run_id 固化完整基线补丁；返回审阅记录，失败记录 error 并抛 ProjectError。

        pending 和 kept 工作树可重新捕获且保留审阅决定；discarded 只能返回已有快照。
        补丁包含非忽略新增文件和二进制数据，不改变源目录或任务工作树的真实索引。
        """
        with self._lock:
            review = self._required(run_id)
            if review.status == "discarded":
                if review.snapshot_status == "ready":
                    return review
                raise ProjectError("project_already_decided", "已丢弃的工作树不能重新生成快照。")
            try:
                owned = self._owned_worktree(review)
                tree, patch, files = self._snapshot(review)
                metadata = self._metadata_dir(review.run_id)
                # Git 操作完成才短暂锁住记录发布，SSE 读取不等待整个捕获过程。
                with self._record_lock:
                    self._atomic_write(metadata / "changes.patch", patch)
                    owned["snapshot_tree"] = tree
                    owned["patch_sha256"] = hashlib.sha256(patch).hexdigest()
                    owned["patch_size"] = len(patch)
                    self._write_json(metadata / "ownership.json", owned)
                    review.files = files
                    review.snapshot_status = "ready"
                    review.patch_available = True
                    review.warnings = [message for message in review.warnings if not message.startswith("代码快照未完成")]
                    self._save(review)
                return review
            except ProjectError as exc:
                review.snapshot_status = "error"
                review.patch_available = False
                review.files = []
                self._warn(review, f"代码快照未完成：{exc.message}")
                self._save(review)
                raise
            except (OSError, ValueError, UnicodeError) as exc:
                review.snapshot_status = "error"
                review.patch_available = False
                review.files = []
                self._warn(review, "代码快照未完成：无法读写完整快照，请检查磁盘或文件内容。")
                self._save(review)
                raise ProjectError("project_snapshot_failed", "无法生成完整的代码审阅快照。") from exc

    def decide(self, run_id: UUID, decision: Literal["keep", "discard"]) -> ProjectReview:
        """对 run_id 执行 keep/discard 并返回记录；同一决定幂等，kept 可再 discard。

        丢弃仅由 Git 删除身份匹配的本任务工作树；保留补丁和元数据。
        无完整快照、归属变化或快照后新增改动均以 ProjectError 拒绝，discarded 不可逆。
        """
        with self._lock:
            if decision not in ("keep", "discard"):
                raise ProjectError("project_decision_invalid", "审阅决定只能是保留或丢弃。")
            review = self._required(run_id)
            target = "kept" if decision == "keep" else "discarded"
            if review.status == target:
                return review
            if review.status == "discarded":
                raise ProjectError("project_already_decided", "该任务已经完成审阅，不能更改决定。")
            if review.snapshot_status != "ready" or not review.patch_available:
                raise ProjectError("project_snapshot_required", "请先完成代码快照，再保留或丢弃工作树。")
            self._verified_patch(review, force=True)
            owned = self._owned_worktree(review)
            tree, _, _ = self._snapshot(review)
            if tree != owned.get("snapshot_tree"):
                raise ProjectError("project_snapshot_stale", "工作树在快照后有新改动，已保留；请手动核对。")
            if decision == "discard":
                self._repository_git(owned, ["worktree", "remove", "--force", "--", review.worktree])
                if Path(review.worktree).exists():
                    raise ProjectError("project_discard_failed", "Git 未能完整移除任务工作树，审阅仍待处理。")
            review.status = target
            self._save(review)
            return review

    def patch_path(self, run_id: UUID) -> Path:
        """返回通过摘要和长度校验的完整补丁路径；无可用快照时抛受控异常。"""
        with self._lock:
            return self._verified_patch(self._required(run_id), force=True)

    def _snapshot(self, review: ProjectReview) -> tuple[str, bytes, list[ChangedFile]]:
        """使用独立临时索引收集所有非忽略变更，返回树哈希、完整补丁及变更文件。"""
        worktree = Path(review.worktree)
        status = self._git(worktree, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        # Agent 可能自行提交；仍以启动基线比较，不能仅看当前 HEAD 的 dirty 状态。
        tracked = self._git(worktree, ["diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", review.base_commit, "--"])
        paths = list(dict.fromkeys([
            *self._status_paths(status),
            *(name.decode("utf-8") for name in tracked.split(b"\0") if name),
        ]))
        if len(paths) > self.MAX_CHANGED_FILES:
            raise ProjectError("project_snapshot_limit", "变更文件数量超过快照上限。")
        size = 0
        for name in paths:
            path = worktree / name
            if path.exists() and not path.is_symlink():
                if path.is_file():
                    size += path.stat().st_size
                elif path.is_dir():
                    raise ProjectError("project_snapshot_unsupported", "变更包含未支持的子模块或特殊目录。")
                else:
                    raise ProjectError("project_snapshot_unsupported", "变更包含无法审阅的特殊文件。")
            if size > self.MAX_CHANGED_BYTES:
                raise ProjectError("project_snapshot_limit", "变更文件总大小超过快照上限，未生成截断补丁。")
        metadata = self._metadata_dir(review.run_id)
        index = metadata / f"index-{uuid4().hex}"
        environment = {"GIT_INDEX_FILE": str(index)}
        try:
            self._git(worktree, ["read-tree", review.base_commit], extra_env=environment)
            self._git(worktree, ["add", "--all", "--", "."], extra_env=environment)
            tree = self._git(worktree, ["write-tree"], extra_env=environment).decode().strip()
            patch = self._git(
                worktree,
                ["diff", "--cached", "--binary", "--full-index", "--find-renames",
                 "--no-ext-diff", "--no-textconv", review.base_commit, "--"],
                extra_env=environment, max_bytes=self.MAX_PATCH_BYTES,
            )
            names = self._git(
                worktree,
                ["diff", "--cached", "--name-status", "-z", "--find-renames",
                 "--no-ext-diff", "--no-textconv", review.base_commit, "--"],
                extra_env=environment,
            )
            numbers = self._git(
                worktree,
                ["diff", "--cached", "--numstat", "-z", "--no-renames",
                 "--no-ext-diff", "--no-textconv", review.base_commit, "--"],
                extra_env=environment,
            )
            binaries = {
                parts[2].decode("utf-8")
                for record in numbers.split(b"\0")
                if record and len(parts := record.split(b"\t", 2)) == 3 and parts[0] == b"-"
            }
            return tree, patch, self._changed_files(names, binaries)
        finally:
            index.unlink(missing_ok=True)
            Path(f"{index}.lock").unlink(missing_ok=True)

    def _owned_worktree(self, review: ProjectReview, *, verify_git: bool = True) -> dict:
        """验证工作树实体归属；verify_git 默认核查 Git 注册，False 仅用于高频只读视图。"""
        expected = self.worktrees_root / str(review.run_id)
        worktree = Path(review.worktree)
        if worktree != expected or self._is_link(worktree) or worktree.resolve() != expected:
            raise ProjectError("project_worktree_changed", "任务工作树路径被替换或不属于当前运行。")
        if not worktree.is_dir():
            raise ProjectError("project_worktree_missing", "任务工作树不存在，不能推断它已被安全丢弃。")
        owned = self._ownership(review.run_id)
        try:
            common = Path(owned["common_git_dir"])
            if self._identity(worktree) != owned["worktree_identity"] or self._identity(common) != owned["common_identity"]:
                raise ProjectError("project_worktree_changed", "任务工作树或 Git 仓库身份已改变。")
            git_file = worktree / ".git"
            if self._is_link(git_file) or not git_file.is_file() or git_file.stat().st_size > 8192:
                raise ProjectError("project_worktree_changed", "任务工作树的 Git 归属信息发生变化。")
            if git_file.read_text(encoding="utf-8").strip() != owned["git_file"]:
                raise ProjectError("project_worktree_changed", "任务工作树的 Git 归属信息发生变化。")
            if not verify_git:
                return owned
            registry = self._repository_git(owned, ["worktree", "list", "--porcelain", "-z"])
            registered = [Path(item[9:].decode("utf-8")).resolve() for item in registry.split(b"\0") if item.startswith(b"worktree ")]
            if worktree not in registered or worktree == Path(review.repository):
                raise ProjectError("project_worktree_changed", "Git 注册中找不到本任务独立工作树。")
            actual_common = Path(
                self._git(worktree, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
                .decode().strip()
            ).resolve()
            if actual_common != common:
                raise ProjectError("project_worktree_changed", "任务工作树指向其他 Git 仓库。")
            return owned
        except (KeyError, OSError, UnicodeError, TypeError) as exc:
            raise ProjectError("project_worktree_changed", "无法验证任务工作树的归属，已拒绝修改或删除。") from exc

    def _repository_git(self, owned: dict, argv: list[str]) -> bytes:
        """对已记录的公共 Git 目录调用管理命令，避免依赖已变化的源工作目录。"""
        return self._git(self.metadata_root, [f"--git-dir={owned['common_git_dir']}", *argv])

    def _git(self, cwd: Path, argv: list[str], *, extra_env: dict[str, str] | None = None, max_bytes: int = 4 * 1024 * 1024, _filter_probe: bool = False) -> bytes:
        """非 shell 运行有界 Git；禁用钩子、监视器和外部过滤器，超时清理独立进程组。

        extra_env 仅允许内部临时 GIT_INDEX_FILE；max_bytes 是单流字节上限。
        _filter_probe 仅供读取配置名称时避免递归探测，不能用于文件操作。
        返回完整 stdout；超限、超时、子进程输出未结束等情况一律抛 ProjectError。
        """
        if self._git_program is None:
            raise ProjectError("project_git_unavailable", "宿主未安装可用的 Git。")
        filter_options: list[str] = []  # 单次命令覆盖，不修改源项目或宿主 Git 配置。
        if not _filter_probe:
            names = self._git(cwd, ["config", "--null", "--list", "--name-only"], _filter_probe=True)
            try:
                drivers = {
                    name.rsplit(".", 1)[0]
                    for raw in names.split(b"\0") if raw
                    if (name := raw.decode("utf-8")).startswith("filter.")
                    and name.rsplit(".", 1)[-1] in {"clean", "smudge", "process", "required"}
                }
            except UnicodeError as exc:
                raise ProjectError("project_config_invalid", "Git 过滤器配置名称无法安全读取。") from exc
            for driver in sorted(drivers):
                for suffix, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
                    filter_options.extend(["-c", f"{driver}.{suffix}={value}"])
        command = [
            self._git_program, "--no-pager", "--no-optional-locks",
            "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false", *filter_options, "-C", str(cwd), *argv,
        ]
        # 与工具进程共用凭据隔离；仅补 Git 定位用户配置所需的目录变量。
        environment = _child_environment()
        environment.update({
            key: value for key, value in os.environ.items()
            if key.upper() in {"HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"}
        })
        environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C"})
        if extra_env:
            if set(extra_env) != {"GIT_INDEX_FILE"} or any(not isinstance(value, str) or "\0" in value for value in extra_env.values()):
                raise ProjectError("project_environment_invalid", "Git 仅允许宿主指定独立临时索引。")
            environment.update(extra_env)
        try:
            group_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment, stdin=subprocess.DEVNULL, **group_options,
            )
        except OSError as exc:
            raise ProjectError("project_git_unavailable", "无法启动宿主 Git 程序。") from exc
        output = [bytearray(), bytearray()]
        overflow = threading.Event()
        stop_lock = threading.Lock()  # 超时和双输出超限只触发一次树清理。
        stopped = False

        def stop_tree() -> None:
            """在父进程仍存活时清理本次 Git 的进程组，失败时至少终止父进程。"""
            nonlocal stopped
            with stop_lock:
                if stopped or process.poll() is not None:
                    return
                stopped = True
                try:
                    if os.name == "nt":
                        subprocess.run(
                            [_windows_taskkill(), "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, timeout=5, env=_child_environment(),
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if process.poll() is None:
                    process.kill()

        def collect(stream, position: int) -> None:
            """持续读取单个管道；超过单流字节上限立即停止 Git，防止无界缓存。"""
            try:
                while chunk := stream.read(65536):
                    if len(output[position]) + len(chunk) > max_bytes:
                        overflow.set()
                        stop_tree()
                        return
                    output[position].extend(chunk)
            finally:
                stream.close()

        readers = [threading.Thread(target=collect, args=(process.stdout, 0), daemon=True), threading.Thread(target=collect, args=(process.stderr, 1), daemon=True)]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            process.wait(timeout=self.GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_tree()
            process.wait(timeout=5)
        finally:
            for reader in readers:
                reader.join(timeout=2)
        if any(reader.is_alive() for reader in readers):
            raise ProjectError("project_git_cleanup_unknown", "Git 子进程输出未结束，未确认完整结果或清理状态。")
        if overflow.is_set():
            raise ProjectError("project_snapshot_limit", "Git 输出超过记录上限，未生成截断补丁。")
        if timed_out:
            raise ProjectError("project_git_timeout", "Git 操作超时，请检查项目大小或磁盘状态。")
        if process.returncode:
            raise ProjectError("project_git_failed", "Git 操作失败，请检查项目、引用或工作树状态。")
        return bytes(output[0])

    def _required(self, run_id: UUID) -> ProjectReview:
        """读取必须存在的审阅记录；不存在时抛 project_review_missing。"""
        review = self.get(self._uuid(run_id))
        if review is None:
            raise ProjectError("project_review_missing", "该运行没有项目审阅记录。")
        return review

    def _ownership(self, run_id: UUID) -> dict:
        """读取私有归属记录；要求是对应运行的 JSON 对象，损坏时受控拒绝。"""
        try:
            value = json.loads(self._read_bytes(self._metadata_dir(run_id) / "ownership.json", self.MAX_METADATA_BYTES))
            if not isinstance(value, dict) or value.get("run_id") != str(run_id):
                raise ValueError("归属记录标识不一致")
            return value
        except (OSError, ValueError) as exc:
            raise ProjectError("project_record_invalid", "无法读取项目工作树的归属记录。") from exc

    def _verified_patch(self, review: ProjectReview, *, force: bool = False) -> Path:
        """检查补丁长度与摘要；force 强制重读，普通视图复用最多八份未变文件的核验结果。"""
        if review.snapshot_status != "ready" or not review.patch_available:
            raise ProjectError("project_patch_unavailable", "当前没有完整可下载的代码补丁。")
        path = self._metadata_dir(review.run_id) / "changes.patch"
        owned = self._ownership(review.run_id)
        try:
            if self._is_link(path):
                raise OSError("补丁不能是链接")
            file_stat = path.stat()
            signature = (file_stat.st_dev, file_stat.st_ino, file_stat.st_mtime_ns, file_stat.st_size)
            with self._record_lock:
                cached = self._patch_cache.get(review.run_id)
            if not force and cached == (signature, owned.get("patch_sha256"), owned.get("patch_size")):
                return path
            data = self._read_bytes(path, self.MAX_PATCH_BYTES)
        except OSError as exc:
            raise ProjectError("project_patch_unavailable", "代码补丁文件缺失或无法读取。") from exc
        if len(data) != owned.get("patch_size") or hashlib.sha256(data).hexdigest() != owned.get("patch_sha256"):
            with self._record_lock:
                self._patch_cache.pop(review.run_id, None)
            raise ProjectError("project_patch_unavailable", "代码补丁完整性检查失败。")
        with self._record_lock:
            self._patch_cache.pop(review.run_id, None)
            self._patch_cache[review.run_id] = (signature, owned["patch_sha256"], owned["patch_size"])
            while len(self._patch_cache) > 8:
                self._patch_cache.pop(next(iter(self._patch_cache)))
        return path

    def _metadata_dir(self, run_id: UUID) -> Path:
        """解析仅含标准 UUID 的元数据目录，并拒绝路径经过被替换的链接。"""
        path = self.metadata_root / str(self._uuid(run_id))
        if path.resolve() != path or self._is_link(path) or self._is_link(self.metadata_root):
            raise ProjectError("project_record_invalid", "项目审阅目录不是受管理的实体目录。")
        return path

    def _save(self, review: ProjectReview) -> None:
        """原子写入公开审阅 JSON，不写 Runner 的正常运行目录。"""
        with self._record_lock:
            self._atomic_write(self._metadata_dir(review.run_id) / "review.json", review.model_dump_json(indent=2).encode("utf-8"))

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        """把内部映射原子保存为 UTF-8 JSON；失败保留旧文件，不静默丢弃异常。"""
        ProjectManager._atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """在同目录写临时文件并替换目标；字节完整写入成功后才发布。"""
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_bytes(path: Path, limit: int) -> bytes:
        """读取上限以内的实体文件；超过 limit 字节或链接文件以 OSError 拒绝。"""
        if ProjectManager._is_link(path):
            raise OSError("拒绝符号链接")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise OSError("文件超过读取上限")
        return data

    @staticmethod
    def _safe_mkdir(path: Path) -> None:
        """只创建解析位置未变化的管理根目录；已有链接根目录受控拒绝。"""
        if path.resolve() != path or ProjectManager._is_link(path):
            raise ProjectError("project_directory_invalid", "项目管理根目录被链接或替换，已拒绝操作。")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProjectError("project_directory_invalid", "无法创建项目管理目录，请检查宿主目录权限。") from exc

    @staticmethod
    def _identity(path: Path) -> list[int]:
        """记录实体目录的设备和文件标识；用于发现相同路径下的替换目录。"""
        if ProjectManager._is_link(path):
            raise OSError("目录被替换为链接")
        status = path.stat()
        return [status.st_dev, status.st_ino]

    @staticmethod
    def _is_link(path: Path) -> bool:
        """同时识别 POSIX 符号链接与 Windows junction，缺失路径返回假。"""
        return path.is_symlink() or path.is_junction()

    @staticmethod
    def _uuid(value: UUID) -> UUID:
        """规范化内部运行标识，拒绝非 UUID 输入以阻止目录越界。"""
        try:
            return UUID(str(value))
        except ValueError as exc:
            raise ProjectError("project_run_invalid", "运行标识必须是有效 UUID。") from exc

    @staticmethod
    def _warn(review: ProjectReview, message: str) -> None:
        """去重追加有界中文警告；只修改传入审阅对象，不单独写盘。"""
        if message not in review.warnings:
            review.warnings = [*review.warnings[-15:], message]

    @staticmethod
    def _status_paths(data: bytes) -> list[str]:
        """解析 Git porcelain v1 的 NUL 分隔路径，重命名记录同时计入源与目标。"""
        records = data.split(b"\0")
        paths = []
        position = 0
        while position < len(records) and records[position]:
            record = records[position]
            if len(record) < 4:
                raise ProjectError("project_snapshot_failed", "Git 变更列表格式无效。")
            paths.append(record[3:].decode("utf-8"))
            position += 1
            if b"R" in record[:2] or b"C" in record[:2]:
                if position >= len(records):
                    raise ProjectError("project_snapshot_failed", "Git 重命名列表格式无效。")
                paths.append(records[position].decode("utf-8"))
                position += 1
        return list(dict.fromkeys(paths))

    @staticmethod
    def _changed_files(data: bytes, binaries: set[str]) -> list[ChangedFile]:
        """解析 NUL 分隔变更类型；不支持的类型受控失败，避免展示不完整审阅。"""
        records = data.split(b"\0")
        files = []
        position = 0
        while position < len(records) and records[position]:
            kind = records[position][:1]
            position += 1
            if position >= len(records):
                raise ProjectError("project_snapshot_failed", "Git 文件列表格式无效。")
            if kind in (b"R", b"C"):
                position += 1  # 重命名的源路径已包含在可下载补丁中，界面显示目标路径。
            if position >= len(records):
                raise ProjectError("project_snapshot_failed", "Git 重命名列表格式无效。")
            name = records[position].decode("utf-8")
            position += 1
            status = {b"A": "added", b"M": "modified", b"D": "deleted", b"R": "renamed", b"T": "modified"}.get(kind)
            if status is None:
                raise ProjectError("project_snapshot_unsupported", "变更含暂不支持的 Git 文件类型。")
            files.append(ChangedFile(path=name, status=status, binary=name in binaries))
        return files
