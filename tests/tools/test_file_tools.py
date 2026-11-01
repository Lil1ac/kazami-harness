"""验证只读文件工具的分页、工作区边界和安全错误协议。"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_harness.schemas import RunConfig, RunLimits
from agent_harness.tools import ToolExecutor


def executor(tmp_path: Path, *, preview_chars: int = 12000) -> ToolExecutor:
    """创建独立工作区和只启用 read_file 的执行器，允许指定输出字符限额。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ToolExecutor(
        workspace,
        workspace / ".runs",
        RunConfig(tools=["read_file"], limits=RunLimits(tool_output_preview_chars=preview_chars)),
    )


def read(tool: ToolExecutor, path: str, **kwargs) -> dict:
    """同步调用异步文件工具，把路径与附加参数编码为模型传入的 JSON 字符串。"""
    return asyncio.run(tool.execute("read_file", json.dumps({"path": path, **kwargs})))


def assert_error(result: dict, code: str) -> None:
    """检查工具失败结果的统一格式，以及预期错误码和非空说明。"""
    assert result["ok"] is False
    assert result["data"] is None
    assert result["error"]["code"] == code
    assert result["error"]["message"]
    assert result["truncated"] is False


def test_definitions_only_describe_enabled_implemented_tools(tmp_path: Path) -> None:
    """验证工具声明只公开已启用且已实现的能力，参数 Schema 保留严格约束。"""
    tool = executor(tmp_path)
    tool.config.tools = ["read_file"]
    definitions = tool.definitions
    assert [item["function"]["name"] for item in definitions] == ["read_file"]
    schema = definitions[0]["function"]["parameters"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["max_bytes"]["maximum"] == 65536
    assert "next_offset" in definitions[0]["function"]["description"]
    tool.config.tools = ["write_file"]
    assert "read_file" not in [item["function"]["name"] for item in tool.definitions]
    assert_error(read(tool, "a.txt"), "unknown_tool")


def test_utf8_byte_pages_reconstruct_chinese_and_emoji(tmp_path: Path) -> None:
    """验证按返回偏移分页能完整重建中文与表情文本，不拆开 UTF-8 字符。"""
    tool = executor(tmp_path)
    text = "A中文🙂尾巴\nZ"
    (tool.workspace / "日志.txt").write_bytes(text.encode("utf-8"))
    offset = 0
    pages = []
    while True:
        result = read(tool, "日志.txt", offset=offset, max_bytes=4)
        assert result["ok"] is True
        data = result["data"]
        pages.append(data["content"])
        assert data["offset"] == offset
        assert data["bytes_read"] == len(data["content"].encode("utf-8")) <= 4
        assert data["path"] == "日志.txt"
        assert result["truncated"] == (not data["eof"])
        if data["eof"]:
            assert data["next_offset"] is None
            break
        assert data["next_offset"] > offset
        offset = data["next_offset"]
    assert "".join(pages) == text


def test_preview_character_limit_keeps_byte_offsets_usable(tmp_path: Path) -> None:
    """验证字符预览裁剪后仍按实际返回字节推进偏移，不遗漏或重复文本。"""
    tool = executor(tmp_path, preview_chars=2)
    (tool.workspace / "a.txt").write_text("中🙂abc", encoding="utf-8")
    first = read(tool, "a.txt", max_bytes=65536)
    assert first["data"]["content"] == "中🙂"
    assert first["data"]["bytes_read"] == first["data"]["next_offset"] == 7
    assert first["truncated"] is True
    second = read(tool, "a.txt", offset=7)
    assert second["data"]["content"] == "ab"
    assert second["data"]["next_offset"] == 9
    third = read(tool, "a.txt", offset=9)
    assert third["data"]["content"] == "c"
    assert third["data"]["eof"] is True


@pytest.mark.parametrize("content", ["", "abcd", "🙂"])
def test_empty_and_exact_boundary_pages_report_eof(tmp_path: Path, content: str) -> None:
    """验证空文件、恰好一页以及文件末尾读取均正确返回 eof 和 next_offset。"""
    tool = executor(tmp_path)
    (tool.workspace / "a.txt").write_text(content, encoding="utf-8")
    result = read(tool, "a.txt", max_bytes=4)
    assert result["ok"] is True
    assert result["data"]["content"] == content
    assert result["data"]["eof"] is True
    at_end = read(tool, "a.txt", offset=len(content.encode("utf-8")))
    assert at_end["data"]["content"] == ""
    assert at_end["data"]["next_offset"] is None


def test_offset_inside_character_and_beyond_eof_are_rejected(tmp_path: Path) -> None:
    """验证字符内部偏移、超出末尾和极大偏移都返回明确错误。"""
    tool = executor(tmp_path)
    (tool.workspace / "a.txt").write_text("中🙂", encoding="utf-8")
    for offset in [1, 2, 4, 5, 6, 8, 10**100]:
        assert_error(read(tool, "a.txt", offset=offset), "invalid_offset")


@pytest.mark.parametrize("text,max_bytes", [("中", 1), ("中", 2), ("🙂", 3)])
def test_page_too_small_for_one_character_is_actionable(tmp_path: Path, text: str, max_bytes: int) -> None:
    """验证页大小不足以容纳一个完整字符时，提示增加 max_bytes。"""
    tool = executor(tmp_path)
    (tool.workspace / "a.txt").write_text(text, encoding="utf-8")
    result = read(tool, "a.txt", max_bytes=max_bytes)
    assert_error(result, "max_bytes_too_small")
    assert "4" in result["error"]["message"]


@pytest.mark.parametrize("raw", [b"\xff\xfe", b"\x80", b"\xe4\xb8", b"\xc3x", b"\xf0\x80\x80\x80"])
def test_invalid_or_incomplete_utf8_is_reported(tmp_path: Path, raw: bytes) -> None:
    """验证非法字节、残缺字符和不合法 UTF-8 序列都被识别为编码错误。"""
    tool = executor(tmp_path)
    (tool.workspace / "a.txt").write_bytes(raw)
    assert_error(read(tool, "a.txt"), "invalid_encoding")


@pytest.mark.parametrize("raw,max_bytes", [(b"A\xe4\xb8", 2), (b"\xe4x", 1)])
def test_cutoff_does_not_hide_broken_character(tmp_path: Path, raw: bytes, max_bytes: int) -> None:
    """验证分页边界附近的损坏字符不会被误当成正常截断而隐藏。"""
    tool = executor(tmp_path)
    (tool.workspace / "a.txt").write_bytes(raw)
    assert_error(read(tool, "a.txt", max_bytes=max_bytes), "invalid_encoding")


def test_parent_and_absolute_outside_paths_cannot_escape_workspace(tmp_path: Path) -> None:
    """验证父级路径、外部绝对路径和同名前缀目录不能越过工作区边界。"""
    tool = executor(tmp_path)
    outside = tmp_path / "secret-host-file.txt"
    outside.write_text("DO_NOT_EXPOSE", encoding="utf-8")
    sibling = tmp_path / "workspace-other"
    sibling.mkdir()
    (sibling / "a.txt").write_text("DO_NOT_EXPOSE", encoding="utf-8")
    for requested in ["../secret-host-file.txt", str(outside), str(sibling / "a.txt")]:
        result = read(tool, requested)
        assert_error(result, "path_outside_workspace")
        assert "DO_NOT_EXPOSE" not in json.dumps(result)
        assert "secret-host-file" not in json.dumps(result)


def test_resolved_internal_paths_and_absolute_inside_path_work(tmp_path: Path) -> None:
    """验证工作区内的规范化相对路径和绝对路径均可读取，并返回相对名称。"""
    tool = executor(tmp_path)
    child = tool.workspace / "child"
    child.mkdir()
    file = tool.workspace / "a.txt"
    file.write_text("ok", encoding="utf-8")
    for requested in ["child/../a.txt", str(file)]:
        result = read(tool, requested)
        assert result["ok"] is True
        assert result["data"]["path"] == "a.txt"


def test_all_run_record_descendants_are_forbidden(tmp_path: Path) -> None:
    """验证记录目录及所有子路径均禁止读取，同时允许仅有相同名称前缀的文件。"""
    tool = executor(tmp_path)
    private = tool.runs_root / "another-run" / "artifacts"
    private.mkdir(parents=True)
    (private / "private.txt").write_text("DO_NOT_EXPOSE", encoding="utf-8")
    for requested in [".runs", ".runs/another-run/artifacts/private.txt", ".runs/new-run/events.jsonl"]:
        result = read(tool, requested)
        assert_error(result, "run_records_forbidden")
        assert "DO_NOT_EXPOSE" not in json.dumps(result)
    # 组件比较允许同名前缀目录，不能误判为运行记录子目录。
    (tool.workspace / ".runs-public.txt").write_text("allowed", encoding="utf-8")
    assert read(tool, ".runs-public.txt")["ok"] is True


def test_runs_root_parent_of_workspace_blocks_workspace_reads(tmp_path: Path) -> None:
    """验证当整个工作区位于记录目录下时，记录保护规则优先阻止读取。"""
    tool = executor(tmp_path)
    tool.runs_root = tmp_path
    (tool.workspace / "a.txt").write_text("ok", encoding="utf-8")
    assert_error(read(tool, "a.txt"), "run_records_forbidden")


def test_outside_and_record_symlinks_are_rejected_but_internal_link_works(tmp_path: Path) -> None:
    """验证外部和记录链接被拒绝，内部链接可用，记录内链接也不能绕过保护。"""
    tool = executor(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("HOST_SECRET", encoding="utf-8")
    tool.runs_root.mkdir()
    record = tool.runs_root / "events.jsonl"
    record.write_text("RECORD_SECRET", encoding="utf-8")
    inside = tool.workspace / "inside.txt"
    inside.write_text("allowed", encoding="utf-8")
    try:
        (tool.workspace / "outside-link.txt").symlink_to(outside)
        (tool.workspace / "record-link.txt").symlink_to(record)
        (tool.workspace / "inside-link.txt").symlink_to(inside)
        (tool.runs_root / "public-link.txt").symlink_to(inside)
    except OSError:
        pytest.skip("当前环境没有创建符号链接的权限")
    assert_error(read(tool, "outside-link.txt"), "path_outside_workspace")
    assert_error(read(tool, "record-link.txt"), "run_records_forbidden")
    assert read(tool, "inside-link.txt")["data"]["content"] == "allowed"
    assert_error(read(tool, ".runs/public-link.txt"), "run_records_forbidden")


def test_missing_and_non_regular_paths_are_safe_errors(tmp_path: Path) -> None:
    """验证文件缺失和目录输入返回稳定错误，错误内容不泄露请求路径。"""
    tool = executor(tmp_path)
    assert_error(read(tool, "DO_NOT_ECHO_missing.txt"), "file_not_found")
    assert_error(read(tool, "."), "not_regular_file")
    assert "DO_NOT_ECHO" not in json.dumps(read(tool, "DO_NOT_ECHO_missing.txt"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="当前平台没有 POSIX FIFO")
def test_fifo_is_rejected_without_waiting_for_writer(tmp_path: Path) -> None:
    """验证 POSIX 命名管道被当作非普通文件拒绝，读取不会等待写入者而阻塞。"""
    tool = executor(tmp_path)
    os.mkfifo(tool.workspace / "pipe")
    assert_error(read(tool, "pipe"), "not_regular_file")


@pytest.mark.parametrize(
    "arguments",
    [
        "{broken DO_NOT_ECHO", "null", "[]", "42", '"DO_NOT_ECHO"',
        {}, {"path": ""}, {"path": "   "}, {"path": 123}, {"path": "\x00"},
        {"path": "a", "offset": -1}, {"path": "a", "offset": "1"},
        {"path": "a", "offset": True}, {"path": "a", "max_bytes": 0},
        {"path": "a", "max_bytes": 65537}, {"path": "a", "max_bytes": "4"},
        {"path": "a", "max_bytes": False}, {"path": "a", "secret": "DO_NOT_ECHO"},
    ],
)
def test_invalid_json_types_limits_and_extra_parameters_are_safe(tmp_path: Path, arguments) -> None:
    """参数化验证非法 JSON、参数类型、范围和多余字段，错误均不回显原值。"""
    tool = executor(tmp_path)
    wire = arguments if isinstance(arguments, str) else json.dumps(arguments)
    result = asyncio.run(tool.execute("read_file", wire))
    assert_error(result, "invalid_arguments")
    serialized = json.dumps(result)
    assert "DO_NOT_ECHO" not in serialized
    assert "input_value" not in serialized


def test_unknown_tool_is_structured_without_evaluating_arguments(tmp_path: Path) -> None:
    """验证未知工具直接返回结构化错误，不解析参数，也不泄露工具名与参数。"""
    tool = executor(tmp_path)
    result = asyncio.run(tool.execute("DO_NOT_ECHO_TOOL", "DO_NOT_ECHO_ARGUMENTS"))
    assert_error(result, "unknown_tool")
    assert "DO_NOT_ECHO" not in json.dumps(result)


def test_os_errors_do_not_echo_private_details(tmp_path: Path, monkeypatch) -> None:
    """验证底层权限错误转换为安全工具结果，不暴露系统异常正文。"""
    tool = executor(tmp_path)
    (tool.workspace / "a.txt").write_text("ok", encoding="utf-8")

    def denied(*args, **kwargs):
        """模拟携带私密正文的打开文件权限错误，检查错误转换是否会泄露正文。"""
        raise PermissionError("DO_NOT_ECHO_OS_DETAILS")

    monkeypatch.setattr("agent_harness.tools.files.os.open", denied)
    result = read(tool, "a.txt")
    assert_error(result, "permission_denied")
    assert "DO_NOT_ECHO" not in json.dumps(result)
