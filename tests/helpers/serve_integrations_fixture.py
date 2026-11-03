"""成果采纳真实页面验收服务；模型离线，HTTP、Git、队列、文件和测试均为真实实现。"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

import uvicorn

from agent_harness.permissions.models import PermissionPolicy
from agent_harness.projects import ProjectPreset
from agent_harness.projects.integration_files import git
from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig
from agent_harness.web.jobs import JobRequest


class IntegrationFixtureModel:
    """固定修改app.txt，所有写入通过真实文件工具执行，不访问供应商。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """首个messages工具结果前请求写文件，之后结束；tools只提供定义，返回模型协议对象。"""
        calls = [] if any(message.get("role") == "tool" for message in messages) else [ToolCall("write-app", "write_file", json.dumps({"path": "app.txt", "content": "ONE\ntwo\nthree\n", "overwrite": True}))]
        message = {"role": "assistant", "content": "工作树成果已完成，请审阅后采纳。"}
        if calls:
            message["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}} for call in calls]
        return ModelResponse(message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop", response_model="offline-integration")


def application(root: Path):
    """在专属root创建或重开夹具，返回真实应用；存在的目标和历史绝不重置。"""
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    if not (project / ".git").exists():
        (project / "app.txt").write_bytes(b"one\ntwo\nthree\n")
        (project / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
        git(project, "init", "-q")
        git(project, "add", ".")
        git(project, "-c", "user.name=HarnessFixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "baseline")
        (project / "app.txt").write_bytes(b"USER\ntwo\nthree\n")
    config = WebConfig(project_dir=root, worktrees_root=root / "trees", frontend_dist=Path(__file__).resolve().parents[2] / "frontend/dist", projects=[ProjectPreset(id="integration", label="成果采纳验收", path=project,
        test_argv=[sys.executable, "-c", "from pathlib import Path; assert Path('app.txt').read_text().startswith('ONE'); print('integration checks passed')"])])
    app = create_app(config, RunConfig(tools=["read_file", "write_file"], permission_policy=PermissionPolicy(verification="allow")), root / "runs", model_client=IntegrationFixtureModel())

    def key(config) -> str:
        """为离线config返回固定占位值；不会读取或发送真实模型密钥。"""
        return "offline-integration"

    app.state.manager._key_provider = key
    return app


async def seed(root: Path) -> None:
    """在空root生成一份固定工作树成果并保存编号；已生成时只读返回，不重放。"""
    if (root / "fixture.json").exists():
        return
    app = application(root)
    async with app.router.lifespan_context(app):
        job = await app.state.manager.jobs.enqueue(JobRequest.model_validate({"request_id": str(uuid4()), "create": {"project_id": "integration", "execution_mode": "isolated", "prompt": "修改app.txt并检查，等待人工采纳"}}))
        await app.state.manager.jobs.worker
    (root / "fixture.json").write_text(json.dumps({"run_id": job["run_id"]}), encoding="utf-8")


def main() -> None:
    """按--data-dir与--port启动专属本机服务；文件和缓存仅写指定目录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18853)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    asyncio.run(seed(root))
    uvicorn.run(application(root), host="127.0.0.1", port=args.port, loop="asyncio", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
