"""选取六个公开来源的固定源码，按真实文件工具回执构造对照检查点。"""

from __future__ import annotations

import ast
from collections import Counter
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import shutil
import sys
import sysconfig

from agent_harness.evaluation.campaign_budget import atomic_json


HERE = Path(__file__).resolve().parent  # 本专项的脚本所在目录。
ROOT = HERE.parents[1]  # AgentHarness 项目根目录。
OUTPUT = ROOT / "output" / HERE.name  # 本专项的产物根目录。
ARCHIVES = ROOT / "evals/memory-repository-20261117/sources"  # 已有的公开仓库固定副本。
DISTRIBUTIONS = {
    "pytest": ("pytest", ("pytest", "_pytest")),
    "click": ("click", ("click",)),
    "rich": ("rich", ("rich",)),
}  # 项目编号对应发行包名称与源码目录。
DEPENDENCIES = ("pluggy", "packaging", "iniconfig", "pygments", "markupsafe",
                "markdown_it", "mdurl", "attrs", "attr", "typing_extensions.py")
# 离线评分的独立依赖目录，只收源码。
STDLIB = (
    "argparse", "calendar", "configparser", "csv", "dataclasses", "difflib", "fnmatch",
    "fractions", "functools", "glob", "gzip", "keyword", "pathlib", "pprint", "re",
    "reprlib", "shlex", "statistics", "string", "textwrap", "token", "tokenize", "uuid",
)  # CPython 安装源码中与六题的格式化、匹配和记录行为相关的模块。
EXTENSIONS = {".py", ".pyi", ".md", ".rst", ".txt", ".toml", ".json", ".in", ".typed"}
# 允许工具读取的发行源码、声明和随包文档类型。
LAYOUT = (("low", 165, 6900), ("low", 184, 8400), ("near", 227, 7200),
          ("near", 241, 8700), ("over", 271, 7500), ("over", 288, 9100))
# 开始回放前固定各仓的压力档、目标 KiB 和工具页的字节上限。


def shipping_files(source: Path) -> list[Path]:
    """枚举 source 下正常公开文本文件，排除字节码和 Git 等本地运行目录。"""
    return [path for path in sorted(source.rglob("*"))
            if path.is_file() and not path.is_symlink()
            and not any(part in {"__pycache__", ".git", ".venv", ".pytest_cache"}
                        for part in path.relative_to(source).parts)
            and (path.suffix in EXTENSIONS or path.name.lower().startswith(("license", "copying", "notice")))]


def copy_text_tree(source: Path, target: Path) -> None:
    """把 source 的原始 UTF-8 文本逐字节复制到 target，二进制辅助文件不进入评测。"""
    for file in shipping_files(source):
        content = file.read_bytes()
        try:
            content.decode("utf-8")
        except UnicodeError:
            continue
        destination = target / file.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def stage_sources(batch: Path) -> dict[str, dict]:
    """准备 batch 的按仓源码和离线评分依赖，返回版本、来源、目录与文件指纹。"""
    from continue_batch import inventory

    bases = batch / "bases"
    deps = batch / "scorer-deps"
    deps.mkdir()
    packages = Path(sysconfig.get_path("purelib"))
    for name in DEPENDENCIES:
        source = packages / name
        if source.is_dir():
            copy_text_tree(source, deps / name)
        else:
            shutil.copyfile(source, deps / name)
    sources = {}
    for project, (distribution, packages_to_copy) in DISTRIBUTIONS.items():
        target = bases / project
        for name in packages_to_copy:
            copy_text_tree(packages / name, target / name)
        dist = metadata.distribution(distribution)
        for file in dist.files or []:
            relative = file.as_posix()
            if ".dist-info/" in relative and not relative.endswith(("RECORD", "direct_url.json", "INSTALLER")):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    destination = target / relative.replace("METADATA", "METADATA.txt")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
        sources[project] = {"basis": "installed public distribution", "version": dist.version,
                            "project_urls": dist.metadata.get_all("Project-URL") or [],
                            "files": inventory(target)}
    for project, cached in (("jinja", "reporting"), ("marshmallow", "importer")):
        target = bases / project
        copy_text_tree(ARCHIVES / cached, target)
        distribution = "Jinja2" if project == "jinja" else "marshmallow"
        version_module = "jinja2" if project == "jinja" else "marshmallow"
        if not (target / "src" / version_module / "__init__.py").is_file():
            raise FileNotFoundError(f"固定仓库快照缺少 Python 包：{project}")
        versions = {"jinja": "3.1.6", "marshmallow": "4.3.1"}
        sources[project] = {"basis": "verified public source archive", "version": versions[project],
                            "package": distribution, "files": inventory(target)}
    target = bases / "cpython"
    stdlib = Path(sysconfig.get_path("stdlib"))
    (target / "Lib").mkdir(parents=True)
    for name in STDLIB:
        source = stdlib / f"{name}.py"
        if source.is_file():
            shutil.copyfile(source, target / "Lib" / source.name)
        elif (stdlib / name).is_dir():
            copy_text_tree(stdlib / name, target / "Lib" / name)
    license_file = stdlib.parent / "LICENSE.txt"
    if license_file.is_file():
        shutil.copyfile(license_file, target / "LICENSE.txt")
    sources["cpython"] = {"basis": "installed CPython standard-library source",
                          "version": sys.version.split()[0], "files": inventory(target)}
    atomic_json(batch / "sources.json", {"repositories": sources, "scorer_dependencies": inventory(deps)})
    return sources


def module_path(case: dict) -> str:
    """把 case 的规范模块名映射到候选仓库中的源码相对路径。"""
    module = case["module"].replace(".", "/") + ".py"
    if case["project"] == "cpython":
        return "Lib/" + module
    if case["project"] in {"jinja", "marshmallow"}:
        return "src/" + module
    return module


def find_anchor(content: bytes, name: str) -> tuple[str, int]:
    """从 content 找到 name 对应定义的唯一原文和字节位置，未登记符号时拒绝冻结。"""
    source = content.decode("utf-8")
    children = ast.parse(source).body
    node = None
    for part in name.split("."):
        node = next((item for item in children if getattr(item, "name", None) == part), None)
        if node is None:
            raise ValueError(f"缺少指定观察出处：{name}")
        children = getattr(node, "body", [])
    lines = source.splitlines(keepends=True)
    for width in range(1, 8):
        needle = "".join(lines[node.lineno - 1:node.lineno - 1 + width]).rstrip()
        encoded = needle.encode("utf-8")
        if content.count(encoded) == 1:
            return needle, content.index(encoded)
    raise ValueError(f"源码观察位置重复：{name}")


def relevant_files(workspace: Path, path: str, symbol: str, cited: tuple[str, ...]) -> list[str]:
    """将预登记 cited、同模块文档、相邻源码和测试按关联词排序，避免重复读页。"""
    names = set(re.findall(r"[a-z]{4,}", (Path(path).stem + "_" + symbol).lower()))
    scope = Path(path).parent
    options = []
    for item in shipping_files(workspace):
        relative = item.relative_to(workspace)
        if relative.as_posix() == path:
            continue
        if ".dist-info" in relative.as_posix() or item.stat().st_size > 1024 * 1024:
            continue
        match = sum(name in item.stem.lower() for name in names)
        siblings = int(relative.parent == scope)
        prefix = int(relative.parts[0] == Path(path).parts[0])
        options.append((-match, -siblings, -prefix, relative.as_posix()))
    return list(dict.fromkeys((*cited, *(item[-1] for item in sorted(options)))))


def freeze_case(case: dict, workspace: Path, position: int, tools: list[dict], log: dict) -> dict:
    """用历史成功读取和实际评分进程状态生成一条按 position 分档的冻结输入。"""
    from freeze import POLICY, SYSTEM, append_tool, measure, page, pressure_band
    from continue_batch import digest
    from replay import _input_content

    band, target_kib, page_size = LAYOUT[position]
    path = module_path(case)
    content = (workspace / path).read_bytes()
    needle, at = find_anchor(content, case["symbol"])
    offset = max(0, min(at - page_size // 3, len(content) - page_size))
    while offset and content[offset] & 0xC0 == 0x80:
        offset -= 1
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"先只读检查 {case['module']} 中的 {case['symbol']}。"
                 + case["setup"] + " 本轮先保留证据，我稍后给出修改要求。"}]
    request, result = page(workspace, path, offset, page_size)
    index = append_tool(messages, "read_file", request, result)
    if needle not in result["data"]["content"]:
        raise ValueError(f"证据未落在实际读取页：{case['id']}")
    matches = {path: result["data"]["sha256"]}
    append_tool(messages, "run_command", {"argv": ["python", "-I", "-B", "/grader/check_case.py", case["id"]]},
                {"ok": False, "data": {"exit_code": log["exit_code"], "stdout": log["stdout"], "stderr": ""},
                 "error": None, "truncated": False})
    messages.append({"role": "user", "content": "沿相关模块继续查看接口、调用方、相邻实现和覆盖记录，等我确认修改范围。"})
    final = {"role": "user", "content": case["prompt"]}
    target = target_kib * 1024
    current = measure([*messages, final], tools)
    for other in relevant_files(workspace, path, case["symbol"], tuple(case.get("related", ()))):
        if current >= target:
            break
        cursor = 0
        while cursor is not None and current < target:
            request, result = page(workspace, other, cursor, page_size)
            data = result["data"]
            if data["bytes_read"] <= 0:
                break
            matches[other] = data["sha256"]
            append_tool(messages, "read_file", request, result)
            cursor = data["next_offset"]
            current = measure([*messages, final], tools)
    if pressure_band(current) != band or not target <= current < target + page_size * 2:
        raise ValueError(f"公开源码不能满足已冻结的输入压力档：{case['id']} {current}/{target}, {band}")
    messages.append(final)
    return {
        "id": case["id"], "title": case["title"], "source": case["project"], "category": case["project"],
        "band": band, "workspace": str(workspace), "source_files": matches, "input_bytes": current,
        "messages": messages, "tools": tools, "checkpoint_sha256": digest(_input_content(messages, tools)),
        "expected_evidence": [{"message_index": index, "contains": needle, "path": path}],
        "limit_bytes": POLICY.max_input_bytes,
    }


def matrix(cases: list[dict]) -> dict[str, list[dict]]:
    """按项目顺序分组 cases，验证全量恰好覆盖六仓三十六题和唯一题号。"""
    expected = set(DISTRIBUTIONS) | {"cpython", "jinja", "marshmallow"}
    counts = Counter(case["project"] for case in cases)
    if counts != dict.fromkeys(expected, 6) or len({case["id"] for case in cases}) != 36:
        raise ValueError(f"当前题单不满足 6 仓 × 6 道：{counts}")
    return {project: [case for case in cases if case["project"] == project] for project in sorted(expected)}

