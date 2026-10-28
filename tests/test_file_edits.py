"""局部编辑的版本、唯一匹配、原字节保持和失败无副作用回归。"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from agent_harness.schemas import RunConfig, RunLimits
from agent_harness.tools import ToolExecutor
from agent_harness.tools import files


def executor(tmp_path: Path, max_bytes: int = 1024 * 1024) -> ToolExecutor:
    """创建启用读/编辑的独立执行器；max_bytes 同时限制原文件和编辑结果，单位为字节。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ToolExecutor(workspace, workspace / ".runs", RunConfig(tools=["read_file", "edit_file"], limits=RunLimits(max_file_write_bytes=max_bytes)))


def call(tool: ToolExecutor, name: str, **arguments) -> dict:
    """把参数作为真实模型 JSON 送入工具注册层，返回完整成功或失败结果。"""
    return asyncio.run(tool.execute(name, json.dumps(arguments, ensure_ascii=True)))


def edit(tool: ToolExecutor, old: str, new: str, sha256: str | None = None, path: str = "app.py") -> dict:
    """按指定版本编辑；sha256 缺省时先走真实 read_file，返回未经投影的工具结果。"""
    if sha256 is None:
        sha256 = call(tool, "read_file", path=path)["data"]["sha256"]
    return call(tool, "edit_file", path=path, old_text=old, new_text=new, expected_sha256=sha256)


def error(result: dict, code: str) -> None:
    """核对稳定业务错误协议；只接受指定错误码，不回显原输入或系统异常。"""
    assert result["ok"] is False and result["data"] is None, result
    assert result["error"]["code"] == code, result
    assert result["error"]["message"] and not result["truncated"]
    assert "DO_NOT_ECHO" not in json.dumps(result)


def test_paginated_read_hashes_whole_file_and_edit_preserves_other_bytes(tmp_path: Path) -> None:
    """分页指纹来自完整原字节；局部改动保留中文、BOM、CRLF、尾部及原权限。"""
    tool = executor(tmp_path)
    original = "\ufeff# 中文保持🙂\r\ndef add(a, b):\r\n    return a - b\r\n# 尾部保持\r\n".encode("utf-8")
    path = tool.workspace / "app.py"
    path.write_bytes(original)
    if os.name != "nt":
        path.chmod(0o754)
    mode = stat.S_IMODE(path.stat().st_mode)
    first = call(tool, "read_file", path="app.py", max_bytes=4)
    second = call(tool, "read_file", path="app.py", offset=first["data"]["next_offset"], max_bytes=4)
    expected = hashlib.sha256(original).hexdigest()
    assert first["truncated"] and first["data"]["sha256"] == expected
    assert second["data"]["sha256"] == expected
    assert first["data"]["size_bytes"] == len(original)
    outcome = edit(tool, "return a - b", "return a + b", expected)
    changed = original.replace(b"return a - b", b"return a + b")
    assert outcome["ok"] and path.read_bytes() == changed
    assert outcome["data"] == {"path": "app.py", "replacements": 1, "bytes_written": len(changed), "sha256": hashlib.sha256(changed).hexdigest()}
    assert outcome["artifact_refs"] == [{"location": "workspace", "path": "app.py", "kind": "file"}]
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert list(tool.workspace.glob(".harness-edit-*")) == []
    # 新指纹可用于已知当前内容的下一次编辑，旧指纹不能再提交。
    error(edit(tool, "a + b", "a * b", expected), "file_changed")
    assert edit(tool, "# 尾部保持\r\n", "", outcome["data"]["sha256"])["ok"]
    assert path.read_bytes() == changed.removesuffix("# 尾部保持\r\n".encode("utf-8"))


@pytest.mark.parametrize("content,old,new,code", [
    ("alpha beta", "missing", "new", "edit_not_found"),
    ("alpha alpha", "alpha", "new", "edit_ambiguous"),
    ("aaa", "aa", "new", "edit_ambiguous"),
    ("alpha", "alpha", "alpha", "edit_no_change"),
])
def test_missing_ambiguous_overlapping_and_noop_edits_leave_original(tmp_path: Path, content: str, old: str, new: str, code: str) -> None:
    """唯一匹配或有效修改条件不成立时，错误明确且没有任何写入产物。"""
    tool = executor(tmp_path)
    path = tool.workspace / "app.py"
    path.write_bytes(content.encode("utf-8"))
    error(edit(tool, old, new), code)
    assert path.read_bytes() == content.encode("utf-8") and not tool.artifact_refs
    assert list(tool.workspace.glob(".harness-edit-*")) == []


def test_external_change_outside_target_is_detected_even_with_same_size_and_mtime(tmp_path: Path) -> None:
    """未修改目标片段但改了其他位置时，旧指纹仍拒绝覆盖；重读后可保留用户修改再编辑。"""
    tool = executor(tmp_path)
    path = tool.workspace / "app.py"
    path.write_bytes(b"# version A\nreturn a - b\n")
    previous = path.stat()
    observed = call(tool, "read_file", path="app.py")["data"]["sha256"]
    path.write_bytes(b"# version B\nreturn a - b\n")
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    error(edit(tool, "a - b", "a + b", observed), "file_changed")
    assert path.read_bytes() == b"# version B\nreturn a - b\n"
    assert edit(tool, "a - b", "a + b")["ok"]
    assert path.read_bytes() == b"# version B\nreturn a + b\n"


def test_edit_budget_bounds_source_hash_and_result_size(tmp_path: Path) -> None:
    """超限文件仍可分页但不签发编辑指纹；原文件或替换结果超限均保持原字节。"""
    tool = executor(tmp_path, max_bytes=8)
    path = tool.workspace / "app.py"
    path.write_bytes(b"123456789")
    page = call(tool, "read_file", path="app.py", max_bytes=4)
    assert page["ok"] and page["data"]["sha256"] is None and page["data"]["next_offset"] == 4
    error(edit(tool, "1", "x", hashlib.sha256(path.read_bytes()).hexdigest()), "file_size_limit")
    assert path.read_bytes() == b"123456789"
    path.write_bytes("中文".encode("utf-8"))
    error(edit(tool, "中", "中文字"), "file_size_limit")
    assert path.read_bytes() == "中文".encode("utf-8")
    assert list(tool.workspace.iterdir()) == [path]


@pytest.mark.parametrize("raw,code", [(b"abc\x00def", "binary_file"), (b"abc\xffdef", "invalid_encoding")])
def test_non_text_source_is_rejected_without_rewriting(tmp_path: Path, raw: bytes, code: str) -> None:
    """不以容错解码重写二进制或损坏编码文件，返回明确错误并保留原字节。"""
    tool = executor(tmp_path)
    path = tool.workspace / "app.py"
    path.write_bytes(raw)
    error(edit(tool, "abc", "new", hashlib.sha256(raw).hexdigest()), code)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("extra", [
    {"old_text": ""}, {"path": " "}, {"path": "\x00"}, {"expected_sha256": "wrong"},
    {"expected_sha256": "A" * 64}, {"expected_sha256": None}, {"new_text": False},
    {"replace_all": True}, {"unknown": "DO_NOT_ECHO"},
])
def test_invalid_edit_parameters_are_rejected_before_writing(tmp_path: Path, extra: dict) -> None:
    """类型、指纹、空片段和未知参数均严格校验，不默许批量替换或回显错误输入。"""
    tool = executor(tmp_path)
    path = tool.workspace / "app.py"
    path.write_bytes(b"original")
    arguments = {"path": "app.py", "old_text": "original", "new_text": "new", "expected_sha256": hashlib.sha256(b"original").hexdigest(), **extra}
    error(call(tool, "edit_file", **arguments), "invalid_arguments")
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize("path,code", [("../outside.py", "path_outside_workspace"), (".runs/events.jsonl", "run_records_forbidden"), ("missing.py", "file_not_found"), (".", "not_regular_file")])
def test_edit_uses_workspace_and_record_boundary(tmp_path: Path, path: str, code: str) -> None:
    """越界、运行记录、缺失和非普通文件均不能变成可编辑目标。"""
    tool = executor(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"DO_NOT_ECHO")
    error(edit(tool, "DO_NOT_ECHO", "new", "0" * 64, path), code)
    assert outside.read_bytes() == b"DO_NOT_ECHO" and not tool.artifact_refs


def test_disabled_edit_does_not_execute_and_schema_requires_version(tmp_path: Path) -> None:
    """工具权限和严格参数来自同一注册层；未启用时不能通过伪造调用编辑。"""
    tool = executor(tmp_path)
    definition = next(item["function"] for item in tool.definitions if item["function"]["name"] == "edit_file")
    schema = definition["parameters"]
    assert set(schema["required"]) == {"path", "old_text", "new_text", "expected_sha256"}
    assert schema["additionalProperties"] is False
    tool.config.tools = ["read_file"]
    error(call(tool, "edit_file", path="app.py"), "unknown_tool")


def test_change_while_temporary_is_written_is_rechecked_before_commit(tmp_path: Path, monkeypatch) -> None:
    """临时文件落盘时模拟用户外部修改，提交前二次核验必须保留用户内容。"""
    tool = executor(tmp_path)
    path = tool.workspace / "app.py"
    path.write_bytes(b"original")
    observed = call(tool, "read_file", path="app.py")["data"]["sha256"]
    original_fsync = files.os.fsync

    def external_edit(descriptor):
        """在真实临时文件落盘后修改目标文件，复现读取与最终提交之间的时间窗口。"""
        original_fsync(descriptor)
        path.write_bytes(b"user changed")

    monkeypatch.setattr(files.os, "fsync", external_edit)
    error(edit(tool, "original", "new", observed), "file_changed")
    assert path.read_bytes() == b"user changed"
    assert list(tool.workspace.glob(".harness-edit-*")) == [] and not tool.artifact_refs


def test_failed_replace_preserves_original_and_cleans_temporary(tmp_path: Path, monkeypatch) -> None:
    """文件系统拒绝原子提交时不损坏原文件，不回显宿主私密异常并清理临时文件。"""
    tool = executor(tmp_path)
    path = tool.workspace / "app.py"
    path.write_bytes(b"original")

    def fail_replace(*args, **kwargs):
        """仅让真实提交失败，参数由编辑器提供，模拟系统写入错误。"""
        raise OSError("DO_NOT_ECHO_SYSTEM_DETAILS")

    monkeypatch.setattr(files.os, "replace", fail_replace)
    error(edit(tool, "original", "new"), "file_edit_error")
    assert path.read_bytes() == b"original"
    assert list(tool.workspace.glob(".harness-edit-*")) == [] and not tool.artifact_refs


def test_read_detects_change_while_fingerprinting(tmp_path: Path, monkeypatch) -> None:
    """分页内容与完整指纹必须来自同一稳定版本，哈希期间出现外部编辑时拒绝本次读取。"""
    tool = executor(tmp_path)
    path = tool.workspace / "app.py"
    path.write_bytes(b"original")
    original_hash = files._hash_open_file

    def hash_then_change(stream, max_bytes):
        """完成真实有界哈希后变更目标，复现页面和版本不一致的读取窗口。"""
        value = original_hash(stream, max_bytes)
        path.write_bytes(b"external")
        return value

    monkeypatch.setattr(files, "_hash_open_file", hash_then_change)
    error(call(tool, "read_file", path="app.py"), "file_changed")
    assert path.read_bytes() == b"external"
