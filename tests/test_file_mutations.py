"""验证文件搜索、原子写入与统一工作区边界，覆盖预算和文件系统失败。"""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_harness.tools.files import (
    FileToolError,
    SearchFilesArguments,
    WriteFileArguments,
    search_files,
    write_file,
)
from agent_harness.workspace import Workspace, WorkspaceError


def make_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """为每个测试创建独立工作区及未创建的运行记录路径。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace.resolve(), workspace / ".runs"


def search(workspace: Path, runs_root: Path, query: str = "ERROR", **kwargs) -> dict:
    """按默认预览限制调用搜索，便于测试只声明需要变化的参数。"""
    preview_chars = kwargs.pop("preview_chars", 12000)
    return search_files(workspace, runs_root, SearchFilesArguments(query=query, **kwargs), preview_chars)


def write(workspace: Path, runs_root: Path, path: str, content: str = "中文🙂", **kwargs) -> dict:
    """按默认字节预算调用写入，独立传递覆盖参数与测试用限额。"""
    max_bytes = kwargs.pop("max_bytes", 1024 * 1024)
    return write_file(workspace, runs_root, WriteFileArguments(path=path, content=content, **kwargs), max_bytes)


def test_search_literal_rows_paths_and_glob_are_deterministic(tmp_path: Path) -> None:
    """搜索按字面量而非正则匹配，返回工作区相对路径和一基行号。"""
    workspace, runs = make_workspace(tmp_path)
    (workspace / "sub").mkdir()
    (workspace / "a.log").write_text("INFO ok\nERROR [x].*\nerror lower\n", encoding="utf-8")
    (workspace / "sub/b.log").write_text("ERROR [x].*\n", encoding="utf-8")
    (workspace / "ignored.txt").write_text("ERROR [x].*", encoding="utf-8")
    result = search(workspace, runs, "[x].*", glob="*.log")
    assert result["matches"] == [
        {"path": "a.log", "line_number": 2, "line": "ERROR [x].*"},
        {"path": "sub/b.log", "line_number": 1, "line": "ERROR [x].*"},
    ]
    assert result["scanned_files"] == 2
    assert result["truncated"] is False
    assert search(workspace, runs, directory="sub")["matches"][0]["path"] == "sub/b.log"


def test_search_excludes_records_binary_and_invalid_utf8_with_counts(tmp_path: Path) -> None:
    """搜索报告受控跳过统计，不扫描记录内容或把二进制解码错误当匹配。"""
    workspace, runs = make_workspace(tmp_path)
    runs.mkdir()
    (runs / "private.log").write_text("ERROR PRIVATE", encoding="utf-8")
    (workspace / "binary.dat").write_bytes(b"ERROR\x00hidden")
    (workspace / "broken.log").write_bytes(b"ERROR \xff")
    (workspace / "a.log").write_text("ERROR allowed", encoding="utf-8")
    result = search(workspace, runs)
    assert len(result["matches"]) == 1
    assert result["skipped"] == {"binary_file": 1, "invalid_encoding": 1, "run_records": 1}
    assert result["skipped_count"] == 3
    assert "PRIVATE" not in json.dumps(result)


def test_search_limits_rows_and_characters_without_claiming_complete(tmp_path: Path) -> None:
    """匹配数量或文本预览到达预算时明确 truncated，不能误报完整搜索。"""
    workspace, runs = make_workspace(tmp_path)
    (workspace / "a.log").write_text("ERROR first\nERROR second\n", encoding="utf-8")
    limited = search(workspace, runs, limit=1)
    assert len(limited["matches"]) == 1
    assert limited["truncated"] is True
    clipped = search(workspace, runs, preview_chars=6)
    assert clipped["matches"][0]["line"] == "ERROR "
    assert clipped["matches"][0]["line_truncated"] is True
    assert clipped["truncated"] is True


def test_search_file_byte_and_total_byte_budgets_are_explicit(tmp_path: Path, monkeypatch) -> None:
    """单文件或累计字节预算触顶时保留有界计数，并明确结果不完整。"""
    workspace, runs = make_workspace(tmp_path)
    (workspace / "a.log").write_text("ERROR one", encoding="utf-8")
    (workspace / "b.log").write_text("ERROR two", encoding="utf-8")
    monkeypatch.setattr("agent_harness.tools.files.MAX_SEARCH_FILE_BYTES", 4)
    too_big = search(workspace, runs)
    assert too_big["skipped"] == {"file_size_limit": 2}
    assert too_big["scanned_bytes"] == 0
    assert too_big["truncated"] is True
    monkeypatch.setattr("agent_harness.tools.files.MAX_SEARCH_FILE_BYTES", 100)
    monkeypatch.setattr("agent_harness.tools.files.MAX_SEARCH_TOTAL_BYTES", 10)
    limited = search(workspace, runs)
    assert len(limited["matches"]) == 1
    assert limited["skipped"] == {"total_bytes_limit": 1}
    assert limited["scanned_bytes"] == 9
    assert limited["truncated"] is True


def test_search_file_and_entry_budgets_stop_bounded_scans(tmp_path: Path, monkeypatch) -> None:
    """无匹配搜索也受到扫描文件数和目录项数限制，避免仅限制结果条数。"""
    workspace, runs = make_workspace(tmp_path)
    for name in ["a.txt", "b.txt", "c.txt"]:
        (workspace / name).write_text("no matching content", encoding="utf-8")
    monkeypatch.setattr("agent_harness.tools.files.MAX_SEARCH_FILES", 1)
    limited = search(workspace, runs)
    assert limited["scanned_files"] == 1
    assert limited["skipped"] == {"file_count_limit": 1}
    assert limited["truncated"] is True
    monkeypatch.setattr("agent_harness.tools.files.MAX_SEARCH_FILES", 100)
    monkeypatch.setattr("agent_harness.tools.files.MAX_SEARCH_ENTRIES", 1)
    entries_limited = search(workspace, runs)
    assert entries_limited["scanned_files"] <= 1
    assert entries_limited["skipped"]["entry_limit"] == 1
    assert entries_limited["truncated"] is True


@pytest.mark.parametrize("arguments", [
    {"query": ""}, {"query": "a\nb"}, {"query": "a\u2028b"}, {"query": "a\rb"}, {"query": "a\x00b"},
    {"query": "a", "limit": 0}, {"query": "a", "limit": 201},
    {"query": "a", "limit": True}, {"query": "a", "directory": " "},
    {"query": "a", "glob": ""}, {"query": "a", "unknown": "secret"},
])
def test_search_arguments_reject_invalid_types_and_multiline(arguments: dict) -> None:
    """严格拒绝跨行查询、超范围条数和未知字段等无效搜索参数。"""
    with pytest.raises(ValidationError):
        SearchFilesArguments.model_validate(arguments)


def test_search_requires_directory_and_rejects_outside_or_records(tmp_path: Path) -> None:
    """搜索起点必须为允许目录，缺失文件和越界路径保持受控错误码。"""
    workspace, runs = make_workspace(tmp_path)
    (workspace / "file.txt").write_text("ERROR", encoding="utf-8")
    for directory, code in [("file.txt", "not_directory"), ("missing", "file_not_found"),
                            ("..", "path_outside_workspace"), (".runs", "run_records_forbidden")]:
        with pytest.raises(FileToolError) as caught:
            search(workspace, runs, directory=directory)
        assert caught.value.code == code


def test_write_creates_parents_utf8_bytes_and_atomic_overwrite(tmp_path: Path) -> None:
    """写入创建父目录，按 UTF-8 字节计数，并仅在显式许可后替换原文件。"""
    workspace, runs = make_workspace(tmp_path)
    result = write(workspace, runs, "nested/report.json")
    target = workspace / "nested/report.json"
    assert target.read_bytes() == "中文🙂".encode("utf-8")
    assert result == {"path": "nested/report.json", "bytes_written": 10, "created": True, "overwritten": False}
    with pytest.raises(FileToolError) as caught:
        write(workspace, runs, "nested/report.json", "new")
    assert caught.value.code == "file_exists"
    assert target.read_text(encoding="utf-8") == "中文🙂"
    replaced = write(workspace, runs, "nested/report.json", "new", overwrite=True)
    assert replaced["created"] is False
    assert replaced["overwritten"] is True
    assert target.read_text(encoding="utf-8") == "new"
    assert list(target.parent.glob(".harness-write-*")) == []


def test_write_rejects_oversized_utf8_before_creating_parent(tmp_path: Path) -> None:
    """大小限制使用字节并在任何父目录或临时文件创建前生效。"""
    workspace, runs = make_workspace(tmp_path)
    with pytest.raises(FileToolError) as caught:
        write(workspace, runs, "new/a.txt", "中", max_bytes=2)
    assert caught.value.code == "file_size_limit"
    assert not (workspace / "new").exists()
    assert write(workspace, runs, "empty.txt", "", max_bytes=0)["bytes_written"] == 0


def test_write_invalid_unicode_and_non_regular_targets_are_rejected(tmp_path: Path) -> None:
    """无法编码的代理字符和目录目标都不能产生文件提交。"""
    workspace, runs = make_workspace(tmp_path)
    with pytest.raises(FileToolError) as caught:
        write(workspace, runs, "a.txt", "\ud800")
    assert caught.value.code == "invalid_encoding"
    with pytest.raises(FileToolError) as caught:
        write(workspace, runs, ".", overwrite=True)
    assert caught.value.code == "not_regular_file"


def test_write_atomic_create_does_not_overwrite_racing_creator(tmp_path: Path, monkeypatch) -> None:
    """在最终排他创建前出现竞争文件时保留竞争文件，不能退回 replace。"""
    workspace, runs = make_workspace(tmp_path)
    original_link = os.link

    def race_create(source, target, **kwargs):
        """模拟另一个调用者恰在提交前创建目标文件。"""
        Path(target).write_text("concurrent owner", encoding="utf-8")
        return original_link(source, target, **kwargs)

    monkeypatch.setattr("agent_harness.tools.files.os.link", race_create)
    with pytest.raises(FileToolError) as caught:
        write(workspace, runs, "a.txt", "must not replace")
    assert caught.value.code == "file_exists"
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "concurrent owner"
    assert list(workspace.glob(".harness-write-*")) == []


def test_failed_atomic_replace_retains_original_and_hides_system_details(tmp_path: Path, monkeypatch) -> None:
    """原子替换失败不损坏原文件，不泄露系统错误正文，并清理临时文件。"""
    workspace, runs = make_workspace(tmp_path)
    target = workspace / "a.txt"
    target.write_text("original", encoding="utf-8")

    def fail_replace(*args, **kwargs):
        """模拟文件系统在提交阶段拒绝替换，且异常带有不可回显的内容。"""
        raise OSError("DO_NOT_ECHO_PRIVATE_PATH")

    monkeypatch.setattr("agent_harness.tools.files.os.replace", fail_replace)
    with pytest.raises(FileToolError) as caught:
        write(workspace, runs, "a.txt", "new", overwrite=True)
    assert caught.value.code == "file_write_error"
    assert "DO_NOT_ECHO" not in str(caught.value)
    assert target.read_text(encoding="utf-8") == "original"
    assert list(workspace.glob(".harness-write-*")) == []


def test_unsupported_atomic_link_does_not_fall_back_to_unsafe_write(tmp_path: Path, monkeypatch) -> None:
    """文件系统不支持排他硬链接时安全失败，不使用可能覆盖竞争文件的回退。"""
    workspace, runs = make_workspace(tmp_path)

    def fail_link(*args, **kwargs):
        """模拟不支持硬链接的文件系统。"""
        raise OSError("hard links unavailable")

    monkeypatch.setattr("agent_harness.tools.files.os.link", fail_link)
    with pytest.raises(FileToolError) as caught:
        write(workspace, runs, "a.txt")
    assert caught.value.code == "file_write_error"
    assert not (workspace / "a.txt").exists()
    assert list(workspace.glob(".harness-write-*")) == []


@pytest.mark.parametrize("requested,code", [
    ("../outside.txt", "path_outside_workspace"),
    (".runs/events.jsonl", "run_records_forbidden"),
    (".runs/../public.txt", "run_records_forbidden"),
])
def test_write_rejects_outside_and_lexical_record_paths(tmp_path: Path, requested: str, code: str) -> None:
    """文件写入统一拒绝越界、记录路径以及经过记录目录再返回公开区域的路径。"""
    workspace, runs = make_workspace(tmp_path)
    with pytest.raises(FileToolError) as caught:
        write(workspace, runs, requested)
    assert caught.value.code == code
    assert not (tmp_path / "outside.txt").exists()
    assert not runs.exists()


def test_workspace_symlink_aliases_cannot_bypass_record_or_external_boundary(tmp_path: Path) -> None:
    """正向外部链接、反向记录别名和记录内转出链接均无法绕过统一边界。"""
    workspace, runs = make_workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    private = workspace / "private"
    private.mkdir()
    public = workspace / "public.txt"
    public.write_text("public", encoding="utf-8")
    try:
        runs.symlink_to(private, target_is_directory=True)
        (workspace / "alias").symlink_to(private, target_is_directory=True)
        (workspace / "external").symlink_to(outside, target_is_directory=True)
        (private / "public-link.txt").symlink_to(public)
    except OSError:
        pytest.skip("当前环境没有创建符号链接的权限")
    boundary = Workspace(workspace, runs)
    for requested, code in [("alias/new.txt", "run_records_forbidden"),
                            (".runs/public-link.txt", "run_records_forbidden"),
                            ("external/new.txt", "path_outside_workspace")]:
        with pytest.raises(WorkspaceError) as caught:
            boundary.resolve(requested)
        assert caught.value.code == code
    with pytest.raises(FileToolError):
        write(workspace, runs, "external/new.txt")
    assert not (outside / "new.txt").exists()
    result = search(workspace, runs)
    assert result["matches"] == []
    assert result["skipped"]["symbolic_link"] == 3
    assert result["skipped"]["run_records"] == 1


def test_write_detects_parent_path_change_before_commit(tmp_path: Path, monkeypatch) -> None:
    """临时文件写好后路径解析变化会拒绝提交，保留已有目标。"""
    workspace, runs = make_workspace(tmp_path)
    target = workspace / "a.txt"
    target.write_text("original", encoding="utf-8")
    original_resolve = Workspace.resolve
    seen = 0

    def change_path(self, requested, *, must_exist=False):
        """在第三次目标检查时模拟路径被重定向至工作区内另一个位置。"""
        nonlocal seen
        result = original_resolve(self, requested, must_exist=must_exist)
        if requested == "a.txt":
            seen += 1
            if seen >= 3:
                return workspace / "different.txt"
        return result

    monkeypatch.setattr(Workspace, "resolve", change_path)
    with pytest.raises(FileToolError) as caught:
        write(workspace, runs, "a.txt", "new", overwrite=True)
    assert caught.value.code == "file_write_error"
    assert target.read_text(encoding="utf-8") == "original"
    assert list(workspace.glob(".harness-write-*")) == []


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 存在设备名和 NTFS 流路径规则")
@pytest.mark.parametrize("requested", ["file.txt:stream", "NUL", "nested/CON.txt", "trailing. "])
def test_windows_device_and_stream_paths_are_invalid(tmp_path: Path, requested: str) -> None:
    """Windows 设备名和备用流路径不能冒充普通工作区文件。"""
    workspace, runs = make_workspace(tmp_path)
    with pytest.raises(WorkspaceError) as caught:
        Workspace(workspace, runs).resolve(requested)
    assert caught.value.code == "invalid_path"


def test_workspace_rejects_missing_outside_before_existence_check(tmp_path: Path) -> None:
    """要求存在的路径也先校验范围，不根据宿主外部文件是否存在改变越界错误。"""
    workspace, runs = make_workspace(tmp_path)
    with pytest.raises(WorkspaceError) as caught:
        Workspace(workspace, runs).resolve("../missing-secret", must_exist=True)
    assert caught.value.code == "path_outside_workspace"
