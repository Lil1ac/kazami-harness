"""真实临时 Git 仓库上的项目隔离、代码快照和审阅生命周期测试。"""

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_harness.projects import ProjectError, ProjectManager, ProjectPreset


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """建立含文本和二进制基线的临时仓库；返回 Git 根目录，不触碰项目实际仓库。"""
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    (root / "app.txt").write_text("initial\n", encoding="utf-8")
    (root / "delete.txt").write_text("remove me\n", encoding="utf-8")
    (root / "image.bin").write_bytes(b"\0initial\x01")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    return root


def git(root: Path, *argv: str, check: bool = True) -> bytes:
    """在临时仓库执行固定测试命令，返回标准输出；默认异常立即使测试失败。"""
    return subprocess.run(["git", "-C", str(root), *argv], check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15).stdout


def preset(root: Path) -> ProjectPreset:
    """创建宿主项目预设；测试命令仅随配置保存，本测试不执行该命令。"""
    return ProjectPreset(id="fixture", label="临时项目", path=root, test_argv=["python", "-m", "pytest"])


def manager(tmp_path: Path) -> ProjectManager:
    """为单个测试绑定独立记录根目录及工作树根目录。"""
    return ProjectManager(tmp_path / "records", tmp_path / "worktrees")


def test_prepare_keeps_source_dirty_and_index_untouched(repository: Path, tmp_path: Path) -> None:
    """源目录的已暂存、未暂存与新增改动均保留，任务只包含固定提交基线。"""
    (repository / "app.txt").write_text("staged\n", encoding="utf-8")
    git(repository, "add", "app.txt")
    (repository / "app.txt").write_text("unstaged\n", encoding="utf-8")
    (repository / "source-only.txt").write_text("private\n", encoding="utf-8")
    before = git(repository, "status", "--porcelain=v1", "-z")
    index = (repository / ".git" / "index").read_bytes()
    service = manager(tmp_path)
    run_id = uuid4()
    review = service.prepare(preset(repository), run_id)
    worktree = Path(review.worktree)
    assert review.source_dirty is True
    assert review.base_commit == git(repository, "rev-parse", "HEAD").decode().strip()
    assert (worktree / "app.txt").read_text() == "initial\n"
    assert not (worktree / "source-only.txt").exists()
    assert not (tmp_path / "records" / str(run_id)).exists()
    assert (tmp_path / "records" / ".projects" / str(run_id) / "review.json").is_file()
    assert git(repository, "status", "--porcelain=v1", "-z") == before
    assert (repository / ".git" / "index").read_bytes() == index
    assert git(worktree, "symbolic-ref", "-q", "HEAD", check=False) == b""


def test_full_patch_includes_added_modified_deleted_binary_and_applies(repository: Path, tmp_path: Path) -> None:
    """完整补丁含新增、修改、删除和二进制，可在原基线干净应用并恢复目标字节。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    worktree = Path(review.worktree)
    (worktree / "app.txt").write_text("fixed\n", encoding="utf-8")
    (worktree / "new folder").mkdir()
    (worktree / "new folder" / "新增.txt").write_text("新增内容\n", encoding="utf-8")
    (worktree / "delete.txt").unlink()
    (worktree / "image.bin").write_bytes(b"\0new binary\x02")
    index_before = (repository / ".git" / "index").read_bytes()
    captured = service.capture(review.run_id)
    files = {item.path: item for item in captured.files}
    assert files["app.txt"].status == "modified"
    assert files["new folder/新增.txt"].status == "added"
    assert files["delete.txt"].status == "deleted"
    assert files["image.bin"].binary
    assert captured.snapshot_status == "ready" and captured.patch_available
    patch = service.patch_path(review.run_id)
    assert b"GIT binary patch" in patch.read_bytes()
    assert (repository / ".git" / "index").read_bytes() == index_before
    git(repository, "apply", "--check", str(patch))
    git(repository, "apply", str(patch))
    assert (repository / "app.txt").read_text() == "fixed\n"
    assert not (repository / "delete.txt").exists()
    assert (repository / "new folder" / "新增.txt").read_text(encoding="utf-8") == "新增内容\n"
    assert (repository / "image.bin").read_bytes() == b"\0new binary\x02"


def test_restart_keeps_review_and_patch_then_discards_idempotently(repository: Path, tmp_path: Path) -> None:
    """实例重建仍能读审阅和补丁，丢弃只删除任务工作树且多次调用结果一致。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    (Path(review.worktree) / "app.txt").write_text("task\n", encoding="utf-8")
    service.capture(review.run_id)
    restarted = manager(tmp_path)
    assert restarted.get(review.run_id).snapshot_status == "ready"
    patch = restarted.patch_path(review.run_id)
    first = restarted.decide(review.run_id, "discard")
    second = restarted.decide(review.run_id, "discard")
    assert first == second and first.status == "discarded"
    assert not Path(review.worktree).exists()
    assert patch.exists()
    assert repository.is_dir() and (repository / "app.txt").read_text() == "initial\n"
    assert restarted.get(review.run_id).patch_available
    with pytest.raises(ProjectError, match="已经完成审阅"):
        restarted.decide(review.run_id, "keep")


def test_keep_is_idempotent_and_preserves_worktree(repository: Path, tmp_path: Path) -> None:
    """保留仅更新审阅状态，独立工作树、补丁和源目录内容均保留。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    service.capture(review.run_id)
    first = service.decide(review.run_id, "keep")
    assert first.status == "kept" and Path(first.worktree).is_dir()
    assert service.decide(review.run_id, "keep") == first
    assert service.patch_path(review.run_id).read_bytes() == b""


def test_invalid_ref_is_controlled_and_creates_no_worktree(repository: Path, tmp_path: Path) -> None:
    """不存在或选项形状的引用无法创建任务工作树，错误消息不含 Git 原始诊断。"""
    service = manager(tmp_path)
    for reference in ("missing-branch", "--output=elsewhere", "HEAD\0bad"):
        with pytest.raises(ProjectError) as caught:
            service.prepare(preset(repository), uuid4(), reference)
        assert caught.value.code == "project_ref_invalid"
        assert "fatal:" not in caught.value.message
    assert not service.worktrees_root.exists()


def test_snapshotted_then_changed_refuses_discard(repository: Path, tmp_path: Path) -> None:
    """完整快照之后用户增加新改动时拒绝丢弃，重新快照后才允许。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    service.capture(review.run_id)
    (Path(review.worktree) / "late.txt").write_text("do not lose\n", encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        service.decide(review.run_id, "discard")
    assert caught.value.code == "project_snapshot_stale"
    assert (Path(review.worktree) / "late.txt").exists()
    service.capture(review.run_id)
    assert service.decide(review.run_id, "discard").status == "discarded"


def test_replaced_directory_refuses_discard(repository: Path, tmp_path: Path) -> None:
    """相同路径替换为其他实体目录时拒绝删除，Git 注册本身不能代替目录身份校验。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    service.capture(review.run_id)
    worktree = Path(review.worktree)
    relocated = tmp_path / "relocated-worktree"
    worktree.rename(relocated)
    worktree.mkdir()
    (worktree / "user.txt").write_text("user data", encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        service.decide(review.run_id, "discard")
    assert caught.value.code == "project_worktree_changed"
    assert (worktree / "user.txt").exists()
    assert service.get(review.run_id).status == "pending"
    assert service.get(review.run_id).warnings


def test_review_path_cannot_point_to_source(repository: Path, tmp_path: Path) -> None:
    """即使公开审阅 JSON 的路径被改成源目录，也拒绝删除源目录。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    service.capture(review.run_id)
    record = service.metadata_root / str(review.run_id) / "review.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    data["worktree"] = str(repository)
    record.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        service.decide(review.run_id, "discard")
    assert caught.value.code == "project_worktree_changed"
    assert (repository / "app.txt").read_text() == "initial\n"


def test_missing_worktree_stays_pending_with_warning(repository: Path, tmp_path: Path) -> None:
    """用户通过 Git 手动移除工作树后，服务保留 pending 并明确提示缺失。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    git(repository, "worktree", "remove", "--force", review.worktree)
    found = service.get(review.run_id)
    assert found.status == "pending" and found.warnings
    with pytest.raises(ProjectError) as caught:
        service.capture(review.run_id)
    assert caught.value.code == "project_worktree_missing"
    assert service.get(review.run_id).snapshot_status == "error"


def test_snapshot_limit_does_not_publish_partial_patch(repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """补丁输出超过上限时状态为 error，旧文件不能冒充可下载的完整补丁。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    (Path(review.worktree) / "app.txt").write_text("a" * 1024, encoding="utf-8")
    monkeypatch.setattr(service, "MAX_PATCH_BYTES", 64)
    with pytest.raises(ProjectError) as caught:
        service.capture(review.run_id)
    assert caught.value.code == "project_snapshot_limit"
    found = service.get(review.run_id)
    assert found.snapshot_status == "error" and not found.patch_available and not found.files
    with pytest.raises(ProjectError):
        service.patch_path(review.run_id)
    assert Path(review.worktree).is_dir()


def test_corrupt_patch_is_not_downloadable_or_discardable(repository: Path, tmp_path: Path) -> None:
    """补丁字节被替换后长度或摘要校验失败，不返回下载路径且拒绝丢弃工作树。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    service.capture(review.run_id)
    service.patch_path(review.run_id).write_bytes(b"corrupt")
    found = service.get(review.run_id)
    assert found.snapshot_status == "error" and not found.patch_available
    with pytest.raises(ProjectError):
        service.decide(review.run_id, "discard")
    assert Path(review.worktree).is_dir()


def test_rename_and_unicode_paths(repository: Path, tmp_path: Path) -> None:
    """重命名以目标路径展示，原始重命名信息仍存在于完整补丁中。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    worktree = Path(review.worktree)
    (worktree / "app.txt").rename(worktree / "重命名.txt")
    captured = service.capture(review.run_id)
    item = next(item for item in captured.files if item.path == "重命名.txt")
    assert item.status == "renamed"
    assert b"rename from app.txt" in service.patch_path(review.run_id).read_bytes()


def test_project_subdirectory_and_duplicate_run_are_rejected(repository: Path, tmp_path: Path) -> None:
    """配置仓库子目录或重复使用运行标识都受控拒绝，避免目录归属不清。"""
    service = manager(tmp_path)
    child = repository / "nested"
    child.mkdir()
    with pytest.raises(ProjectError) as caught:
        service.prepare(preset(child), uuid4())
    assert caught.value.code == "project_root_required"
    run_id = uuid4()
    service.prepare(preset(repository), run_id)
    with pytest.raises(ProjectError) as caught:
        service.prepare(preset(repository), run_id)
    assert caught.value.code == "project_run_exists"


@pytest.mark.parametrize("python_path", ["../outside", "C:/outside", "/outside", "..\\outside", "\\rooted", ""])
def test_python_import_paths_cannot_escape(repository: Path, python_path: str) -> None:
    """Python 导入目录不能使用绝对路径、盘符或父目录，避免宿主源目录抢占任务代码。"""
    with pytest.raises(ValidationError):
        ProjectPreset(id="fixture", label="项目", path=repository, test_argv=["python"], test_python_paths=[python_path])


def test_kept_worktree_can_later_be_discarded(repository: Path, tmp_path: Path) -> None:
    """已保留工作树仍允许明确丢弃；同样保留完整补丁并拒绝逆向恢复。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    service.capture(review.run_id)
    service.decide(review.run_id, "keep")
    assert service.decide(review.run_id, "discard").status == "discarded"
    assert not Path(review.worktree).exists()
    assert service.patch_path(review.run_id).is_file()


def test_kept_worktree_change_can_be_recaptured(repository: Path, tmp_path: Path) -> None:
    """已保留后手动修改需先重捕快照，状态保持 kept，随后才能安全丢弃。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    service.capture(review.run_id)
    service.decide(review.run_id, "keep")
    (Path(review.worktree) / "later.txt").write_text("later", encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        service.decide(review.run_id, "discard")
    assert caught.value.code == "project_snapshot_stale"
    captured = service.capture(review.run_id)
    assert captured.status == "kept"
    assert any(item.path == "later.txt" for item in captured.files)
    assert service.decide(review.run_id, "discard").status == "discarded"


def test_prepare_does_not_execute_checkout_hooks(repository: Path, tmp_path: Path) -> None:
    """仓库存在可执行 post-checkout 钩子时，建工作树不会运行它。"""
    marker = tmp_path / "hook-marker.txt"
    hook = repository / ".git" / "hooks" / "post-checkout"
    hook.write_text('#!/bin/sh\nprintf ran > "' + str(marker).replace('\\', '/') + '"\n', encoding="utf-8")
    if os.name != "nt":
        hook.chmod(0o755)
    review = manager(tmp_path).prepare(preset(repository), uuid4())
    assert Path(review.worktree).is_dir()
    assert not marker.exists()


def test_snapshot_limits_also_apply_after_agent_commits(repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent 自行提交并让 HEAD 干净时，仍按启动基线检查变更大小。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    worktree = Path(review.worktree)
    (worktree / "app.txt").write_text("a" * 2048, encoding="utf-8")
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", "agent commit")
    monkeypatch.setattr(service, "MAX_CHANGED_BYTES", 1024)
    with pytest.raises(ProjectError) as caught:
        service.capture(review.run_id)
    assert caught.value.code == "project_snapshot_limit"
    assert service.get(review.run_id).snapshot_status == "error"


def test_empty_patch_is_complete_and_does_not_modify_task_index(repository: Path, tmp_path: Path) -> None:
    """无代码改动生成可下载空补丁，带暂存改动的任务索引也保持原样。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    empty = service.capture(review.run_id)
    assert empty.files == [] and empty.patch_available
    worktree = Path(review.worktree)
    (worktree / "app.txt").write_text("staged task", encoding="utf-8")
    git(worktree, "add", "app.txt")
    git_dir = Path(git(worktree, "rev-parse", "--absolute-git-dir").decode().strip())
    before = (git_dir / "index").read_bytes()
    captured = service.capture(review.run_id)
    assert captured.files[0].path == "app.txt"
    assert (git_dir / "index").read_bytes() == before


def test_repeated_get_does_not_launch_git_or_rehash_unchanged_patch(repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """高频 SSE 视图读取不启动 Git，已核验且 stat 未变的完整补丁不会反复读取。"""
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    (Path(review.worktree) / "app.txt").write_text("fixed", encoding="utf-8")
    service.capture(review.run_id)
    service.get(review.run_id)  # 预热小容量摘要缓存。
    original_read = service._read_bytes
    patch_reads = []

    def counted_read(path: Path, limit: int) -> bytes:
        """只记录大补丁正文读取次数，仍用原读取器验证其他元数据。"""
        if path.name == "changes.patch":
            patch_reads.append(path)
        return original_read(path, limit)

    def forbidden_git(*args, **kwargs) -> bytes:
        """只读视图意外启动 Git 时立刻失败，避免高频轮询退化为进程风暴。"""
        raise AssertionError("get 不应启动 Git")

    monkeypatch.setattr(service, "_read_bytes", counted_read)
    monkeypatch.setattr(service, "_git", forbidden_git)
    for _ in range(10):
        assert service.get(review.run_id).patch_available
    assert patch_reads == []
    patch = service.metadata_root / str(review.run_id) / "changes.patch"
    patch.write_bytes(b"tampered")
    assert service.get(review.run_id).snapshot_status == "error"
    assert len(patch_reads) == 1


def test_get_does_not_wait_for_capture_git(repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """后台捕获 Git 阻塞时，高频只读视图仍能即时读取旧记录，不持大操作锁等待。"""
    import concurrent.futures
    import threading

    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    started = threading.Event()
    release = threading.Event()
    original_snapshot = service._snapshot

    def blocked_snapshot(value):
        """模拟耗时 Git 读取，等待测试明确放行后返回真实快照。"""
        started.set()
        assert release.wait(5)
        return original_snapshot(value)

    monkeypatch.setattr(service, "_snapshot", blocked_snapshot)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        captured = executor.submit(service.capture, review.run_id)
        assert started.wait(5)
        view = executor.submit(service.get, review.run_id)
        try:
            assert view.result(timeout=1).snapshot_status == "pending"
        finally:
            release.set()
        assert captured.result(timeout=10).snapshot_status == "ready"


def test_host_identifier_and_path_separator_limits(repository: Path) -> None:
    """宿主标识与网页提交长度一致，导入目录不能把一项拼成多个路径。"""
    for identifier in ("x" * 65, "with spaces", "../outside"):
        with pytest.raises(ValidationError):
            ProjectPreset(id=identifier, label="项目", path=repository, test_argv=["python"])
    with pytest.raises(ValidationError):
        ProjectPreset(id="ok", label="x" * 101, path=repository, test_argv=["python"])
    with pytest.raises(ValidationError):
        ProjectPreset(id="ok", label="项目", path=repository, test_argv=["python"], test_python_paths=[f"src{os.pathsep}outside"])


def test_git_does_not_inherit_api_keys_or_parent_git_injection(repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Git 仅继承目录与基础进程环境，父密钥、Python 注入和 Git 重定向均被剔除。"""
    service = manager(tmp_path)
    original_spawn = subprocess.Popen
    environments = []

    def inspected_spawn(*args, **kwargs):
        """保存本次 Git 的环境名称以断言隔离，随后仍启动真实 Git。"""
        environments.append(kwargs["env"])
        return original_spawn(*args, **kwargs)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-secret")
    monkeypatch.setenv("UNRELATED_SERVICE_TOKEN", "fixture-token")
    monkeypatch.setenv("PYTHONPATH", "parent-injection")
    monkeypatch.setenv("GIT_DIR", "bad-parent-repository")
    monkeypatch.setattr(subprocess, "Popen", inspected_spawn)
    result = service._git(repository, ["rev-parse", "HEAD"])
    assert len(result.strip()) in (40, 64)
    assert environments
    for environment in environments:
        for name in ("DEEPSEEK_API_KEY", "UNRELATED_SERVICE_TOKEN", "PYTHONPATH", "GIT_DIR"):
            assert name not in environment
    assert os.environ["DEEPSEEK_API_KEY"] == "fixture-secret"


def test_inspect_reads_fixed_head_without_directories_or_index_refresh(repository: Path, tmp_path: Path) -> None:
    """项目选项探测只读 HEAD 和 dirty，不创建记录目录、不刷新用户索引。"""
    service = manager(tmp_path)
    index = repository / ".git" / "index"
    original_index = index.read_bytes()
    original_stat = index.stat().st_mtime_ns
    clean = service.inspect(preset(repository))
    assert clean == {"head_commit": git(repository, "rev-parse", "HEAD").decode().strip(), "source_dirty": False}
    (repository / "app.txt").write_text("source edit", encoding="utf-8")
    dirty = service.inspect(preset(repository))
    assert dirty["source_dirty"] is True
    assert index.read_bytes() == original_index and index.stat().st_mtime_ns == original_stat
    assert not service.metadata_root.exists() and not service.worktrees_root.exists()


def test_repository_clean_smudge_filters_are_disabled(repository: Path, tmp_path: Path) -> None:
    """宿主仓库配置外部过滤器后，建工作树与捕获代码均不运行其脚本。"""
    marker = tmp_path / "filter-ran.txt"
    script = tmp_path / "filter.sh"
    script.write_text('#!/bin/sh\nprintf ran > "' + str(marker).replace('\\', '/') + '"\ncat\n', encoding="utf-8")
    if os.name != "nt":
        script.chmod(0o755)
    command = 'sh "' + str(script).replace('\\', '/') + '"'
    git(repository, "config", "filter.fixture.clean", command)
    git(repository, "config", "filter.fixture.smudge", command)
    git(repository, "config", "filter.fixture.required", "true")
    (repository / ".gitattributes").write_text("*.txt filter=fixture\n", encoding="utf-8")
    git(repository, "add", ".gitattributes")
    git(repository, "commit", "-m", "declare filter")
    marker.unlink(missing_ok=True)
    service = manager(tmp_path)
    review = service.prepare(preset(repository), uuid4())
    (Path(review.worktree) / "app.txt").write_text("modified\n", encoding="utf-8")
    captured = service.capture(review.run_id)
    assert captured.patch_available and not marker.exists()
