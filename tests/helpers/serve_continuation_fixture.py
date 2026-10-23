"""通过独立真实Git项目核对中断后继续；脚本模型不访问供应商。"""

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
from agent_harness.schemas import ContextPolicy, RunConfig, RunLimits
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig


# 用例只操作带专属归属标记的临时项目；原始仓库保持未提交说明。
MARKER = "harness-continuation-clean-browser-20261023"
FIXED_SOURCE = 'def add(left, right):\n    """返回两个参数之和。"""\n    return left + right\n'


class ContinuationBrowserModel:
    """在首次改码后等待显式释放文件，供浏览器验证服务中断与普通消息接续。"""

    def __init__(self, root: Path) -> None:
        """绑定专属root；其中release文件仅由验收操作创建，不由模型工具修改。"""
        self.root = root  # 保存模型等待状态的专属宿主目录。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """根据实际messages返回下一步；等待可取消，tools只用于判断是否开放回读。"""
        await asyncio.sleep(0.15)
        prompt = next(item["content"] for item in reversed(messages) if item["role"] == "user")
        results = [item for item in messages if item["role"] == "tool"]
        calls: list[ToolCall] = []
        content = None
        if not results:
            if "命令中断" in prompt:
                command = "import pathlib,time;pathlib.Path('command.ready').write_text('ready');time.sleep(60)"
                calls = [ToolCall("pending-command", "run_command", json.dumps({"argv": [sys.executable, "-c", command]}))]
            else:
                command = "from pathlib import Path;p=Path('execution-count.txt');p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
                calls = [
                    ToolCall("fix-add", "write_file", json.dumps({"path": "src/calc.py", "content": FIXED_SOURCE, "overwrite": True}, ensure_ascii=False)),
                    ToolCall("count-once", "run_command", json.dumps({"argv": [sys.executable, "-c", command]})),
                    ToolCall("read-evidence", "read_file", json.dumps({"path": "evidence.txt"})),
                ]
        elif not any(item.get("tool_call_id") == "finish-note" for item in results):
            if "命令中断" not in prompt:
                (self.root / "model-waiting").touch()
                while not (self.root / "release").exists():
                    await asyncio.sleep(0.1)
            reference = next((found.group(0) for item in results if (found := re.search(r"ctx:[a-f0-9-]{36}:[a-f0-9]{64}", item.get("content", "")))), None)
            enabled = any(item["function"]["name"] == "read_context" for item in tools)
            if reference and enabled and not any(item.get("tool_call_id") == "read-restored" for item in results):
                calls = [ToolCall("read-restored", "read_context", json.dumps({"reference": reference, "offset": 1900, "max_bytes": 512}))]
            else:
                calls = [ToolCall("finish-note", "write_file", json.dumps({"path": "CHANGELOG.md", "content": "从已核验接续继续；原命令只执行一次。\n"}, ensure_ascii=False))]
        else:
            content = "修复与说明已完成。请核对本次宿主测试、本轮预算及代码差异。"
        message = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}} for call in calls]
        return ModelResponse(message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop", response_model="offline-continuation-browser", usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


def initialize_repository(path: Path) -> None:
    """首次创建专属path及已有测试；已存在时只核验归属，绝不重置内容。"""
    marker = path / ".fixture-owned"
    if path.exists():
        if not marker.is_file() or marker.read_text(encoding="utf-8") != MARKER:
            raise RuntimeError("该目录不属于接续浏览器验收")
        return
    path.mkdir(parents=True)
    marker.write_text(MARKER, encoding="utf-8")
    (path / "src").mkdir()
    (path / "tests").mkdir()
    (path / "src/calc.py").write_text('def add(left, right):\n    """返回待修复的计算结果。"""\n    return left - right\n', encoding="utf-8")
    (path / "tests/test_calc.py").write_text('from pathlib import Path\nfrom calc import add\n\ndef test_add():\n    """宿主检查修改生效且已完成命令没有重复执行。"""\n    assert add(1, 2) == 3\n    assert Path("execution-count.txt").read_text() == "1"\n    assert Path("CHANGELOG.md").is_file()\n', encoding="utf-8")
    (path / "AGENTS.md").write_text("保留 CONTINUATION_RULE，不修改测试。\n", encoding="utf-8")
    (path / "evidence.txt").write_text("HEAD\n" + "x" * 2000 + "\nCONTINUATION_MIDDLE_EVIDENCE\n" + "y" * 2200 + "\nTAIL\n", encoding="utf-8")
    (path / "README.md").write_text("独立接续验收项目。\n", encoding="utf-8")
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.fixture-owned\n", encoding="utf-8")
    for argv in [["git", "init"], ["git", "config", "user.name", "Harness Fixture"], ["git", "config", "user.email", "fixture@example.invalid"], ["git", "add", "."], ["git", "commit", "-m", "Create continuation fixture"]]:
        subprocess.run(argv, cwd=path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
    with (path / "README.md").open("a", encoding="utf-8") as target:
        target.write("用户保留的未提交说明，不应被任务修改。\n")


def main() -> None:
    """读取--port（默认18777）启动单进程离线服务；重启复用原记录与测试项目。"""
    parser = argparse.ArgumentParser(description="中断后继续浏览器验收")
    parser.add_argument("--port", type=int, default=18777)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    root = project / ".runtime/continuation-browser-clean"
    marker = root / ".fixture-owned"
    if root.exists() and (not marker.is_file() or marker.read_text(encoding="utf-8") != MARKER):
        raise RuntimeError("同名目录没有正确归属标记")
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(MARKER, encoding="utf-8")
    source = root / "source"
    initialize_repository(source)
    os.environ["DEEPSEEK_API_KEY"] = "offline-continuation-browser-key"
    config = WebConfig(project_dir=project, runtime_root=root / "worktrees", frontend_dist=project / "frontend/dist", projects=[ProjectPreset(id="continuation", label="中断后继续联动（真实Git/离线模型）", path=source, test_argv=[sys.executable, "-m", "pytest", "-q"], test_python_paths=["src", "."])])
    host = RunConfig(requested_model="offline-continuation", tools=["read_file", "write_file", "run_command"], context=ContextPolicy(compression="masking", inline_tool_bytes=1024), limits=RunLimits(max_model_turns=16, run_timeout_seconds=300.0))
    app = create_app(config, host, root / "runs", model_client=ContinuationBrowserModel(root))
    uvicorn.run(app, host="127.0.0.1", port=args.port, loop="asyncio", workers=1, timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
