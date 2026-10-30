"""显式桌面验收入口：离线模型驱动真实原生窗口、文件工具、命令和规划批准。"""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
from agent_harness.desktop.server import main as server_main
from agent_harness.providers.base import ModelResponse, ToolCall


def response(name: str | None = None, **arguments) -> ModelResponse:
    """把工具 name/arguments 转为标准模型响应；name=None 返回固定的最终交付说明。"""
    calls = [ToolCall("desktop-" + name, name, json.dumps(arguments, ensure_ascii=False))] if name else []
    message = {"role": "assistant", "content": None if calls else "桌面验收完成：已生成 result.txt，并完成文件内容检查。"}
    if calls:
        message["tool_calls"] = [{"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}} for call in calls]
    return ModelResponse(message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop", response_model="offline-desktop-fixture")


class FixtureModel:
    """按真实工具历史决定下一步，跨验收服务重启仍能继续已批准的方案。"""

    def __init__(self) -> None:
        """初始化有限响应序列；所有文件与命令副作用由真实 Runner 执行。"""
        self.responses = [
            response("submit_plan", title="桌面端验收", summary="在当前测试项目生成结果并执行检查。", scope=["result.txt"], steps=["生成结果并测试"], verification=["检查文件正文和下载快照"]),
            response("write_file", path="result.txt", content="desktop acceptance passed\n"),
            response("run_command", argv=[sys.executable, "-c", "from pathlib import Path; assert Path('result.txt').read_text().strip() == 'desktop acceptance passed'; print('desktop checks passed')"]),
            response(),
        ]  # 提交方案会真实等待用户批准，后续响应只能在批准后执行。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """按 messages 中已有工具选择下个响应；tools 不执行，等待取消任务显式阻塞。"""
        if any(item.get("role") == "user" and "等待取消" in str(item.get("content", "")) for item in messages):
            await asyncio.Event().wait()
        # 成功的规划工具历史已经投影，由每次请求中的最新宿主状态确定是否已经有方案。
        planning = json.loads(messages[0]["content"].rsplit("\n", 1)[-1])
        if planning["current_plan"] is None:
            return self.responses[0]
        completed = {call.get("function", {}).get("name") for message in messages for call in message.get("tool_calls", [])}
        for name, reply in zip(("write_file", "run_command"), self.responses[1:]):
            if name not in completed:
                return reply
        return self.responses[-1]


def main() -> int:
    """准备 Electron 指定数据目录中的测试项目，向生产后台注入离线模型，返回退出码。"""
    root = Path(sys.argv[sys.argv.index("--data-dir") + 1]).resolve()
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    if not (project / ".git").exists():
        (project / "README.md").write_text("# 桌面验收专用项目\n", encoding="utf-8")
        for arguments in (["init", "-q"], ["add", "README.md"], ["-c", "user.name=HarnessTest", "-c", "user.email=test@localhost", "commit", "-qm", "fixture baseline"]):
            subprocess.run(["git", "-C", str(project), *arguments], check=True, capture_output=True)
    return server_main(model_client=FixtureModel())


if __name__ == "__main__":
    raise SystemExit(main())
