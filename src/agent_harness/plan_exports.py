"""从可信计划生成 Markdown 派生文件；导出不改变方案、批准事实或工作区。"""

import hashlib
import html
import os
from pathlib import Path
import re
import tempfile

from .schemas import PlanRevision
from .store import StorageError


_PLAN_LABELS = {"pending": "待批准", "approved": "已批准", "rejected": "已拒绝", "superseded": "已被替代"}  # 原轮固定批准状态，不随最新轮变化。
_STEP_LABELS = {"pending": "待处理", "in_progress": "进行中", "completed": "已完成", "blocked": "受阻"}  # Agent 进度，不代表独立验收。


def _plain_markdown(value: str) -> str:
    """将 value 的纯文本字段转为 Markdown 字面内容，返回转义文本，保留换行并缩进续行。"""
    escaped = html.escape(value, quote=False)
    escaped = re.sub(r"([\\`*_{}\[\]()#+.!|])", r"\\\1", escaped)
    return escaped.replace("\n", "\n  ")


def render_plan_markdown(plan: PlanRevision) -> str:
    """返回 plan 完整正文及本轮状态的 UTF-8 Markdown 文本；不读写文件，不授予执行权限。"""
    lines = [f"# {_plain_markdown(plan.title)}", "", f"- 版本：v{plan.version}",
             f"- 批准状态：{_PLAN_LABELS[plan.status]}", f"- 执行目录：{_plain_markdown(plan.workspace)}", "",
             "> 此文件是计划记录的导出快照；执行授权以宿主保存的用户决定为准。", "",
             "## 目标与架构", "", plan.summary, "", "## 改动范围", ""]
    lines.extend(f"- {_plain_markdown(item)}" for item in plan.scope)
    lines.extend(["", "## 执行步骤", ""])
    for index, step in enumerate(plan.steps, start=1):
        checked = "x" if step.status == "completed" else " "
        lines.append(f"{index}. [{checked}] {_plain_markdown(step.text)} · {_STEP_LABELS[step.status]}")
    lines.extend(["", "> 步骤进度由 Agent 更新，实际验收结论见对应运行结果。", "", "## 验收方式", ""])
    lines.extend(f"- {_plain_markdown(item)}" for item in plan.verification)
    return "\n".join(lines) + "\n"


def materialize_plan_export(run_directory: Path, plan: PlanRevision) -> Path:
    """在 run_directory 的受控缓存生成 plan 的 Markdown，并返回不可变内容对应的文件路径。

    相同内容复用哈希目录；正文或进度变化产生新目录，不覆盖旧下载来源。写入使用原子替换，
    拒绝目录链接及缓存损坏。只写运行内派生缓存，失败抛 StorageError，不修改运行事实或项目。
    """
    return materialize_text_export(run_directory, render_plan_markdown(plan).encode("utf-8"), ".plan-exports", f"plan-v{plan.version}.md")


def materialize_text_export(directory: Path, content: bytes, cache_directory: str, filename: str) -> Path:
    """把 content 存到 directory 内的哈希缓存并返回路径；缓存名和文件名只能是单层宿主名称。

    缓存复用前核验完整字节，路径链接、名称越界和缓存篡改抛 StorageError；只写派生文件。
    """
    run_directory = directory
    temporary: Path | None = None
    try:
        if run_directory.is_symlink() or run_directory.is_junction():
            raise ValueError("linked run")
        root = run_directory.resolve(strict=True)
        if Path(cache_directory).name != cache_directory or Path(filename).name != filename or cache_directory in {".", ".."} or filename in {".", ".."}:
            raise ValueError("invalid export name")
        fingerprint = hashlib.sha256(content).hexdigest()
        parent = root
        for part in (cache_directory, fingerprint):
            parent = parent / part
            if parent.is_symlink() or parent.is_junction():
                raise ValueError("linked export directory")
            parent.mkdir(exist_ok=True)
            if not parent.resolve(strict=True).is_relative_to(root):
                raise ValueError("export outside run")
        target = parent / filename
        if target.is_symlink() or target.is_junction():
            raise ValueError("linked export file")
        if target.exists():
            if not target.is_file() or target.read_bytes() != content:
                raise ValueError("corrupt plan export")
            return target
        with tempfile.NamedTemporaryFile(dir=parent, prefix=".plan-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        return target
    except (OSError, ValueError, RuntimeError):
        raise StorageError("计划导出不可用，请检查运行记录目录。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # 派生缓存清理失败不能掩盖原始存储错误，事件事实始终不变。
