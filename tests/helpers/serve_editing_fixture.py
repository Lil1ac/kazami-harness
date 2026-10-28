"""局部编辑的离线模型和浏览器服务；文件、工具、命令及项目测试均真实执行。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import uvicorn

from agent_harness.projects import ProjectPreset
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig


OWNER = "harness-edit-file-20261026"  # 只允许复用本轮明确拥有的夹具目录。
SOURCE = "# 保留原有说明：中文🙂\r\ndef add(a, b):\r\n    \"\"\"返回两数之和。\"\"\"\r\n    return a - b\r\n\r\ndef untouched():\r\n    \"\"\"保留此函数和原换行。\"\"\"\r\n    return '保持不变'\r\n"
# 测试由固定模型通过 write_file 创建；用户仓库和宿主验收入口不被模型更换。
TEST_SOURCE = "from calculator import add\n\ndef test_add():\n    \"\"\"覆盖正负数，执行真实待修改代码。\"\"\"\n    assert add(2, 1) == 3\n    assert add(-2, 1) == -1\n"


class EditingModel:
    """按最新用户要求使用真实读取、局部编辑、测试文件和命令，保持供应商完全离线。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """根据本轮工具观察返回下一项调用或最终回答；输入来自真实 Agent 循环，不直接修改文件。"""
        latest = max(index for index, message in enumerate(messages) if message["role"] == "user")
        observations = [json.loads(message["content"]) for message in messages[latest + 1:] if message["role"] == "tool"]
        assert all(result["ok"] for result in observations), observations
        assert "edit_file" in {item["function"]["name"] for item in tools}
        following = "空值" in messages[latest]["content"] or "None" in messages[latest]["content"]
        stage = len(observations)
        if stage == 0:
            name, arguments = "read_file", {"path": "calculator.py"}
        elif stage == 1:
            data = observations[0]["data"]
            newline = "\r\n" if "\r\n" in data["content"] else "\n"
            old = "return a + b" if following else "return a - b"
            new = ("if a is None or b is None:" + newline + "        raise ValueError('参数不能为空')"
                   + newline + "    return a + b") if following else "return a + b"
            name, arguments = "edit_file", {"path": "calculator.py", "old_text": old, "new_text": new, "expected_sha256": data["sha256"]}
        elif stage == 2:
            if following:
                name, arguments = "read_file", {"path": "test_calculator.py"}
            else:
                name, arguments = "write_file", {"path": "test_calculator.py", "content": TEST_SOURCE}
        elif following and stage == 3:
            data = observations[2]["data"]
            old = "    assert add(-2, 1) == -1\n"
            new = old + "\ndef test_none():\n    \"\"\"空值必须报错，保留既有加法断言。\"\"\"\n    import pytest\n    with pytest.raises(ValueError):\n        add(None, 1)\n"
            name, arguments = "edit_file", {"path": "test_calculator.py", "old_text": old, "new_text": new, "expected_sha256": data["sha256"]}
        elif stage == (4 if following else 3):
            name, arguments = "run_command", {"argv": [sys.executable, "-m", "pytest", "-q"]}
        else:
            return ModelResponse(message={"role": "assistant", "content": "已局部修改加法函数并运行测试，其他内容保持。"}, tool_calls=[], finish_reason="stop", response_model="offline-editing")
        call = ToolCall(uuid4().hex, name, json.dumps(arguments, ensure_ascii=False))
        return ModelResponse(message={"role": "assistant", "content": None, "tool_calls": [{"id": call.call_id, "type": "function", "function": {"name": name, "arguments": call.arguments_json}}]}, tool_calls=[call], finish_reason="tool_calls", response_model="offline-editing")


def fixture_app(root: Path, frontend_dist: Path):
    """在有归属标记的 root 创建/复用真实 Git 小项目，返回生产 API 应用；frontend_dist 只读。"""
    marker = root / ".fixture-owned"
    if root.exists() and (not marker.is_file() or marker.read_text(encoding="utf-8") != OWNER):
        raise RuntimeError("目标目录不属于本轮局部编辑夹具。")
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(OWNER, encoding="utf-8")
    source = root / "局部编辑项目"
    if not source.exists():
        source.mkdir()
        (source / "calculator.py").write_bytes(SOURCE.encode("utf-8"))
        (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
        for argv in (["init", "-q"], ["config", "core.autocrlf", "false"], ["add", "."],
                     ["-c", "user.name=Fixture", "-c", "user.email=fixture@invalid", "commit", "-qm", "baseline"]):
            subprocess.run(["git", *argv], cwd=source, capture_output=True, check=True, timeout=15)
        (source / "user-note.txt").write_bytes("用户未提交内容保持。\n".encode("utf-8"))
    config = WebConfig(project_dir=root, runtime_root=root / "copies", frontend_dist=frontend_dist,
                       projects=[ProjectPreset(id="editing-project", label="局部编辑项目", path=source,
                                               test_argv=[sys.executable, "-m", "pytest", "-q"], test_timeout_seconds=30.0)])
    # 宿主默认未启用局部编辑，浏览器验收必须从配置页明确启用。
    app = create_app(config, RunConfig(tools=["read_file", "search_files", "write_file", "run_command"]),
                     root / "runs", model_client=EditingModel())
    app.state.fixture_project_id = app.state.manager.catalog.register(source)
    return app


def main() -> None:
    """启动 --port 指定服务（默认18786）；仅使用专属离线密钥与持久化夹具目录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18786)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    os.environ["DEEPSEEK_API_KEY"] = "offline-editing-fixture"
    app = fixture_app(project / ".runtime/edit-file-browser-20261026", project / "frontend/dist")
    uvicorn.run(app, host="127.0.0.1", port=args.port, loop="asyncio", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
