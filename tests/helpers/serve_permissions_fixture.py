"""动作审批的离线页面验收服务，使用真实队列、Runner、工具与验收进程。"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

import uvicorn

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.runtime.runner import REQUEST_CONTEXT
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace, WebVerifier
from agent_harness.web.jobs import JobRequest


class PermissionFixtureModel:
    """依据真实回执提供文件、命令或验收场景；不访问供应商或直接修改任务文件。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """读取 messages 和当前任务选择有限响应，返回 ModelResponse；所有副作用由真实工具执行。"""
        prompt = REQUEST_CONTEXT.get().manifest.task.prompt
        observations = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        successful = any(item.get("ok") for item in observations)
        calls = []
        if not successful and "验收" not in prompt:
            if "命令" in prompt:
                program = "from pathlib import Path; p=Path('command-count.txt'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); print('本机命令完成')"
                name, arguments = "run_command", {"argv": [sys.executable, "-c", program]}
            else:
                name, arguments = "write_file", {"path": "existing.txt" if "覆盖" in prompt else "result.txt", "content": "审批后才能看到这份文件。\n", "overwrite": True}
            calls = [ToolCall(str(uuid4()), name, json.dumps(arguments, ensure_ascii=False))]
        message = {"role": "assistant", "content": None if calls else "本轮处理结束，请核对真实文件与检查结果。"}
        if calls:
            message["tool_calls"] = [{"id": item.call_id, "type": "function", "function": {"name": item.name, "arguments": item.arguments_json}} for item in calls]
        return ModelResponse(message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop", response_model="offline-permissions", usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12})


def application(root: Path):
    """为专属 root 建立真实应用并返回；仅准备验收夹具，不修改用户项目或读取真实密钥。"""
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    if not (workspace / "existing.txt").exists():
        (workspace / "existing.txt").write_text("原有内容，需要先审阅再覆盖。\n", encoding="utf-8")
    profile = root / "checks.toml"
    profile.write_text("argv=" + json.dumps([sys.executable, "-c", "print('独立检查通过')"]) + "\n", encoding="utf-8")
    web = WebConfig(project_dir=root, worktrees_root=root / "trees", frontend_dist=Path(__file__).resolve().parents[2] / "frontend/dist",
        workspaces=[WebWorkspace(id="permissions", label="权限验收项目", path=workspace)], verifiers=[WebVerifier(id="checks", label="独立检查", path=profile)])
    app = create_app(web, RunConfig(tools=["read_file", "write_file", "edit_file", "search_files", "run_command"]), root / "runs", model_client=PermissionFixtureModel())

    def key(config) -> str:
        """为离线 config 返回固定占位密钥，不读取、打印或发送真实凭据。"""
        return "offline-permissions"
    app.state.manager._key_provider = key
    return app


async def seed(root: Path) -> None:
    """在空 root 生成四个待审批场景并保存编号，返回None；重复启动保留旧证据，不重放。"""
    if (root / "fixture.json").exists():
        return
    app = application(root)
    ids = {}
    async with app.router.lifespan_context(app):
        for name, prompt, mode, verifier in (("file", "生成结果文件", "strict", None), ("overwrite", "覆盖原有文件", "strict", None), ("command", "运行本机命令", "workspace_edit", None), ("verification", "执行验收", "workspace_edit", "checks")):
            job = await app.state.manager.jobs.enqueue(JobRequest.model_validate({"request_id": str(uuid4()), "create": {"workspace_id": "permissions", "prompt": prompt, "verifier_id": verifier, "config": {"permission_mode": mode}}}))
            await app.state.manager.jobs.worker
            ids[name] = job["run_id"]
    (root / "fixture.json").write_text(json.dumps(ids, indent=2), encoding="utf-8")


def main() -> None:
    """读取--data-dir和--port（默认18849）启动本机服务；所有生成文件只写专属目录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18849)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    asyncio.run(seed(root))
    uvicorn.run(application(root), host="127.0.0.1", port=args.port, loop="asyncio", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
