"""全工作台离线验收服务：真实任务、计划、子运行与文件工具，仅模型输出确定化。"""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from agent_harness.providers.base import ModelDelta, ModelResponse, ToolCall
from agent_harness.runtime.runner import REQUEST_CONTEXT
from agent_harness.schemas import RunConfig, RunLimits
from tests.helpers.models import configured_run
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace


class WorkspaceFixtureModel:
    """按已收到的真实工具回执推进页面验收，增量通过运行上下文进入持久化事件。"""

    def __init__(self, fast: bool = False) -> None:
        """fast=True 用于首次播种会话，False 使用可观察的文字间隔；不访问网络。"""
        self.fast = fast  # 播种时跳过等待，页面操作时每片间隔 120ms。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """根据 messages 的工具事实返回进度、调用或最终回答；tools 为宿主实际允许的工具。"""
        context = REQUEST_CONTEXT.get()
        start = max(index for index, item in enumerate(messages) if item["role"] == "user")
        recent = messages[start + 1:]
        names = {call["id"]: call["function"]["name"] for item in recent for call in item.get("tool_calls", [])}
        observations = [(names.get(item.get("tool_call_id")), json.loads(item["content"])) for item in recent if item["role"] == "tool"]
        used = {name for name, result in observations if result.get("ok")}
        calls: list[ToolCall] = []
        if context.manifest.delegation is not None:
            if "read_file" not in used:
                text, name, arguments = "我会读取计算模块，核对函数的输入和返回值。", "read_file", {"path": "app.py"}
            else:
                evidence = observations[-1][1]["evidence_ref"]["tool_call_id"]
                text = json.dumps({"summary": "已读取计算模块，接口接受两个数字并返回计算结果。", "findings": [{"category": "observation", "title": "计算接口", "detail": "函数定义和返回表达式已通过文件回执核对。", "evidence_call_ids": [evidence]}], "limitations": [], "pending": [], "complete": True}, ensure_ascii=False)
                name, arguments = None, None
        elif context.result.planning.phase == "planning":
            if "read_file" not in used:
                text, name, arguments = "我会先读取项目代码，确认当前结构，再整理实施步骤。", "read_file", {"path": "app.py"}
            else:
                text, name, arguments = "已完成初步读取。下面提交可审阅计划，批准后再修改文件。", "submit_plan", {
                    "title": "完善计算模块与交付验证", "summary": "先核对接口和调用方式，再完成计算逻辑调整，并通过实际文件与工具回执核对结果。",
                    "scope": ["app.py 的计算接口", "任务进度与结果证据"], "steps": ["读取代码并核对接口", "审阅现有实现", "调整代码并核对结果"], "verification": ["从聊天发送到工具执行的完整链路", "文件改动和任务状态正确展示"]}
        elif "read_file" not in used:
            text, name, arguments = "我会先读取项目文件，确认当前实现和需要调整的位置。", "read_file", {"path": "app.py"}
        elif not context.result.tasks.get("items"):
            text, name, arguments = "已读到当前代码。我会把接口核对与实现调整分别记为工作项。", "task_create", {"subject": "核对计算接口与实现", "description": "读取计算模块，核对输入参数和返回表达式。", "acceptance": ["实际读取 app.py", "明确本次调整范围"]}
        elif "delegate_readonly" not in used:
            text, name, arguments = "现在请探索 Agent 独立读取接口，随后我会结合报告继续调整。", "delegate_readonly", {"title": "核对计算模块", "objective": "读取 app.py，核对计算函数的输入与返回值。", "role": "explore", "timeout_seconds": 60}
        elif "write_file" not in used:
            text, name, arguments = "子 Agent 已返回读取报告。我会更新计算逻辑并保留中文函数说明。", "write_file", {"path": "app.py", "content": 'def add(left, right):\n    """返回 left 与 right 的和；无副作用。"""\n    return left + right\n', "overwrite": True}
        else:
            text, name, arguments = "已完成本轮调整。\n\n- 读取并核对了计算接口。\n- 子 Agent 的报告与来源可在工作面板查看。\n- 更新了 `app.py`，保留了中文职责说明。\n\n本轮未配置独立验收，工作项仍需按完成条件核对。", None, None
        if name:
            calls = [ToolCall(uuid4().hex, name, json.dumps(arguments, ensure_ascii=False))]
        for index in range(0, len(text), 7):
            context.model_delta(ModelDelta("text", text[index:index + 7]))
            if not self.fast:
                await asyncio.sleep(0.12)
        message = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}} for call in calls]
        return ModelResponse(message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop", response_model="offline-workspace", usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})


def application(root: Path, fast: bool = False):
    """为专属 root 构造应用；fast 控制演示速度，返回 ASGI 应用，不读取真实模型密钥。"""
    config = WebConfig(project_dir=root, worktrees_root=root / "trees", frontend_dist=Path(__file__).resolve().parents[2] / "frontend/dist", workspaces=[WebWorkspace(id="workspace", label="计算工具", path=root / "project"), WebWorkspace(id="notes", label="项目文档", path=root / "notes")])
    app = create_app(config, configured_run(tools=["read_file", "search_files", "write_file"], limits=RunLimits(max_model_turns=24, max_tool_calls=40)), root / "runs", model_client=WorkspaceFixtureModel(fast))
    app.state.manager._key_provider = lambda config: "offline-ui-fixture"  # config 仅遵循固定接口，无真实凭据。
    return app


async def seed(root: Path) -> None:
    """首次在 root 接纳一条自动执行和一条待批准计划，保存真实编号，返回 None。"""
    app = application(root, fast=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", trust_env=False) as client:
            runs = []
            for mode, prompt in [("auto", "核对并完善计算模块"), ("plan", "先规划计算模块的改造方案")]:
                response = await client.post("/api/jobs", json={"request_id": str(uuid4()), "create": {"workspace_id": "workspace", "prompt": prompt, "mode": mode}})
                response.raise_for_status()
                identifier = response.json()["run_id"]
                async with asyncio.timeout(30):
                    while (await client.get("/api/runs/" + identifier)).json()["active"]:
                        await asyncio.sleep(0.02)
                    await app.state.manager.wait_idle()
                runs.append(identifier)
            (root / "fixture.json").write_text(json.dumps({"runs": runs}), encoding="utf-8")


def main() -> None:
    """读取 --data-dir 专属目录与 --port（默认 18847），播种后监听本机，不改用户项目。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18847)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    marker = root / ".ui-fixture-owned"
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        raise ValueError("该目录已有其他内容，拒绝覆盖。")
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text("workspace-ui-fixture", encoding="utf-8")
    for name in ("project", "notes"):
        folder = root / name
        folder.mkdir(exist_ok=True)
        if not (folder / "app.py").exists():
            (folder / "app.py").write_text('def add(left, right):\n    """计算两个数的结果；等待本次核对。"""\n    return left - right\n', encoding="utf-8")
    if not (root / "fixture.json").is_file():
        asyncio.run(seed(root))
    uvicorn.run(application(root), host="127.0.0.1", port=args.port, loop="asyncio", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
