"""将独立的功能变更构建为可审计的仓库学习、答题与评分夹具。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent, indent

from sources import Source


@dataclass(frozen=True)
class Task:
    """一条维护需求、正常行为探针及可选的过期行为探针。"""

    key: str  # 在仓库内唯一的英文题号。
    category: str  # locate、knowledge 或 safety。
    module: str  # 候选包内不带前缀的 Python 模块名称。
    title: str  # 维护手册小节和需求标题。
    question: str  # 仅提供用户观察、目标入口与保持兼容的约束。
    contract: str  # 现行维护说明，学习可见性由 category 决定。
    edits: tuple[tuple[str, str], ...]  # 参考解的唯一原文片段及替换内容。
    checks: str  # 评分脚本使用 m 指代实际候选模块。
    old: tuple[str, str, str] | None = None  # 失效类型、过期说明、以 OLD 赋布尔值的独立探针。


def _doc_path(task: Task) -> str:
    """返回 task 的现行维护说明，可由目录与主题寻获。"""
    return f"docs/maintainers/{task.category}/{task.key}.md"


def _patch(source: str, task: Task) -> str:
    """对冻结源码 source 逐项应用 task 的唯一参考修改，返回完整候选。"""
    for before, after in task.edits:
        if source.count(before) != 1:
            raise ValueError(f"参考补丁标记必须唯一 {task.key}: {before!r}")
        source = source.replace(before, after, 1)
    return source


def _bootstrap(source: Source, task: Task) -> str:
    """返回隔离评分的加载逻辑，确保 m 对应 source 的 task 模块而非镜像预装版本。"""
    return (
        "import importlib, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, str(Path({source.import_root!r}).resolve()))\n"
        f"m = importlib.import_module({(source.package + '.' + task.module)!r})\n"
        f"assert Path(m.__file__).resolve() == Path({source.module_path(task.module)!r}).resolve()\n"
    )


def _reference(title: str, target: str, evidence: str) -> dict:
    """返回只提供 title 文档入口的已确认笔记，evidence 为实际读过的索引。"""
    return {"type": "reference", "title": title, "details": {"target": target, "purpose": title},
            "evidence": [evidence]}


def _fact(title: str, data: str, evidence: str) -> dict:
    """返回带实际来源 evidence 的 title 资料事实，data 是确认过的正文。"""
    return {"type": "fact", "title": title, "details": {"subject": title, "attribute": "maintainer contract", "value": data},
            "evidence": [evidence], "origin": "tool_observation"}


def build_case(source: Source, task: Task, neighbors: tuple[Task, ...], originals: dict[str, str]) -> dict:
    """组合原生 originals、本题 task 和同仓指引 neighbors，返回可直接交给既有 Runner 的冻结题。"""
    path = _doc_path(task)
    target = source.module_path(task.module)
    catalog = f"docs/maintainers/{task.category}/index.md"
    entry = "docs/maintainers/index.md"
    marker = task.contract.split(". ", 1)[0] + "."
    current = f"# {task.title} contract\n\n{task.contract}\n"
    additions = {
        entry: ("# Release maintenance navigation\n\n"
                "Use locate for the operator workflow and file entry points, knowledge for already settled maintenance "
                "rules, and safety for versioned or withdrawn material. Public release documentation is unchanged.\n\n"
                + "".join(f"- {category} notes: docs/maintainers/{category}/index.md\n"
                          for category in ("locate", "knowledge", "safety"))),
        path: current,
    }
    for category in ("locate", "knowledge", "safety"):
        links = [f"- {other.title}: {_doc_path(other)}" for other in neighbors if other.category == category]
        additions[f"docs/maintainers/{category}/index.md"] = "# Maintainer index\n\n" + "\n".join(links) + "\n"
    for other in neighbors:
        if other is task:
            continue
        # 邻近真实任务的正文也可被定位，但始终只允许本题目标的一项修复。
        additions[_doc_path(other)] = f"# {other.title} contract\n\n{other.contract}\n"
    files = originals | additions
    critic = _bootstrap(source, task)
    row = {
        "id": f"memory-expanded-{source.slug}-{task.key}",
        "title": task.title,
        "project": source.slug,
        "scenario": task.category,
        "kind": "stale" if task.category == "safety" else "reuse",
        "target": target,
        "evidence_paths": [path],
        "evidence_anchors": {path: marker},
        "files": files,
        "reference": _patch(originals[target], task),
        "score": critic + dedent(task.checks).strip() + "\n",
        "prompt": (
            f"{task.title} reported for {source.project}. {task.question} "
            f"Change only {target}. Follow the current maintenance contract in this repository, "
            "preserve compatible behavior, and report what was changed."
        ),
    }
    if task.category == "locate":
        row["episodes"] = [{"prompt": f"Find the maintenance entry for {task.title}. Read the navigation and its area index.",
                            "read": [entry, catalog]}]
        row["notes"] = [{"episode": 0, "status": "active", "content": _reference(task.title, path, catalog)}]
    elif task.category == "knowledge":
        row["episodes"] = [{"prompt": f"Confirm the exact maintenance contract for {task.title}.", "read": [path]}]
        row["notes"] = [{"episode": 0, "status": "active",
                         "content": _fact(task.title, task.contract, path)}]
    else:
        if task.category != "safety" or task.old is None:
            raise ValueError(f"无有效的题型或失效契约 {task.key}")
        behavior, older_contract, probe = task.old
        retired = f"HISTORIC-{source.slug.upper()}-{task.key.upper()}-R1"
        old_text = f"# {task.title} prior handbook\n\n{retired}. {older_contract}\n"
        if behavior == "foreign":
            row["foreign_files"] = {path: old_text}
        else:
            files[path] = old_text
            row["updates"] = {path: current}
        row["episodes"] = [{"prompt": f"Record the available {task.title} rule before the next change.", "read": [path]}]
        row["notes"] = [{"episode": 0, "status": {"changed": "active", "foreign": "active"}.get(behavior, behavior),
                         "foreign": behavior == "foreign", "content": _fact(task.title, old_text.strip(), path)}]
        row["stale_values"] = [retired]
        row["misuse"] = (critic + "try:\n" + indent(dedent(probe).strip(), "    ") +
                         "\nexcept Exception:\n    OLD = False\nraise SystemExit(0 if OLD else 1)\n")
    return row


def validate_case(case: dict) -> None:
    """验证冻结 case 的候选都存在、学习没有读候选及所有正文锚点可从答题材料找到。"""
    files = case["files"] | case.get("updates", {})
    assert case["target"] in files and files[case["target"]] != case["reference"]
    assert all(anchor in files[path] for path, anchor in case["evidence_anchors"].items())
    for index, episode in enumerate(case["episodes"]):
        is_foreign = any(note["episode"] == index and note.get("foreign") for note in case["notes"])
        original = case["foreign_files"] if is_foreign else case["files"]
        assert all(path in original and path != case["target"] for path in episode["read"])
    assert all(not Path(path).is_absolute() and ".." not in Path(path).parts for path in files)
