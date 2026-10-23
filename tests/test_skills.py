"""验证技能发现、显式选择、规范元数据与宿主文件边界。"""

import hashlib
import os
from pathlib import Path

import pytest

import agent_harness.skills as skills
from agent_harness.skills import SkillCatalog, SkillError


def skill(root: Path, name: str = "api-development", *, header: str | None = None, body: str = "按需求修改接口并运行测试。") -> Path:
    """在测试集合 root 写入一个 UTF-8 技能，返回 SKILL.md；header 为 None 时使用合法默认元数据。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    frontmatter = header if header is not None else f"name: {name}\ndescription: 开发 API 时使用"
    path = directory / "SKILL.md"
    path.write_bytes(f"---\n{frontmatter}\n---\n{body}\n".encode("utf-8"))
    return path


def test_constructor_and_empty_selection_do_not_touch_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构造及空选择不访问不存在的目录，默认未启用技能不会增加文件访问或异常。"""
    def forbidden(*args, **kwargs):
        """拒绝任何文件探测调用，确保空选择提前返回。"""
        raise AssertionError("unexpected filesystem access")
    monkeypatch.setattr(Path, "lstat", forbidden)
    catalog = SkillCatalog([tmp_path / "not-created"])
    monkeypatch.setattr(catalog, "_scan", forbidden)
    assert catalog.load([]) == []


def test_selected_snapshot_keeps_exact_bytes_and_order(tmp_path: Path) -> None:
    """保存含 CRLF 的完整原始正文和指纹，所选顺序与调用方一致且无额外技能。"""
    first = skill(tmp_path, "first")
    second = skill(tmp_path, "second")
    raw = first.read_bytes().replace(b"\n", b"\r\n")
    first.write_bytes(raw)
    loaded = SkillCatalog([tmp_path]).load(["second", "first"])
    assert [item.name for item in loaded] == ["second", "first"]
    snapshot = loaded[1]
    assert snapshot.content.encode("utf-8") == raw
    assert snapshot.size_bytes == len(raw)
    assert snapshot.sha256 == hashlib.sha256(raw).hexdigest()
    assert snapshot.source == str(first)
    assert loaded[0].source == str(second)


def test_discovery_reports_damage_but_load_ignores_unselected_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目录展示缺失正文的原因；明确加载另一技能时，不读取损坏项。"""
    skill(tmp_path, "good")
    (tmp_path / "broken").mkdir()
    catalog = SkillCatalog([tmp_path])
    options = {item.name: item for item in catalog.discover()}
    assert options["good"].available
    assert not options["broken"].available
    assert options["broken"].error_code == "skill_unreadable"
    original = catalog._read
    def read_selected(candidate):
        """断言 load 仅读取用户选择的候选项，返回原安全加载结果。"""
        assert candidate.name == "good"
        return original(candidate)
    monkeypatch.setattr(catalog, "_read", read_selected)
    assert catalog.load(["good"])[0].name == "good"


@pytest.mark.parametrize("header", [
    "name: wrong\ndescription: 合法说明",
    "name: api-development",
    "name: api-development\ndescription: 123",
    "name: api-development\ndescription: '   '",
    "name: api-development\ndescription: " + "x" * 1025,
    "name: api-development\ndescription: ok\ncompatibility: " + "x" * 501,
    "name: api-development\ndescription: ok\nmetadata:\n  version: 1",
    "name: api-development\ndescription: ok\nallowed-tools: [Bash]",
    "name: api-development\ndescription: ok\nunrecognized-field: surprise",
    "name: api-development\ndescription: !!python/object:builtins.object {}",
    "- api-development\n- description",
])
def test_frontmatter_must_match_specification(tmp_path: Path, header: str) -> None:
    """错误类型、越界长度、目录名不匹配及自定义 YAML 标签均返回安全元数据错误。"""
    skill(tmp_path, header=header)
    with pytest.raises(SkillError) as caught:
        SkillCatalog([tmp_path]).load(["api-development"])
    assert caught.value.code == "skill_invalid_metadata"
    assert caught.value.name == "api-development"
    assert header not in caught.value.message


def test_optional_metadata_is_preserved_without_execution(tmp_path: Path) -> None:
    """规范可选字段保留在正文中；脚本目录存在时只返回快照，不执行脚本或授予工具。"""
    path = skill(tmp_path, header="""name: api-development
description: 修改接口时使用
license: MIT
compatibility: Python 3.12
metadata:
  version: "1"
allowed-tools: Bash(git:*) Read""")
    script = path.parent / "scripts"
    script.mkdir()
    (script / "run.py").write_text("raise RuntimeError('must not execute')", encoding="utf-8")
    loaded = SkillCatalog([tmp_path]).load(["api-development"])
    assert loaded[0].content == path.read_bytes().decode("utf-8")
    assert "allowed-tools: Bash(git:*) Read" in loaded[0].content


@pytest.mark.parametrize("name", ["../escape", "Upper", "bad--name", "-bad", "bad-", "x" * 65, ""])
def test_invalid_selection_names_never_resolve_paths(tmp_path: Path, name: str) -> None:
    """选择名称不允许路径或规范外字符；无需存在目录即可拒绝。"""
    with pytest.raises(SkillError) as caught:
        SkillCatalog([tmp_path / "missing"]).load([name])
    assert caught.value.code == "skill_invalid_name"


def test_duplicate_skill_names_are_not_resolved_by_root_order(tmp_path: Path) -> None:
    """两个根中的同名技能均显示冲突，实际加载拒绝，避免配置顺序决定权限或内容。"""
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        skill(root)
    catalog = SkillCatalog(roots)
    assert {item.error_code for item in catalog.discover()} == {"skill_duplicate_name"}
    with pytest.raises(SkillError) as caught:
        catalog.load(["api-development"])
    assert caught.value.code == "skill_duplicate_name"


def test_missing_root_is_reported_without_blocking_other_root(tmp_path: Path) -> None:
    """目录索引明确展示配置根缺失，而选择另一个有效根中的技能仍能正常运行。"""
    skill(tmp_path / "good")
    catalog = SkillCatalog([tmp_path / "missing", tmp_path / "good"])
    options = catalog.discover()
    assert any(not item.available and item.name == "" and item.error_code == "skill_unreadable" for item in options)
    assert catalog.load(["api-development"])[0].name == "api-development"


def test_not_found_and_duplicate_selection_have_explicit_errors(tmp_path: Path) -> None:
    """未知名称和重复选择分别拒绝，均不静默删除或替换用户选择。"""
    catalog = SkillCatalog([tmp_path])
    with pytest.raises(SkillError) as caught:
        catalog.load(["unknown"])
    assert caught.value.code == "skill_not_found"
    with pytest.raises(SkillError) as caught:
        catalog.load(["first", "first"])
    assert caught.value.code == "skill_duplicate_name"


def test_root_entry_and_selection_limits_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目录数、实际扫描项和选择数都有独立限额；索引超限不返回部分可用目录。"""
    with pytest.raises(SkillError) as caught:
        SkillCatalog([tmp_path] * 9)
    assert caught.value.code == "skill_catalog_limit"
    catalog = SkillCatalog([tmp_path])
    with pytest.raises(SkillError) as caught:
        catalog.load([f"skill-{number}" for number in range(9)])
    assert caught.value.code == "skill_selection_limit"
    for number in range(3):
        (tmp_path / f"file-{number}").write_text("not a skill", encoding="utf-8")
    monkeypatch.setattr(catalog, "MAX_ENTRIES", 2)
    options = catalog.discover()
    assert len(options) == 1 and options[0].error_code == "skill_catalog_limit"
    with pytest.raises(SkillError) as caught:
        catalog.load(["unknown"])
    assert caught.value.code == "skill_catalog_limit"


def test_file_and_selection_byte_limits_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """按原始 UTF-8 字节计算单文件和选择总量，超限整体拒绝而不截断技能约定。"""
    path = skill(tmp_path, "first")
    skill(tmp_path, "second")
    catalog = SkillCatalog([tmp_path])
    monkeypatch.setattr(catalog, "MAX_FILE_BYTES", path.stat().st_size - 1)
    with pytest.raises(SkillError) as caught:
        catalog.load(["first"])
    assert caught.value.code == "skill_file_too_large"
    monkeypatch.setattr(catalog, "MAX_FILE_BYTES", 64 * 1024)
    monkeypatch.setattr(catalog, "MAX_TOTAL_BYTES", path.stat().st_size)
    with pytest.raises(SkillError) as caught:
        catalog.load(["first", "second"])
    assert caught.value.code == "skill_total_too_large"


def test_invalid_utf8_and_missing_frontmatter_are_rejected(tmp_path: Path) -> None:
    """非 UTF-8、缺少开始或结束分隔符的文件均不能成为技能快照。"""
    path = skill(tmp_path)
    catalog = SkillCatalog([tmp_path])
    for raw, expected in [(b"\xff", "skill_invalid_encoding"), (b"plain text", "skill_invalid_metadata"),
                          (b"---\nname: api-development", "skill_invalid_metadata")]:
        path.write_bytes(raw)
        with pytest.raises(SkillError) as caught:
            catalog.load(["api-development"])
        assert caught.value.code == expected


def test_file_replacement_during_open_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """在检查后、打开前替换同路径文件，句柄身份核验拒绝读取变化后的内容。"""
    path = skill(tmp_path)
    original = skills.os.open
    def replace_then_open(target, flags, *args, **kwargs):
        """模拟文件探测与打开之间的替换，再调用真实 open 返回句柄。"""
        replacement = path.with_name("replacement.md")
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
        return original(target, flags, *args, **kwargs)
    monkeypatch.setattr(skills.os, "open", replace_then_open)
    with pytest.raises(SkillError) as caught:
        SkillCatalog([tmp_path]).load(["api-development"])
    assert caught.value.code == "skill_file_changed"


def test_special_file_and_symlink_are_not_followed(tmp_path: Path) -> None:
    """正文目录等特殊类型明确拒绝；系统允许创建链接时，集合内链接也不会被跟随。"""
    path = skill(tmp_path)
    path.unlink()
    path.mkdir()
    with pytest.raises(SkillError) as caught:
        SkillCatalog([tmp_path]).load(["api-development"])
    assert caught.value.code == "skill_path_unsafe"
    target = tmp_path / "target"
    skill(target, "valid")
    linked_root = tmp_path / "collection"
    linked_root.mkdir()
    try:
        (linked_root / "valid").symlink_to(target / "valid", target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 账户未获得创建符号链接权限。")
    catalog = SkillCatalog([linked_root])
    assert catalog.discover()[0].error_code == "skill_path_unsafe"
    with pytest.raises(SkillError) as caught:
        catalog.load(["valid"])
    assert caught.value.code == "skill_path_unsafe"
