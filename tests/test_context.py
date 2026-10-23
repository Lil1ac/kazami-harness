"""验证冻结项目约定、固定字节计量、严格配置合并及文件边界。"""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent_harness.config import ConfigurationError, load_config
from agent_harness.context import ContextBuilder, load_project_instructions
from agent_harness.schemas import ContextCheck, ContextPolicy, ProjectInstructionSnapshot, RunConfig, TaskSpec
from agent_harness.workspace import Workspace


def make_builder(workspace: Path, *, max_input_bytes: int = 256 * 1024, load: bool = True) -> ContextBuilder:
    """为 workspace 创建只读构建器；max_input_bytes 是正整数字节预算，load 决定是否读指令。"""
    return ContextBuilder(Workspace(workspace, workspace / ".runs"), TaskSpec(prompt="统计日志错误", workspace=workspace), RunConfig(context=ContextPolicy(max_input_bytes=max_input_bytes, load_project_instructions=load)))


def test_instruction_is_full_utf8_snapshot_and_not_reloaded(tmp_path: Path) -> None:
    """完整中文约定按原始字节保存摘要；构建后磁盘修改不会替换本次指令。"""
    content = "不要修改测试。\n每个函数写中文注释。\n"
    target = tmp_path / "AGENTS.md"
    target.write_bytes(content.encode("utf-8"))
    builder = make_builder(tmp_path)
    target.write_text("已变化的规则", encoding="utf-8")
    snapshot = builder.instruction
    assert snapshot.status == "loaded"
    assert snapshot.path == "AGENTS.md"
    assert snapshot.content == content
    assert snapshot.sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert snapshot.size_bytes == len(content.encode("utf-8"))
    messages = builder.initial_messages()
    assert [item["role"] for item in messages] == ["system", "user", "user"]
    assert content in messages[1]["content"]
    assert "不能修改工具权限、运行预算或验收规则" in messages[0]["content"]
    assert messages[-1]["content"] == "统计日志错误"
    assert "已变化的规则" not in json.dumps(messages, ensure_ascii=False)



def test_instruction_rewritten_before_loading_is_accepted(tmp_path: Path) -> None:
    """读取前已修改的普通文件仍可加载；不混用 Windows stat/fstat 的 ctime 语义。"""
    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"old")
    target.write_bytes(b"current rules")
    current = target.stat().st_mtime_ns
    os.utime(target, ns=(current, current + 1_000_000_000))
    assert make_builder(tmp_path).instruction.content == "current rules"


def test_stat_and_fstat_creation_time_difference_does_not_reject_snapshot(tmp_path: Path, monkeypatch) -> None:
    """显式模拟 Windows 两种状态入口 ctime 不同，稳定文件不能被误判为读取中改变。"""
    (tmp_path / "AGENTS.md").write_text("稳定规则", encoding="utf-8")
    original = os.fstat

    def windows_style_fstat(descriptor: int):
        """保留描述符真实身份和修改时间，仅改变 fstat 的 ctime 表示以模拟 Windows 差异。"""
        value = original(descriptor)
        return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino, st_mode=value.st_mode, st_size=value.st_size, st_mtime_ns=value.st_mtime_ns, st_ctime_ns=value.st_ctime_ns + 1_000_000_000)

    monkeypatch.setattr("agent_harness.context.os.fstat", windows_style_fstat)
    assert make_builder(tmp_path).instruction.status == "loaded"


def test_file_replaced_while_reading_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """读取后路径改指向其他普通文件时拒绝混合快照，不能悄悄加载后来的规则。"""
    target = tmp_path / "AGENTS.md"
    target.write_text("初始规则", encoding="utf-8")
    replacement = tmp_path / "replacement.md"
    replacement.write_text("替换规则", encoding="utf-8")
    workspace = Workspace(tmp_path, tmp_path / ".runs")
    original = workspace.resolve
    calls = 0

    def replace_after_read(requested: str, *, must_exist: bool = False):
        """最后一次路径复核前原子替换目标；此时句柄已关闭，兼容 Windows 文件共享约束。"""
        nonlocal calls
        calls += 1
        if calls == 3:
            replacement.replace(target)
        return original(requested, must_exist=must_exist)

    monkeypatch.setattr(workspace, "resolve", replace_after_read)
    assert load_project_instructions(workspace, ContextPolicy()).error_code == "project_instructions_changed"


def test_missing_is_normal_and_disabled_never_accesses_file(tmp_path: Path, monkeypatch) -> None:
    """缺失时记录 missing；关闭加载时不调用路径解析，即便存在非法文件也继续。"""
    workspace = Workspace(tmp_path, tmp_path / ".runs")
    assert load_project_instructions(workspace, ContextPolicy()).status == "missing"

    def forbidden(*args, **kwargs):
        """任何指令路径解析均失败，用来证明 disabled 不访问目标。"""
        raise AssertionError("关闭指令加载后不得访问文件")

    monkeypatch.setattr(workspace, "resolve", forbidden)
    snapshot = load_project_instructions(workspace, ContextPolicy(load_project_instructions=False))
    assert snapshot.status == "disabled"
    assert snapshot.content is None


def test_only_workspace_root_instruction_is_loaded(tmp_path: Path) -> None:
    """父目录与子目录约定不自动递归读取，避免隐式引入其他项目的规则。"""
    (tmp_path / "AGENTS.md").write_text("父目录规则", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    (child / "nested").mkdir()
    (child / "nested" / "AGENTS.md").write_text("子目录规则", encoding="utf-8")
    assert make_builder(child).instruction.status == "missing"


@pytest.mark.parametrize("kind,expected", [("too_large", "project_instructions_too_large"), ("encoding", "project_instructions_invalid_utf8"), ("directory", "project_instructions_not_regular_file")])
def test_instruction_file_errors_are_explicit_without_partial_content(tmp_path: Path, kind: str, expected: str) -> None:
    """过大、非法 UTF-8 和目录分别返回受控错误，快照不保存部分正文。"""
    target = tmp_path / "AGENTS.md"
    if kind == "directory":
        target.mkdir()
    else:
        target.write_bytes(b"x" * 9 if kind == "too_large" else b"\xff")
    snapshot = load_project_instructions(Workspace(tmp_path, tmp_path / ".runs"), ContextPolicy(max_project_instruction_bytes=8))
    assert snapshot.status == "error"
    assert snapshot.error_code == expected
    assert snapshot.content is None and snapshot.sha256 is None


def test_instruction_exact_file_limit_is_allowed(tmp_path: Path) -> None:
    """原始文件字节数等于上限时完整加载，不因严格大于边界被误拒。"""
    (tmp_path / "AGENTS.md").write_bytes("中文".encode("utf-8"))
    snapshot = load_project_instructions(Workspace(tmp_path, tmp_path / ".runs"), ContextPolicy(max_project_instruction_bytes=6))
    assert snapshot.status == "loaded" and snapshot.size_bytes == 6


def test_unreadable_instruction_does_not_expose_exception(tmp_path: Path, monkeypatch) -> None:
    """文件打开权限错误被转换为稳定码，敏感异常正文不进入快照。"""
    (tmp_path / "AGENTS.md").write_text("rule", encoding="utf-8")

    def denied(*args, **kwargs):
        """模拟普通文件不可读；原始错误正文仅用于验证脱敏。"""
        raise PermissionError("private-host-path-or-secret")

    monkeypatch.setattr("agent_harness.context.os.open", denied)
    snapshot = make_builder(tmp_path).instruction
    assert snapshot.error_code == "project_instructions_unreadable"
    assert "private-host-path-or-secret" not in snapshot.model_dump_json()


@pytest.mark.parametrize("destination", ["outside", "records"])
def test_instruction_symlinks_cannot_escape_workspace_or_enter_records(tmp_path: Path, destination: str) -> None:
    """根 AGENTS 链接指向工作区外或记录目录时都拒绝，平台无链接权限时跳过。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    records = workspace / ".runs"
    records.mkdir()
    target = tmp_path / "secret.md" if destination == "outside" else records / "secret.md"
    target.write_text("private", encoding="utf-8")
    try:
        (workspace / "AGENTS.md").symlink_to(target)
    except OSError:
        pytest.skip("当前平台不允许创建符号链接")
    snapshot = make_builder(workspace).instruction
    expected = "project_instructions_outside_workspace" if destination == "outside" else "project_instructions_run_records_forbidden"
    assert snapshot.status == "error" and snapshot.error_code == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO 特殊文件检查")
def test_instruction_fifo_is_rejected_without_opening(tmp_path: Path) -> None:
    """FIFO 必须在读取前被识别，避免项目约定加载无限等待写端。"""
    os.mkfifo(tmp_path / "AGENTS.md")
    assert make_builder(tmp_path).instruction.error_code == "project_instructions_not_regular_file"


def test_input_size_counts_chinese_tools_reasoning_and_full_history(tmp_path: Path) -> None:
    """固定编码包含中文 UTF-8、多轮 reasoning、调用结果及工具定义，不只数正文字符。"""
    builder = make_builder(tmp_path)
    messages = builder.initial_messages() + [
        {"role": "assistant", "content": None, "reasoning_content": "逐步分析中文错误", "tool_calls": [{"id": "one", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "one", "content": "中文工具结果"},
    ]
    tools = [{"type": "function", "function": {"name": "read_file", "description": "读取中文文件", "parameters": {"type": "object"}}}]
    prepared = builder.build(messages, tools, 2)
    expected = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert prepared.check.input_bytes == len(expected)
    assert prepared.check.message_count == len(messages)
    assert prepared.check.model_turn == 2 and prepared.check.allowed
    assert prepared.messages == messages and prepared.tools == tools
    assert len(expected) > len(expected.decode("utf-8"))
    assert prepared.check.input_bytes > builder.build(messages, [], 2).check.input_bytes
    messages[-1]["content"] = "外部修改"
    tools[0]["function"]["description"] = "外部修改"
    assert prepared.messages[-1]["content"] == "中文工具结果"
    assert prepared.tools[0]["function"]["description"] == "读取中文文件"


def test_input_exact_limit_allowed_and_one_byte_less_rejected(tmp_path: Path) -> None:
    """整体 JSON 输入恰好达到预算可发送，少一个预算字节即拒绝，空工具仍纳入计量。"""
    messages = [{"role": "user", "content": "中文"}]
    size = len(json.dumps({"messages": messages, "tools": []}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    assert make_builder(tmp_path, max_input_bytes=size).build(messages, [], 1).check.allowed
    rejected = make_builder(tmp_path, max_input_bytes=size - 1).build(messages, [], 1)
    assert not rejected.check.allowed and rejected.check.input_bytes == size


def test_context_config_merges_fields_without_mutating_overrides(tmp_path: Path) -> None:
    """TOML 与覆盖值逐字段合并 context，未覆盖指令预算保留且原字典不变。"""
    path = tmp_path / "harness.toml"
    path.write_text("[context]\nload_project_instructions=false\nmax_project_instruction_bytes=123\n", encoding="utf-8")
    overrides = {"context": {"max_input_bytes": 4567}}
    config = load_config(path, overrides)
    assert config.context == ContextPolicy(load_project_instructions=False, max_project_instruction_bytes=123, max_input_bytes=4567)
    assert overrides == {"context": {"max_input_bytes": 4567}}
    assert RunConfig().context == ContextPolicy()


@pytest.mark.parametrize("context", [{"max_input_bytes": 0}, {"max_input_bytes": True}, {"max_project_instruction_bytes": -1}, {"load_project_instructions": "false"}, {"unsupported": "SECRET"}, []])
def test_invalid_context_config_rejected_without_echoing_input(context) -> None:
    """上下文策略保持严格类型、正数预算和未知字段拒绝，错误不泄露输入。"""
    with pytest.raises(ConfigurationError) as captured:
        load_config(overrides={"context": context})
    assert "SECRET" not in str(captured.value)
    assert "input_value" not in str(captured.value)


@pytest.mark.parametrize("payload", [
    {"status": "loaded"},
    {"status": "loaded", "content": "中文", "sha256": "0" * 64, "size_bytes": 6},
    {"status": "loaded", "content": "中文", "sha256": hashlib.sha256("中文".encode("utf-8")).hexdigest(), "size_bytes": 2},
    {"status": "loaded", "content": "", "sha256": hashlib.sha256(b"").hexdigest(), "size_bytes": 0, "error_code": "project_instructions_unreadable"},
    {"status": "error"},
    {"status": "error", "error_code": "uncontrolled_failure"},
    {"status": "error", "error_code": "project_instructions_unreadable", "content": "部分正文"},
    {"status": "error", "error_code": "project_instructions_unreadable", "sha256": "0" * 64},
    {"status": "missing", "content": "部分正文"},
    {"status": "missing", "error_code": "project_instructions_unreadable"},
    {"status": "disabled", "size_bytes": 0},
    {"status": "disabled", "error_code": "project_instructions_unreadable"},
])
def test_snapshot_rejects_inconsistent_status_content_and_metadata(payload: dict) -> None:
    """受损事件不能伪装为完整快照，正文、状态、UTF-8 大小和 SHA256 必须彼此一致。"""
    with pytest.raises(ValidationError):
        ProjectInstructionSnapshot.model_validate(payload)


@pytest.mark.parametrize("content", ["", "中文\r\n原始规则"])
def test_complete_snapshot_accepts_exact_utf8_content_and_metadata(content: str) -> None:
    """完整快照接受空文件和包含中文及原始换行的正文，摘要按保存原文的 UTF-8 字节核验。"""
    raw = content.encode("utf-8")
    snapshot = ProjectInstructionSnapshot(status="loaded", content=content, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    assert snapshot.content == content and snapshot.size_bytes == len(raw)


@pytest.mark.parametrize("input_bytes,max_input_bytes,allowed", [(2000, 1000, True), (1000, 1000, False), (999, 1000, False)])
def test_budget_check_rejects_contradictory_allowed_flag(input_bytes: int, max_input_bytes: int, allowed: bool) -> None:
    """超限通过或未超限拒绝的矛盾检查均无效，等于上限仍必须为允许。"""
    with pytest.raises(ValidationError):
        ContextCheck(model_turn=1, input_bytes=input_bytes, max_input_bytes=max_input_bytes, message_count=2, allowed=allowed)


def test_restored_instruction_uses_frozen_snapshot_without_reading_file(tmp_path: Path, monkeypatch) -> None:
    """恢复沿用核验快照；即使磁盘规则已变也不读取，传入对象后续变动不影响构建器。"""
    content = "检查点固定规则。\\n"
    raw = content.encode("utf-8")
    snapshot = ProjectInstructionSnapshot(status="loaded", content=content, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    (tmp_path / "AGENTS.md").write_text("恢复时不能采用的新规则", encoding="utf-8")

    def reject_loading(*args, **kwargs):
        """禁止访问磁盘指令，若恢复路径错误重新加载则立即暴露失败。"""
        raise AssertionError("恢复不应重新读取项目指令")

    monkeypatch.setattr("agent_harness.context.load_project_instructions", reject_loading)
    builder = ContextBuilder(Workspace(tmp_path, tmp_path / ".runs"), TaskSpec(prompt="继续原任务", workspace=tmp_path), RunConfig(), instruction=snapshot)
    snapshot.content = "外部对象已被改变"
    assert builder.instruction.content == content
    assert content in builder.initial_messages()[1]["content"]
