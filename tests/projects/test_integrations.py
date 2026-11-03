"""真实Git验证固定成果、目标修改保护、冲突、增量、恢复和受保护撤销。"""

from pathlib import Path
import threading
from uuid import uuid4

import pytest

from agent_harness.permissions.models import PermissionPolicy
from agent_harness.projects import ProjectManager, ProjectPreset
from agent_harness.projects.integration_files import git
from agent_harness.projects.integrations import IntegrationStore
from agent_harness.work.tasks import TaskError


@pytest.fixture
def setup(tmp_path: Path):
    """在tmp_path准备真实仓库、会话工作树和采纳仓储；返回源目录、管理器、仓储、项目及首轮。"""
    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, "init", "-q")
    (repository / "app.txt").write_bytes(b"one\ntwo\nthree\n")
    (repository / "keep.txt").write_bytes(b"keep\n")
    (repository / ".gitignore").write_text("ignored.txt\n__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "-c", "user.name=test", "-c", "user.email=test@localhost", "commit", "-qm", "baseline")
    manager = ProjectManager(tmp_path / "records", tmp_path / "worktrees")
    preset = ProjectPreset(id="source", label="源项目", path=repository)
    review = manager.prepare(preset, uuid4())
    return repository, manager, IntegrationStore(manager), preset, review


def prepare(setup, content: bytes = b"ONE\ntwo\nthree\n") -> dict:
    """给setup的工作树写content并固定首轮成果；返回尚未采纳的集成记录。"""
    repository, manager, store, preset, review = setup
    (Path(review.worktree) / "app.txt").write_bytes(content)
    manager.capture(review.run_id)
    value = store.create(str(review.run_id), str(review.session_id), preset.id, str(uuid4()))
    return store.prepare(value["id"], preset, threading.Event())


def apply(store: IntegrationStore, value: dict) -> dict:
    """按value的固定版本和指纹执行真实写回；返回回执，不运行项目测试。"""
    return store.apply(value["id"], value["revision"], value["fingerprint"], PermissionPolicy(), threading.Event())


def test_adoption_preserves_dirty_index_and_undo(setup):
    """合并保留目标其他未提交文件、暂存区字节及历史；撤销只恢复本次变化。"""
    repository, manager, store, preset, review = setup
    (repository / "keep.txt").write_bytes(b"staged\n")
    git(repository, "add", "keep.txt")
    (repository / "keep.txt").write_bytes(b"unstaged\n")
    (repository / "local.txt").write_bytes(b"local\n")
    index = (repository / ".git" / "index").read_bytes()
    value = prepare(setup)
    assert value["status"] == "ready" and (repository / "app.txt").read_bytes().startswith(b"one")
    applied = apply(store, value)
    assert applied["status"] == "applied"
    assert (repository / ".git" / "index").read_bytes() == index
    assert (repository / "keep.txt").read_bytes() == b"unstaged\n"
    assert (repository / "local.txt").read_bytes() == b"local\n"
    undone = store.undo(value["id"], str(uuid4()), preset, threading.Event())
    apply(store, undone)
    assert (repository / "app.txt").read_bytes() == b"one\ntwo\nthree\n"
    assert (repository / ".git" / "index").read_bytes() == index
    assert store.get(value["id"])["status"] == "undone"


def test_conflicts_are_resolved_in_candidate_only(setup):
    """同一行冲突需要明确解决；预览和解决不改目标，确认后才写回。"""
    repository, manager, store, preset, review = setup
    (repository / "app.txt").write_bytes(b"TARGET\ntwo\nthree\n")
    value = prepare(setup)
    assert value["status"] == "conflicts" and "app.txt" in value["conflicts"]
    assert store.preview(value["id"], "app.txt")["target"].startswith("TARGET")
    value = store.resolve(value["id"], value["revision"], [{"path": "app.txt", "choice": "edit", "content": "BOTH\ntwo\nthree\n"}])
    assert value["status"] == "ready"
    assert (repository / "app.txt").read_bytes().startswith(b"TARGET")
    apply(store, value)
    assert (repository / "app.txt").read_bytes().startswith(b"BOTH")


def test_target_change_invalidates_confirmation(setup):
    """预览后修改不相关源码也会使整体目标指纹失效，不能继续使用旧批准。"""
    repository, manager, store, preset, review = setup
    value = prepare(setup)
    (repository / "keep.txt").write_bytes(b"later\n")
    with pytest.raises(TaskError, match="目标代码"):
        apply(store, value)
    assert (repository / "app.txt").read_bytes().startswith(b"one")


def test_ignored_file_collision_blocks_preparation(setup):
    """来源强制纳入的文件不能覆盖目标同名忽略文件。"""
    repository, manager, store, preset, review = setup
    (repository / ".gitignore").write_text("incoming.txt\n", encoding="utf-8")
    (repository / "incoming.txt").write_bytes(b"private\n")
    (Path(review.worktree) / "incoming.txt").write_bytes(b"agent\n")
    with pytest.raises(TaskError, match="忽略文件"):
        prepare(setup)
    assert (repository / "incoming.txt").read_bytes() == b"private\n"


def test_external_edit_prevents_undo(setup):
    """采纳文件的后续编辑不会被撤销覆盖。"""
    repository, manager, store, preset, review = setup
    value = apply(store, prepare(setup))
    (repository / "app.txt").write_bytes(b"manual\n")
    undo_id = str(uuid4())
    with pytest.raises(TaskError, match="后续编辑"):
        store.undo(value["id"], undo_id, preset, threading.Event())
    assert (repository / "app.txt").read_bytes() == b"manual\n"
    assert store.get(undo_id)["status"] == "failed"
    assert store.get(undo_id)["kind"] == "undo"
    assert store.get(value["id"])["status"] == "applied"
    assert not any(item["status"] == "preparing" for item in store.list(value["session_id"]))


def test_binary_conflict_and_modify_delete_are_explicit(setup):
    """二进制冲突按完整字节选择；修改/删除冲突明确删除，目标不会提前变化。"""
    repository, manager, store, preset, review = setup
    worktree = Path(review.worktree)
    (repository / "app.txt").write_bytes(b"\0target")
    (repository / "keep.txt").write_bytes(b"local modification")
    (worktree / "keep.txt").unlink()
    value = prepare(setup, b"\0source")
    assert set(value["conflicts"]) == {"app.txt", "keep.txt"}
    value = store.resolve(value["id"], value["revision"], [
        {"path": "app.txt", "choice": "source"}, {"path": "keep.txt", "choice": "delete"},
    ])
    apply(store, value)
    assert (repository / "app.txt").read_bytes() == b"\0source"
    assert not (repository / "keep.txt").exists()


def test_rename_preserves_target_edits(setup):
    """源文件重命名与目标文件内容修改可以三方合并，最终差异包含原路径删除与新路径。"""
    repository, manager, store, preset, review = setup
    worktree = Path(review.worktree)
    (worktree / "keep.txt").rename(worktree / "renamed.txt")
    (repository / "keep.txt").write_bytes(b"local keep\n")
    value = prepare(setup)
    assert not value["conflicts"]
    apply(store, value)
    assert not (repository / "keep.txt").exists()
    assert (repository / "renamed.txt").read_bytes() == b"local keep\n"


def test_crlf_and_bom_are_preserved(setup):
    """三方合并统一文本换行，写回仍保持目标已有CRLF和UTF-8 BOM。"""
    repository, manager, store, preset, review = setup
    (repository / "app.txt").write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\nthree\r\n")
    value = prepare(setup)
    assert not value["conflicts"]
    apply(store, value)
    assert (repository / "app.txt").read_bytes() == b"\xef\xbb\xbfONE\r\ntwo\r\nthree\r\n"


def test_staged_and_unstaged_versions_of_same_file_survive(setup):
    """同一文件暂存版本与磁盘版本不同时只合并磁盘，原暂存内容仍可精确读取。"""
    repository, manager, store, preset, review = setup
    (repository / "app.txt").write_bytes(b"one\ntwo\nSTAGED\n")
    git(repository, "add", "app.txt")
    staged = git(repository, "show", ":app.txt")
    index = (repository / ".git/index").read_bytes()
    (repository / "app.txt").write_bytes(b"one\ntwo\nWORKING\n")
    value = prepare(setup)
    if value["conflicts"]:
        value = store.resolve(value["id"], value["revision"], [{"path": "app.txt", "choice": "edit", "content": "ONE\ntwo\nWORKING\n"}])
    apply(store, value)
    assert (repository / "app.txt").read_bytes() == b"ONE\ntwo\nWORKING\n"
    assert git(repository, "show", ":app.txt") == staged
    assert (repository / ".git/index").read_bytes() == index


def test_partial_write_recovery_never_replays(setup, monkeypatch):
    """两文件写回在第一项后停止；重启只标观察状态，剩余文件不会自动执行。"""
    repository, manager, store, preset, review = setup
    (Path(review.worktree) / "keep.txt").write_bytes(b"new keep\n")
    value = prepare(setup)
    cancel = threading.Event()
    original = store.journal

    def stop_after_first(journal: dict):
        """保存真实journal并在首项已写后请求取消，模拟真实文件边界中断。"""
        original(journal)
        if any(item["state"] == "written" for item in journal["files"]):
            cancel.set()

    monkeypatch.setattr(store, "journal", stop_after_first)
    with pytest.raises(TaskError, match="文件边界"):
        store.apply(value["id"], value["revision"], value["fingerprint"], PermissionPolicy(), cancel)
    assert (repository / "app.txt").read_bytes().startswith(b"ONE")
    assert (repository / "keep.txt").read_bytes() == b"keep\n"
    restarted = IntegrationStore(manager)
    observed = restarted.reconcile(value["id"])
    assert observed["status"] == "needs_review"
    assert {item["state"] for item in observed["observations"]} == {"after", "before"}
    with pytest.raises(TaskError, match="尚未完整"):
        restarted.reconcile(value["id"], True)
    assert (repository / "keep.txt").read_bytes() == b"keep\n"


def test_completed_file_receipt_repairs_database_only(setup, monkeypatch):
    """完整文件回执已落盘而SQLite发布失败时，仅补记事实和锚点，不再次写目标。"""
    repository, manager, store, preset, review = setup
    value = prepare(setup)

    def database_failed(value: dict, journal: dict):
        """模拟value的完整journal发布到数据库前失败，不触碰已写文件。"""
        raise OSError("simulated database failure")

    monkeypatch.setattr(store, "finish", database_failed)
    with pytest.raises(OSError):
        apply(store, value)
    (repository / "app.txt").write_bytes(b"later manual edit\n")
    restarted = IntegrationStore(manager)
    observed = restarted.reconcile(value["id"])
    assert observed["status"] == "applied"
    assert (repository / "app.txt").read_bytes() == b"later manual edit\n"


def test_incremental_and_multiple_targets_use_independent_anchors(setup):
    """续轮以已采纳来源为基线，保留目标后续编辑；另一目标首次采纳仍使用原始基线。"""
    from agent_harness.schemas import RunManifest, RunConfig, TaskSpec
    from agent_harness.storage.runs import RunStore
    repository, manager, store, preset, review = setup
    first = apply(store, prepare(setup))
    RunStore.create(store.work.root, RunManifest(run_id=review.run_id, session_id=review.session_id, turn_index=1,
                                               task=TaskSpec(prompt="first", workspace=Path(review.worktree)), config=RunConfig()))
    next_id = uuid4()
    next_review = manager.prepare_turn(preset, review.run_id, next_id)
    (Path(review.worktree) / "app.txt").write_bytes(b"ONE\ntwo\nthree\nfour\n")
    manager.capture(next_id)
    RunStore.create(store.work.root, RunManifest(run_id=next_id, session_id=review.session_id, turn_index=2,
                                               task=TaskSpec(prompt="next", workspace=Path(review.worktree)), config=RunConfig()))
    (repository / "app.txt").write_bytes(b"manual\ntwo\nthree\n")
    value = store.create(str(next_id), str(review.session_id), preset.id, str(uuid4()))
    value = store.prepare(value["id"], preset, threading.Event())
    assert not value["conflicts"]
    apply(store, value)
    assert (repository / "app.txt").read_bytes() == b"manual\ntwo\nthree\nfour\n"
    duplicate = store.create(str(next_id), str(review.session_id), preset.id, str(uuid4()))
    assert store.prepare(duplicate["id"], preset, threading.Event())["status"] == "already_applied"
    other = repository.parent / "other"
    git(repository, "worktree", "add", "--detach", str(other), review.base_commit)
    other_preset = preset.model_copy(update={"id": "other", "path": other})
    value = store.create(str(review.run_id), str(review.session_id), "other", str(uuid4()))
    value = store.prepare(value["id"], other_preset, threading.Event())
    apply(store, value)
    assert (other / "app.txt").read_bytes() == b"ONE\ntwo\nthree\n"


def test_git_export_attributes_cannot_omit_fixed_files(setup):
    """Git导出属性不能删改固定成果；从原始blob读取，历史不会因归档属性漏文件。"""
    repository, manager, store, preset, review = setup
    worktree = Path(review.worktree)
    (worktree / ".gitattributes").write_text("keep.txt export-ignore\n", encoding="utf-8")
    value = prepare(setup)
    assert "keep.txt" in value["source"] and "keep.txt" in value["candidate"]
    apply(store, value)
    assert (repository / "keep.txt").read_bytes() == b"keep\n"
