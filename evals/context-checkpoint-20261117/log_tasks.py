"""四个基于本机 pytest 源码快照的诊断题及其独立零模型预检。"""

from __future__ import annotations

import importlib
import importlib.metadata
import io
import os
from pathlib import Path
import sys
import types
from unittest.mock import patch

import pytest


PACKAGE_ROOT = Path(pytest.__file__).resolve().parent.parent  # 当前评测解释器安装的真实源码。
SOURCE = f"pytest {importlib.metadata.version('pytest')}（本机发行包源码）"  # 明确快照来源及版本。
TASKS = (
    {
        "id": "log-xml-c1-control",
        "title": "JUnit XML 导出转义 C1 控制字符",
        "evidence_file": "_pytest/junitxml.py",
        "early": "先定位 JUnit XML 字符过滤规则，核对现有控制字符的 #x 可见替代格式。",
        "final": "兼容导入器的严格字符集。将 U+0080 至 U+009F 转为相同的可见替代格式，继续保留合法的补充平面字符。",
        "anchor": r"\u0080-\ud7ff\ue000-\ufffd",
        "old": r"\u0080-\ud7ff",
        "new": r"\u00a0-\ud7ff",
    },
    {
        "id": "log-junit-win-path",
        "title": "JUnit 测试标识归一化混合路径分隔符",
        "evidence_file": "_pytest/junitxml.py",
        "early": "梳理节点名到 JUnit 测试名称的转换。参数方括号里的路径和分隔符是用户数据。",
        "final": "归档输入可能来自 Windows。归一化文件路径段的反斜杠，使它与正斜杠产生相同的点分路径，保留参数文本原样。",
        "anchor": 'names[0] = names[0].replace(nodes.SEP, ".")',
        "old": 'names[0] = names[0].replace(nodes.SEP, ".")',
        "new": 'names[0] = names[0].replace(nodes.SEP, ".").replace(chr(92), ".")',
    },
    {
        "id": "log-cache-directory-number",
        "title": "临时目录编号排除带下划线的后缀",
        "evidence_file": "_pytest/pathlib.py",
        "early": "查看编号目录枚举与解析逻辑，确认无效后缀由既有的 -1 标记排除。",
        "final": "只把 ASCII 十进制数字组成的后缀视为有效目录编号，修复例如 run-1_0 被当成编号 10 的问题。",
        "anchor": "return int(maybe_num)",
        "old": "    try:\n        return int(maybe_num)\n    except ValueError:\n        return -1",
        "new": "    if not maybe_num.isascii() or not maybe_num.isdecimal():\n        return -1\n    try:\n        return int(maybe_num)\n    except ValueError:\n        return -1",
    },
    {
        "id": "log-terminal-empty-no-color",
        "title": "终端颜色兼容空值 NO_COLOR",
        "evidence_file": "_pytest/_io/terminalwriter.py",
        "early": "先排查测试报告的颜色优先级。明确指定 PY_COLORS 的行为需要继续保留。",
        "final": "修复 NO_COLOR 已设置为空字符串时仍会被 FORCE_COLOR 开启颜色的行为，保留显式 PY_COLORS 对颜色的既有优先级。",
        "anchor": 'if os.environ.get("NO_COLOR"):',
        "old": 'if os.environ.get("NO_COLOR"):',
        "new": 'if "NO_COLOR" in os.environ:',
    },
)

# 四题共用经排查链路排序的 pytest 模块。运行时实际读取这些文件的非重叠页。
RELATED = (
    "_pytest/nodes.py", "_pytest/reports.py", "_pytest/runner.py", "_pytest/terminal.py",
    "_pytest/python.py", "_pytest/main.py", "_pytest/config/__init__.py", "_pytest/junitxml.py",
    "_pytest/pathlib.py", "_pytest/cacheprovider.py", "_pytest/_io/terminalwriter.py",
    "_pytest/capture.py", "_pytest/fixtures.py", "_pytest/_code/code.py",
)
CASES = [
    {
        "id": task["id"], "title": task["title"], "category": "log", "source": SOURCE,
        "workspace": PACKAGE_ROOT, "evidence_file": task["evidence_file"], "anchor": task["anchor"],
        "early": task["early"], "final": task["final"], "history_files": RELATED,
    }
    for task in TASKS
]


@pytest.fixture(scope="session")
def subject() -> tuple[str, types.ModuleType]:
    """从环境读取题号并加载目标源码，返回题号与隔离模块。

    AH_REPLAY_LOG_CANDIDATE 未设置时使用已安装的原始包，允许 reference 预检替换。设置为候选根绝对路径时，
    直接执行其中对应的 _pytest 源码，不应用参考修改；缺少目标文件或路径越界时由评测失败报告呈现。
    """
    identifier = os.environ["AH_REPLAY_LOG_CASE"]
    task = next(item for item in TASKS if item["id"] == identifier)
    candidate = os.environ.get("AH_REPLAY_LOG_CANDIDATE")
    variant = os.environ.get("AH_REPLAY_LOG_VARIANT")
    path = PACKAGE_ROOT / task["evidence_file"]
    if candidate is not None:
        if variant == "reference":
            raise ValueError("候选评分不能同时启用参考修改")
        root = Path(candidate)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("候选根必须是已存在的绝对目录")
        root = root.resolve(strict=True)
        path = (root / task["evidence_file"]).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"待评分源码超出根目录或不是普通文件：{identifier}")
    name = task["evidence_file"].removesuffix(".py").replace("/", ".")
    original = importlib.import_module(name)
    source = path.read_text(encoding="utf-8")
    if variant == "reference":
        if source.count(task["old"]) != 1:
            raise ValueError(f"参考修改定位不唯一：{identifier}")
        source = source.replace(task["old"], task["new"], 1)
    # 原名用于源码内部符号解析。执行环境隔离，既有发行包文件保持可供检查点读取。
    namespace = types.ModuleType(original.__name__)
    namespace.__dict__.update(__file__=str(path), __package__=original.__package__)
    exec(compile(source, str(path), "exec"), namespace.__dict__)
    return identifier, namespace


@pytest.mark.parametrize("number", range(24))
def test_existing_contract(subject: tuple[str, types.ModuleType], number: int) -> None:
    """让每个编号对应一个既有行为样本，生成真实的逐项测试日志。"""
    identifier, module = subject
    if identifier == "log-xml-c1-control":
        assert module.bin_xml_escape(f"case-{number}\x07") == f"case-{number}#x07"
    elif identifier == "log-junit-win-path":
        address = f"tests/unit_{number}.py::test_x[dir/a::{number}]"
        assert module.mangle_test_address(address) == [f"tests.unit_{number}", f"test_x[dir/a::{number}]"]
    elif identifier == "log-cache-directory-number":
        assert module.parse_num(f"{number:03d}") == number
    else:
        with patch.dict(os.environ, {"PY_COLORS": str(number % 2), "NO_COLOR": "1"}, clear=True):
            assert module.should_do_markup(io.StringIO()) == bool(number % 2)


def test_new_contract(subject: tuple[str, types.ModuleType]) -> None:
    """断言题面新增规则。基线必须在对应断言失败，参考修改必须通过。"""
    identifier, module = subject
    if identifier == "log-xml-c1-control":
        assert module.bin_xml_escape("Case 🧪\x85\x07") == "Case 🧪#x85#x07"
    elif identifier == "log-junit-win-path":
        assert module.mangle_test_address(r"tests\unit.py::test_a[foo\bar]") == ["tests.unit", r"test_a[foo\bar]"]
    elif identifier == "log-cache-directory-number":
        assert module.parse_num("1_0") == -1
        assert module.parse_num("-2") == -1
    else:
        with patch.dict(os.environ, {"NO_COLOR": "", "FORCE_COLOR": "1"}, clear=True):
            assert module.should_do_markup(io.StringIO()) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-vv", "--color=no", "-p", "no:cacheprovider"]))
