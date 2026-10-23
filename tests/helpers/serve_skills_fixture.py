"""技能加载离线浏览器服务；真实执行 Git、文件工具和宿主 pytest，不访问供应商。"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import uvicorn

from agent_harness.projects.models import ProjectPreset
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig


_OWNER = "agent-harness-skills-browser-20261023"  # 专属目录的归属标记，不复用未知目录。
_FIXED_SOURCE = (
    'def add(left, right):\n'
    '    """返回两数之和；宿主已有测试验证实际结果。"""\n'
    '    return left + right\n'
)  # 固定模型只验证技能与执行链路，不评价真实模型修复能力。


class SkillsBrowserModel:
    """按本轮实际技能前缀生成交付文件，完整记录离线模型输入。"""

    def __init__(self, root: Path) -> None:
        """绑定专属 root 下的请求证据文件；构造不写文件或读取环境凭据。"""
        self.requests_path = root / "requests.jsonl"  # 逐次追加完整 messages/tools，不含任何密钥。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """记录 messages/tools，按当前固定前缀与本轮工具数返回响应；不发起 HTTP。"""
        await asyncio.sleep(0.25)
        # 当前固定前缀紧随 system，不能从历史工具结果或旧任务猜测当前技能。
        prefix = messages[1].get("content", "") if len(messages) > 1 and messages[1].get("role") == "user" else ""
        selected = prefix if isinstance(prefix, str) and "<selected_skill>" in prefix else ""
        markers = list(dict.fromkeys(re.findall(r"STYLE_ALPHA_V[0-9]+|STYLE_BETA", selected)))
        marker = ", ".join(markers) if markers else "NO_SKILL"
        task_index = max(index for index, message in enumerate(messages) if message.get("role") == "user")
        used = sum(message.get("role") == "tool" for message in messages[task_index + 1:])
        evidence = {"messages": messages, "tools": tools, "selected_marker": marker, "current_tool_count": used}
        with self.requests_path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(evidence, ensure_ascii=False, allow_nan=False) + "\n")
        calls: list[ToolCall] = []
        content = None
        if used == 0:
            calls = [ToolCall("skill-fix-add", "write_file", json.dumps({
                "path": "src/calc.py", "content": _FIXED_SOURCE, "overwrite": True,
            }, ensure_ascii=False))]
        elif used == 1:
            calls = [ToolCall("skill-delivery", "write_file", json.dumps({
                "path": "delivery.txt", "content": f"本轮技能：{marker}。\n已修正加法；实际测试由宿主运行。\n",
                "overwrite": True,
            }, ensure_ascii=False))]
        else:
            content = f"技能验收完成：{marker}。请查看本轮宿主测试、交付文件和代码差异。"
        message = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = [
                {"id": call.call_id, "type": "function",
                 "function": {"name": call.name, "arguments": call.arguments_json}}
                for call in calls
            ]
        return ModelResponse(
            message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop",
            response_model="offline-skills-browser",
            usage={"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14},
        )


def owned_root(path: Path) -> None:
    """创建或复用带正确 marker 的专属 path；拒绝未知目录和链接，不清理既有证据。"""
    marker = path / ".fixture-owned"
    if path.is_symlink() or path.resolve() != path:
        raise RuntimeError("技能验收目录必须是普通目录。")
    if path.exists():
        if not path.is_dir() or marker.is_symlink() or not marker.is_file() or marker.read_text(encoding="utf-8") != _OWNER:
            raise RuntimeError("技能验收目录已存在且没有正确归属标记。")
        return
    path.mkdir(parents=True)
    marker.write_text(_OWNER, encoding="utf-8")


def initialize_repository(path: Path) -> None:
    """首次创建真实 Git 项目与已有测试；归属正确的已有项目重启时原样保留。"""
    marker = path / ".fixture-owned"
    if path.exists():
        if path.is_symlink() or not path.is_dir() or not marker.is_file() or marker.read_text(encoding="utf-8") != _OWNER:
            raise RuntimeError("技能测试项目已存在且不属于本辅助服务。")
        return
    path.mkdir()
    marker.write_text(_OWNER, encoding="utf-8")
    (path / "src").mkdir()
    (path / "tests").mkdir()
    (path / "src/calc.py").write_bytes(
        'def add(left, right):\n    """待修复的加法实现；应由已有测试发现错误。"""\n    return left - right\n'.encode("utf-8")
    )
    (path / "tests/test_calc.py").write_bytes(
        'from calc import add\n\ndef test_add():\n    """已有测试要求正数和负数都满足加法。"""\n'
        '    assert add(2, 3) == 5\n    assert add(-2, 3) == 1\n'.encode("utf-8")
    )
    (path / "AGENTS.md").write_text("新增或修改的函数使用中文说明；保留已有 tests/test_calc.py。\n", encoding="utf-8")
    (path / "README.md").write_text("技能加载浏览器验收专用项目。\n", encoding="utf-8")
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.fixture-owned\n", encoding="utf-8")
    commands = (
        ["git", "init", "-q"],
        ["git", "config", "core.autocrlf", "false"],
        ["git", "add", "."],
        ["git", "-c", "user.name=Harness Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-qm", "Create skills browser fixture"],
    )
    for argv in commands:
        subprocess.run(argv, cwd=path, check=True, capture_output=True, timeout=15)
    # 源目录保留用户未提交文件，代码副本不得覆盖或清除它。
    (path / "local.txt").write_text("用户源仓库的未提交文件保持原样。\n", encoding="utf-8")


def initialize_skills(root: Path) -> None:
    """首次建立 alpha/beta/broken；已有技能保留，便于验证源文件修改后逐轮重新加载。"""
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise RuntimeError("技能集合必须是专属普通目录。")
    root.mkdir(exist_ok=True)
    documents = {
        "alpha": "---\nname: alpha\ndescription: 交付说明使用 Alpha 规范\n---\n交付文件必须包含 STYLE_ALPHA_V1。\n",
        "beta": "---\nname: beta\ndescription: 交付说明使用 Beta 规范\n---\n交付文件必须包含 STYLE_BETA。\n",
        "broken": "---\nname: broken\n---\n缺少 description，用于验证页面展示禁用原因。\n",
    }
    for name, content in documents.items():
        directory = root / name
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise RuntimeError("技能子目录不能使用链接或特殊文件。")
        directory.mkdir(exist_ok=True)
        path = directory / "SKILL.md"
        if path.is_symlink():
            raise RuntimeError("技能正文不能使用链接。")
        if not path.exists():
            path.write_bytes(content.encode("utf-8"))


def main() -> None:
    """读取 --port（默认 18779）启动隔离服务；资源与占位密钥仅属于辅助服务。"""
    parser = argparse.ArgumentParser(description="技能加载的真实 Git 与浏览器离线验收服务")
    parser.add_argument("--port", type=int, default=18779)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    root = project / ".runtime" / "skills-browser"
    owned_root(root)
    source = root / "source"
    initialize_repository(source)
    skills = root / "skills"
    initialize_skills(skills)
    # 只在本辅助进程设置占位值以通过预检；离线模型不读取真实凭据或访问供应商。
    os.environ["DEEPSEEK_API_KEY"] = "offline-skills-browser-key"
    config = WebConfig(
        project_dir=project, runtime_root=root / "worktrees", frontend_dist=project / "frontend/dist",
        workspaces=[], projects=[ProjectPreset(
            id="skills-project", label="技能代码项目（离线验收）", path=source,
            test_argv=[sys.executable, "-m", "pytest", "-q"], test_python_paths=["src"],
        )],
    )
    app = create_app(
        config, RunConfig(requested_model="offline-skills-browser", tools=["read_file", "write_file"],
                          skill_roots=[skills]),
        root / "runs", model_client=SkillsBrowserModel(root),
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, loop="asyncio", workers=1, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
