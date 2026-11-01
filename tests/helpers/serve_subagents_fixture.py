"""离线页面验收服务：真实队列、父子Runner、文件工具和独立验收，仅模型响应确定化。"""

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys

import uvicorn

from agent_harness.providers.base import ModelResponse, ToolCall
from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebVerifier, WebWorkspace


class SubagentFixtureModel:
    """按真实工具回执研究金额接口并交付结果；取消场景在子模型内等待，不启动后台进程。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """读取独立messages和可用tools，返回下一次工具调用或报告；不访问模型网络。

        正常任务从子文件回执提取金额，再由父报告生成JSON；含“取消”的任务在子研究等待。
        响应人为延迟0.4秒用于观察活动状态，真实副作用只由宿主工具执行。
        """
        await asyncio.sleep(0.4)
        observations = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        prompt = next(item["content"] for item in reversed(messages) if item["role"] == "user")
        calls, content = [], None
        if "你是只读子Agent" in messages[0]["content"]:
            if "取消" in prompt:
                await asyncio.Event().wait()
            if not observations:
                calls = [ToolCall("child-source", "read_file", '{"path":"service.py"}')]
            else:
                source = observations[-1]["data"]["content"]
                amount = re.search(r"return (\d+)", source).group(1)
                partial = "部分" in prompt
                reference = observations[-1]["evidence_ref"]["tool_call_id"]
                content = json.dumps({"summary": f"service.py返回金额{amount}分。", "findings": [{"category": "observation", "title": "金额单位", "detail": "返回整数分，不作元转换。", "evidence_call_ids": [reference]}],
                                      "limitations": ["本次只进行了初步读取。"] if partial else [],
                                      "pending": ["尚未核对调用方。"] if partial else [], "complete": not partial}, ensure_ascii=False)
        elif not observations:
            calls = [ToolCall("research", "delegate_readonly", json.dumps({"title": "研究金额接口", "objective": "读取service.py，核对金额单位和返回值。" + prompt, "role": "explore", "timeout_seconds": 180.0 if "取消" in prompt else 60.0}, ensure_ascii=False))]
        elif not observations[-1].get("ok"):
            content = "子研究未完成，本轮没有生成新文件，请核对失败原因。"
        elif "subagent" in (observations[-1].get("data") or {}):
            report = observations[-1]["data"]["subagent"]["report"]
            amount = int(re.search(r"金额(\d+)分", report["summary"]).group(1))
            calls = [ToolCall("deliver", "write_file", json.dumps({"path": "result.json", "content": json.dumps({"amount_minor": amount}), "overwrite": True}))]
        else:
            content = "已使用子 Agent 的读取报告生成result.json，独立检查结果请查看本轮详情。"
        message = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = [{"id": item.call_id, "type": "function", "function": {"name": item.name, "arguments": item.arguments_json}} for item in calls]
        return ModelResponse(message=message, tool_calls=calls, finish_reason="tool_calls" if calls else "stop", response_model="offline-subagents", usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8})


def main() -> None:
    """读取--data-dir和--port启动仅监听本机的验收服务；返回None，所有数据写指定D盘目录。

    默认端口18843；不读取真实密钥，不发送用户代码或修改生产数据，退出复用宿主取消清理。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18843)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "service.py").write_text('def price_minor():\n    """返回商品单价4200分；无参数，不修改文件。"""\n    return 4200\n', encoding="utf-8")
    (workspace / "AGENTS.md").write_text("金额使用整数分，子研究报告不能代替最终检查。", encoding="utf-8")
    check = root / "verify.py"
    check.write_text("import json,os\nfrom pathlib import Path\nassert json.loads((Path(os.environ['HARNESS_WORKSPACE'])/'result.json').read_text()) == {'amount_minor':4200}\nprint('独立金额检查通过')\n", encoding="utf-8")
    profile = root / "verify.toml"
    profile.write_text("argv = " + json.dumps([sys.executable, str(check)]) + "\ncwd = " + json.dumps(str(root)) + "\n", encoding="utf-8")
    web = WebConfig(project_dir=root, worktrees_root=root / "trees", frontend_dist=Path(__file__).resolve().parents[2] / "frontend/dist",
                    workspaces=[WebWorkspace(id="subagents", label="子Agent验收项目", path=workspace)],
                    verifiers=[WebVerifier(id="amount", label="独立金额检查", path=profile)])
    app = create_app(web, RunConfig(tools=["read_file", "search_files", "write_file"]), root / "runs", model_client=SubagentFixtureModel())

    def key(config):
        """为离线config返回固定占位凭据；不读取、输出或发送任何真实密钥。"""
        return "offline-subagents"

    app.state.manager._key_provider = key
    uvicorn.run(app, host="127.0.0.1", port=args.port, loop="asyncio")


if __name__ == "__main__":
    main()
